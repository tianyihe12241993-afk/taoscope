"""SN15 — ORO. Daily winner-take-all shopping-agent races.

WHAT MAKES THIS SUBNET AWKWARD, and why the adapter is shaped the way it is:

  * **It has a hard daily deadline.** Qualifying closes at 19:00 UTC, the race
    runs overnight and completes ~03:00 UTC, and the next race record appears
    when it does. Unlike most subnets here there IS a window to miss, so the
    countdown is the single most valuable thing this adapter says.

  * **Mid-race the whole field is SEALED.** While `status = RACE_RUNNING` every
    qualifier's `race_score`, `weighted_score`, `race_rank` and `window` come
    back `null`, and so does the race's own `top50_mean`. You get
    `scored_count` and nothing else. Rendering a stale board as if it were live
    would be a lie, so `/board` says sealed and shows progress instead.

  * **Ranking and elimination are judged on DIFFERENT numbers**, which is the
    trap that costs people their agent:

        elimination -> RAW race_score, bottom ~65% cut
        the crown   -> weighted_score = anchor + mean(delta over a 3-race window)
        anchor      -> that race's top50_mean; an unrun race seeds delta 0.0

    In race 124 Bolt-1 was ranked **#2 by weighted_score and eliminated anyway**
    on a raw 0.4889. A good window does not protect you. Both numbers are
    therefore rendered side by side, never one as a proxy for the other.

  * **You cannot win on debut, at any score.** Two of a newcomer's three window
    slots are seeds, so its edge is divided by three. Race 132's winner had only
    the 4th-best raw score of the day. Plan to survive three races.

  * **The leaderboard's SORT is a trap** (`score_type=qualifying` is the default
    and ranks on a saturated public suite shared by hundreds — every one of the
    visible top 25 has been measured as an eliminated agent). Its `?q=` search
    is fine and is used here for exactly that: looking one hotkey up, never for
    ranking anything.

Everything comes from `api.oroagents.com/v1/public/*`, unauthenticated. Measured
2026-08-24: seven requests back to back all returned 200, so unlike SN62 this
API does not need a paced client.

Identity comes from the CHAIN, the same way as SN62: our coldkeys ->
`neuron_live` for what is registered NOW, plus `neuron_snapshot` for what we
have EVER held on this netuid. The history half is not decoration -- our own
`sn15-1` deregistered on 2026-08-22 while its agent was still on the platform,
and a chain-only view would have made both silently vanish from /mine.
"""
from __future__ import annotations

import datetime as dt
import logging

from ..base import (CompEvent, SubnetAdapter, changed, diff_block, esc,
                    fetch_json, num, short, two_col)

log = logging.getLogger("taoscope.comp.sn15")

API = "https://api.oroagents.com/v1/public"

# The repo that defines the rules. `data/suites/` carries the public qualifying
# suite verbatim and `src/agent/` carries the scorer, so a commit here can move
# the gate under a submission. One repo only: the GitHub watcher is already over
# its unauthenticated 60/hour budget (see comp/github.py).
RULES_REPO = "ORO-AI/oro"

HOT_PATHS: list[tuple[str, str]] = [
    ("data/suites/",            "the public qualifying suite — a new one invalidates every local measurement"),
    ("src/agent/scoring.py",    "how a problem is scored"),
    ("src/agent/problem_scorer.py", "how a problem is scored"),
    ("src/agent/reasoning_scorer.py", "the reasoning coefficient"),
    ("src/agent/rewards/",      "the reward model behind the score"),
    ("subnet/validator/",       "how an agent is run and scored"),
    ("docs/",                   "the miner code-requirements policy — the anti-cheat rules a submission is judged against"),
]

# Minutes before the qualifying close that are worth a message. ASCENDING: after
# downtime a poller resuming at t-10m must fire "10 minutes", not "6 hours".
DEADLINE_MARKS = (15, 30, 60, 180, 360)

BURN_ON_REJECT_H = 18       # submission cooldown per hotkey, burned on rejection


def _dtp(value: str | None) -> dt.datetime | None:
    """Parse the API's timestamps, which are naive UTC."""
    if not value:
        return None
    try:
        d = dt.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return d.replace(tzinfo=dt.timezone.utc) if d.tzinfo is None else d


def _mins_until(value: str | None) -> float | None:
    d = _dtp(value)
    if d is None:
        return None
    return (d - dt.datetime.now(dt.timezone.utc)).total_seconds() / 60.0


def _dur(minutes: float | None) -> str:
    if minutes is None:
        return "—"
    m = abs(minutes)
    s = f"{int(m)}m" if m < 60 else (f"{m / 60:.1f}h" if m < 48 * 60
                                     else f"{m / 1440:.1f}d")
    return s if minutes >= 0 else f"{s} ago"


class SN15(SubnetAdapter):
    # ---- identity -----------------------------------------------------------
    netuid = 15
    slug = "oro"
    label = "ORO"

    # A daily window needs finer resolution than a continuous queue: 5 min is
    # enough to land every deadline mark on the right side of its boundary.
    poll_seconds = 300

    repos: list[str] = [RULES_REPO]

    links = {
        "leaderboard": "https://oroagents.com/leaderboard",
        "api": API,
        "code policy": "https://docs.oroagents.com/docs/miners/code-requirements",
        "repo": f"https://github.com/{RULES_REPO}",
    }

    alerts = {
        "window_closing": "the qualifying window is closing — after 19:00 UTC "
                          "nothing new enters today's race",
        "race_open": "a new race record exists and qualifying is open again",
        "race_done": "a race completed — winner, the new anchor, who was cut, "
                     "and where we placed",
        "king_change": "the crown moved",
        "bar": "the score to beat for the crown changed",
        "our_run": "one of our agents changed state — qualified, entered, "
                   "placed or was eliminated",
        "eliminated": "one of our agents was CUT — elimination is on RAW race "
                      "score, not on the weighted score that decides the crown",
        "dereg": "a hotkey of ours left the metagraph",
        "suite": "the qualifying suite changed — every local measurement is "
                 "now against the wrong problems",
        "repo_impact": "a commit touched a file that defines the rules",
    }

    def __init__(self) -> None:
        self._sha_seen: str | None = None

    # ---- 1. COLLECT ---------------------------------------------------------
    async def snapshot(self) -> dict:
        s: dict = {}
        s.update(await self._race())
        s.update(await self._crown())
        s.update(await self._field())
        s.update(await self._queue())
        s.update(await self._repo_impact())
        s["ours"] = await self._ours(s)
        s.update(await self._mine_meta(s["ours"]))
        return s

    # -- the race --------------------------------------------------------------
    async def _race(self) -> dict:
        """The live race record and its field.

        `phase` is derived from EVIDENCE, not from `status`: what a miner needs
        to know is whether they can still enter, whether the field is sealed, or
        whether results are readable — and the label alone answers none of those.
        """
        d = await fetch_json(f"{API}/races/current", timeout=45.0)
        if not isinstance(d, dict) or not isinstance(d.get("race"), dict):
            return {}
        r = d["race"]
        out: dict = {
            "race_id": r.get("race_id"),
            "race_no": r.get("race_number"),
            "suite_id": r.get("suite_id"),
            "status": r.get("status"),
            "closes_at": r.get("qualifying_closes_at"),
            "started_at": r.get("race_started_at"),
            "completed_at": r.get("race_completed_at"),
            "eta": r.get("projected_completion_at"),
            "n_qualifiers": r.get("qualifier_count"),
            "scored_count": r.get("scored_count"),
            "qual_gate": r.get("qualifying_threshold"),
        }
        if r.get("top50_mean") is not None:
            out["anchor"] = r["top50_mean"]         # the delta baseline
        if r.get("winner_agent_name"):
            out["winner"] = r["winner_agent_name"]
            out["winner_score"] = r.get("winner_score")

        mins = _mins_until(r.get("qualifying_closes_at"))
        if r.get("race_completed_at"):
            phase = "complete"
        elif r.get("race_started_at"):
            phase = "racing"
        else:
            phase = "qualifying"
        out["phase"] = phase
        # Only meaningful while the window is actually ahead of us. Once the
        # race starts, the next close is not knowable until the next race record
        # is created -- so the key is omitted rather than going negative.
        if phase == "qualifying" and mins is not None and mins > 0:
            out["mins_to_close"] = mins
            marks = [m for m in DEADLINE_MARKS if mins <= m]
            out["close_mark"] = min(marks) if marks else 0

        quals = d.get("qualifiers") or []
        if quals:
            out["n_field"] = len(quals)
            out["n_eliminated"] = len([q for q in quals if q.get("eliminated_at")])
            # Ranked rows exist only once the race completes; mid-race every one
            # of these is null and the board must say so rather than show blanks.
            ranked = [q for q in quals if q.get("race_rank") is not None]
            out["sealed"] = not ranked
            if ranked:
                ranked.sort(key=lambda q: q["race_rank"])
                out["board"] = [{
                    "rank": q.get("race_rank"),
                    "name": q.get("agent_name"),
                    "hk": q.get("miner_hotkey"),
                    "ver": q.get("version_number"),
                    "raw": q.get("race_score"),
                    "weighted": q.get("weighted_score"),
                    "cut": bool(q.get("eliminated_at")),
                    "seeds": len([w for w in (q.get("window") or [])
                                  if w.get("is_seed")]),
                } for q in ranked[:20]]
            out["_quals"] = quals               # transient: consumed by _ours
        return out

    async def _crown(self) -> dict:
        """Who holds the crown and what it costs to take it."""
        d = await fetch_json(f"{API}/top")
        out: dict = {}
        if isinstance(d, dict) and d.get("top_agent_version_id"):
            pol = d.get("policy") or {}
            out.update({
                "king_id": d["top_agent_version_id"],
                "king_hk": d.get("top_miner_hotkey"),
                "king_score": d.get("top_score"),
                "days_as_top": pol.get("days_as_top"),
                # Decays continuously with days_as_top, so it is RENDERED but
                # never diffed -- see _diff_crown.
                "threshold": d.get("challenge_threshold"),
                "margin": d.get("margin"),
            })
        p = await fetch_json(f"{API}/top-miner-payout")
        if isinstance(p, dict) and p.get("tao_per_day") is not None:
            out["king_name"] = p.get("agent_name")
            out["tao_day"] = p["tao_per_day"]
            out["usd_day"] = p.get("usd_per_day")
        return out

    async def _field(self) -> dict:
        """Field pressure, from the leaderboard ENVELOPE only.

        The rows are not read and the ranking is not shown: `score_type`
        defaults to `qualifying`, which ranks a saturated public suite, and
        every one of its visible top 25 was measured as an already-eliminated
        agent. The envelope's counters are honest; the ordering is not.
        """
        d = await fetch_json(f"{API}/leaderboard?limit=1")
        if not isinstance(d, dict):
            return {}
        out = {}
        for src, dst in (("unique_miners", "n_miners"),
                         ("agents_submitted_24h", "n_24h"),
                         ("total", "n_agents_ever")):
            if d.get(src) is not None:
                out[dst] = d[src]
        suite = d.get("suite") or {}
        if suite.get("suite_id") is not None:
            out["suite_ver"] = suite.get("suite_version")
        return out

    async def _queue(self) -> dict:
        """Evaluation backlog — how far the overnight race has to go."""
        d = await fetch_json(f"{API}/evaluations/pending")
        if not isinstance(d, dict):
            return {}
        summ = d.get("summary") or {}
        out = {}
        for src, dst in (("total_pending", "q_pending"),
                         ("total_runs_needed", "q_runs"),
                         ("total_runs_active", "q_active")):
            if summ.get(src) is not None:
                out[dst] = summ[src]
        return out

    # -- our own agents --------------------------------------------------------
    #
    # Same chain-derived identity as SN62, with one addition this subnet forced:
    # `neuron_snapshot` as well as `neuron_live`, so a hotkey we USED to hold is
    # still recognised as ours. Our `sn15-1` deregistered on 2026-08-22 with an
    # eliminated agent still on the platform; a registered-only view would have
    # dropped both without a word, which is the opposite of what /mine is for.

    async def _coldkeys(self) -> list[str]:
        import os
        env = os.getenv("SN15_COLDKEYS", "").strip()
        if env:
            return [c.strip() for c in env.split(",") if c.strip()]
        from ...db import pool
        rows = await pool().fetch("SELECT DISTINCT coldkey FROM my_coldkey")
        return sorted(r["coldkey"] for r in rows if r["coldkey"])

    async def _hotkeys(self, coldkeys: list[str]) -> tuple[dict, set] | None:
        """(registered now -> uid, ever held). None when the metagraph is unknown."""
        if not coldkeys:
            return {}, set()
        from ...db import pool
        if not await pool().fetchval(
                "SELECT 1 FROM neuron_live WHERE netuid=$1 LIMIT 1", self.netuid):
            return None
        live = await pool().fetch(
            "SELECT uid, hotkey FROM neuron_live"
            " WHERE netuid=$1 AND coldkey = ANY($2::text[])", self.netuid, coldkeys)
        ever = await pool().fetch(
            "SELECT DISTINCT hotkey FROM neuron_snapshot"
            " WHERE netuid=$1 AND coldkey = ANY($2::text[])", self.netuid, coldkeys)
        reg = {r["hotkey"]: r["uid"] for r in live if r["hotkey"]}
        return reg, {r["hotkey"] for r in ever if r["hotkey"]} | set(reg)

    async def _ours(self, s: dict) -> dict:
        coldkeys = await self._coldkeys()
        if not coldkeys:
            return {}
        pair = await self._hotkeys(coldkeys)
        if pair is None:
            return {}
        reg, ever = pair
        if not ever:
            return {}

        # Rows for this race come free out of the payload already fetched.
        in_race = {q.get("miner_hotkey"): q for q in (s.get("_quals") or [])
                   if q.get("miner_hotkey") in ever}

        out: dict = {}
        for hk in sorted(ever):
            # `?q=` is a search, not the trapped ranking: it returns every agent
            # version this hotkey has ever had, which is the only way to see one
            # that is not in the current race.
            d = await fetch_json(f"{API}/leaderboard?q={hk}&limit=20")
            entries = (d or {}).get("entries") or []
            entries.sort(key=lambda e: e.get("version_number") or 0, reverse=True)
            q = in_race.get(hk)
            latest = entries[0] if entries else None
            if not latest and not q:
                # A registered hotkey that has never submitted. Still ours, and
                # still worth counting -- see _mine_meta.
                continue
            row = {
                "hk": hk,
                "uid": reg.get(hk),
                "registered": hk in reg,
                "name": (q or latest or {}).get("agent_name"),
                "ver": (q or latest or {}).get("version_number"),
                "agent_id": (q or {}).get("agent_version_id")
                            or (latest or {}).get("agent_version_id"),
                "n_versions": len(entries),
                "qual": (q or {}).get("qualifying_score")
                        or (latest or {}).get("final_score"),
                "raw": (q or {}).get("race_score")
                       or (latest or {}).get("race_score"),
                "weighted": (q or {}).get("weighted_score")
                            or (latest or {}).get("weighted_score"),
                "rank": (q or {}).get("race_rank"),
                "cut_at": (q or latest or {}).get("eliminated_at"),
                "in_race": q is not None,
                "banned": bool((latest or {}).get("is_miner_banned")),
                "seeds": len([w for w in ((q or {}).get("window") or [])
                              if w.get("is_seed")]),
            }
            row["phase"] = self._phase_of(row, s)
            out[short(hk, 8, 4)] = row
        return out

    @staticmethod
    def _phase_of(row: dict, s: dict) -> str:
        """What is actually happening to this agent, in words a miner acts on.

        Derived, not passed through: the platform has no single field for it,
        and the two that come closest (`is_active_qualifier`, `eliminated_at`)
        are both false for an agent sitting in the queue.
        """
        if row.get("banned"):
            return "miner BANNED"
        if row.get("cut_at"):
            return "eliminated — cut on raw score"
        if row.get("in_race"):
            if row.get("rank") is not None:
                return f"placed #{row['rank']} of {s.get('n_field', '?')}"
            return "in today's race — field sealed until it completes"
        if row.get("qual") is not None:
            gate = s.get("qual_gate")
            if gate is not None and float(row["qual"]) < float(gate):
                return f"qualifying score {row['qual']} — BELOW the {gate} gate"
            return "qualified, not in the current race"
        return "submitted, not yet qualified"

    async def _mine_meta(self, ours: dict) -> dict:
        coldkeys = await self._coldkeys()
        out: dict = {"n_coldkeys": len(coldkeys)}
        if not coldkeys:
            return out
        pair = await self._hotkeys(coldkeys)
        if pair is None:
            return out
        reg, ever = pair
        out["n_registered"] = len(reg)
        out["our_hotkeys"] = sorted(reg)
        out["n_ever"] = len(ever)
        submitted = {o.get("hk") for o in ours.values()}
        out["n_idle"] = len([h for h in reg if h not in submitted])
        return out

    # -- the repo --------------------------------------------------------------
    async def _repo_impact(self) -> dict:
        """One GitHub request per REAL commit, none otherwise. See sn62."""
        from .. import github, store

        sha = None
        for row in await store.repos_for(self.netuid):
            if row.get("repo") == RULES_REPO:
                sha = row.get("sha")
                break
        if not sha:
            return {}
        if self._sha_seen is None:
            self._sha_seen = sha
            return {}
        if sha == self._sha_seen:
            return {}
        base, head = self._sha_seen, sha
        self._sha_seen = sha
        data, code = await github.conditional_json(
            f"https://api.github.com/repos/{RULES_REPO}/compare/{base}...{head}",
            cache_key=f"sn15:compare:{base}", timeout=30.0)
        if not isinstance(data, dict):
            log.info("sn15: compare %s...%s unavailable (HTTP %s)",
                     base[:8], head[:8], code)
            return {}
        files = [f.get("filename") or "" for f in (data.get("files") or [])]
        hot: list[dict] = []
        for path, why in HOT_PATHS:
            hit = [f for f in files if f.startswith(path)]
            if hit and not any(h["path"] == path for h in hot):
                hot.append({"path": path, "why": why, "n": len(hit)})
        return {"repo_range": f"{base[:10]}...{head[:10]}",
                "repo_n_files": len(files),
                "repo_commits": len(data.get("commits") or []),
                "repo_hot": hot}

    # ---- 2. DIFF ------------------------------------------------------------
    def diff(self, old: dict, new: dict) -> list[CompEvent]:
        out: list[CompEvent] = []
        if not old:
            return out
        out += self._diff_window(old, new)
        out += self._diff_race(old, new)
        out += self._diff_crown(old, new)
        out += self._diff_ours(old, new)
        out += self._diff_repo(old, new)
        return out

    # -- the window ------------------------------------------------------------
    def _diff_window(self, old: dict, new: dict) -> list[CompEvent]:
        """The daily deadline. The one alert this subnet exists to send."""
        out = []
        if changed(old, new, "race_no") and new.get("phase") == "qualifying":
            out.append(CompEvent(
                kind="race_open", severity="warn", icon="🏁",
                title=f"ORO race {new.get('race_no')} — qualifying is open",
                body=f"closes <b>{_dur(new.get('mins_to_close'))}</b> from now "
                     f"(19:00 UTC)\ngate {num(new.get('qual_gate'))} on the "
                     f"public suite\n" + self._bar_lines(new),
                dedup_key=f"open:{new.get('race_no')}"))

        if not changed(old, new, "close_mark"):
            # First appearance of the mark is a baseline, as everywhere else.
            # The window OPENING is announced by race_open above, so nothing is
            # lost -- except on a cold start inside the last few minutes of a
            # window, which stays silent by design rather than firing a deadline
            # warning on every deploy.
            return out
        mark = new.get("close_mark") or 0
        was = old.get("close_mark") or 0
        if not mark:
            return out                      # window closed, or plenty of time
        if was and mark >= was:
            return out                      # only a TIGHTENING deadline is news
        mine_in = [o for o in (new.get("ours") or {}).values() if o.get("in_race")]
        # Same clock, different decision: at t-30m with nothing entered there is
        # something to do, and at t-30m with an entry in there is not. Only one
        # of those should wake anybody.
        sev = "info" if mine_in else ("critical" if mark <= 30 else "warn")
        body = (f"race {new.get('race_no')} · qualifying closes in "
                f"<b>{_dur(new.get('mins_to_close'))}</b>\n"
                f"{new.get('n_qualifiers', '?')} agents already in")
        body += ("\n<i>we have an entry in.</i>" if mine_in else
                 "\n<i>nothing of ours is in — a submission needs "
                 f"{BURN_ON_REJECT_H}h of cooldown headroom and clears "
                 "qualifying before it can race.</i>")
        out.append(CompEvent(
            kind="window_closing", severity=sev, icon="⏳",
            title=f"⏳ ORO qualifying closes in {_dur(new.get('mins_to_close'))}",
            body=body, dedup_key=f"close:{new.get('race_no')}:{mark}"))
        return out

    # -- the race --------------------------------------------------------------
    def _diff_race(self, old: dict, new: dict) -> list[CompEvent]:
        out = []
        if changed(old, new, "phase") and new.get("phase") == "complete":
            ours = new.get("ours") or {}
            placed = [o for o in ours.values() if o.get("rank")]
            L = [f"🏆 <b>{esc(new.get('winner') or '?')}</b> · raw "
                 f"{num(new.get('winner_score'))}"]
            if new.get("anchor") is not None:
                L.append(f"anchor (top50 mean) <b>{num(new['anchor'], 4)}</b> "
                         f"— every delta is measured off this")
            if new.get("n_eliminated"):
                L.append(f"<b>{new['n_eliminated']}</b> of "
                         f"{new.get('n_field', '?')} eliminated")
            for o in sorted(placed, key=lambda o: o.get("rank") or 999):
                L.append(f"ours <b>{esc(o.get('name'))}</b> #{o.get('rank')} · "
                         f"raw {num(o.get('raw'))} · "
                         f"weighted {num(o.get('weighted'))}")
            if not placed and ours:
                L.append("<i>nothing of ours placed in this race.</i>")
            # A race completes every night. That is only worth a buzz when we
            # were in it -- otherwise it is the daily result of a contest we did
            # not enter, and a monitor that wakes someone for that gets muted.
            entered = bool(placed) or any(o.get("in_race") for o in ours.values())
            out.append(CompEvent(
                kind="race_done", severity="warn" if entered else "info",
                icon="🏁",
                title=f"ORO race {new.get('race_no')} complete",
                body="\n".join(L), dedup_key=f"done:{new.get('race_id')}"))

        # A new qualifying suite invalidates every local measurement we hold.
        if changed(old, new, "suite_id") or changed(old, new, "suite_ver"):
            out.append(CompEvent(
                kind="suite", severity="critical", icon="📋",
                title="ORO qualifying suite changed",
                body=f"suite {esc(old.get('suite_id'))} v{esc(old.get('suite_ver'))} "
                     f"→ <b>{esc(new.get('suite_id'))} "
                     f"v{esc(new.get('suite_ver'))}</b>\n"
                     "<i>every local score is now against the wrong problems.</i>",
                dedup_key=f"suite:{new.get('suite_id')}:{new.get('suite_ver')}"))
        return out

    def _diff_crown(self, old: dict, new: dict) -> list[CompEvent]:
        out = []
        if changed(old, new, "king_id") and old.get("king_id"):
            out.append(CompEvent(
                kind="king_change", severity="critical", icon="👑",
                title="👑 New ORO crown",
                body=f"<b>{esc(new.get('king_name') or '?')}</b> at "
                     f"{num(new.get('king_score'), 4)}\n"
                     f"was {num(old.get('king_score'), 4)}\n"
                     + self._bar_lines(new),
                dedup_key=str(new.get("king_id"))))
        elif changed(old, new, "king_score"):
            # `challenge_threshold` is deliberately NOT diffed: it decays
            # continuously with days_as_top, so it moves every single poll and
            # would fire an alert every five minutes saying nothing.
            out.append(CompEvent(
                kind="bar", severity="info", icon="🎯",
                title=f"ORO crown score → {num(new.get('king_score'), 4)}",
                body=f"same holder {esc(new.get('king_name') or '?')}\n"
                     + self._bar_lines(new),
                dedup_key=f"bar:{new.get('king_score')}"))
        return out

    # -- ours ------------------------------------------------------------------
    def _diff_ours(self, old: dict, new: dict) -> list[CompEvent]:
        out = []
        if changed(old, new, "n_registered"):
            try:
                lost = int(old["n_registered"]) - int(new["n_registered"])
            except (TypeError, ValueError):
                lost = 0
            if lost > 0:
                gone = sorted(set(old.get("our_hotkeys") or [])
                              - set(new.get("our_hotkeys") or []))
                out.append(CompEvent(
                    kind="dereg", severity="warn", icon="🔌",
                    title=f"{lost} of our SN15 hotkeys deregistered",
                    body="\n".join(f"<code>{esc(short(h))}</code>" for h in gone[:8])
                         + f"\n{old['n_registered']} → "
                           f"<b>{new['n_registered']}</b> registered\n"
                           "<i>a deregistered hotkey cannot submit and earns "
                           "nothing; re-registering costs the burn.</i>",
                    dedup_key=",".join(gone) or str(new["n_registered"])))

        was_all, now_all = old.get("ours") or {}, new.get("ours") or {}
        for ref, cur in now_all.items():
            was = was_all.get(ref)
            if was is None:
                if was_all:
                    out.append(CompEvent(
                        kind="our_run", severity="info", icon="📤",
                        title=f"New ORO agent — {esc(cur.get('name') or ref)}",
                        body=f"<b>{esc(cur.get('name') or '?')}</b> "
                             f"v{cur.get('ver')}\n"
                             f"{esc(cur.get('phase') or '')}",
                        dedup_key=f"new:{cur.get('agent_id')}"))
                continue
            if was.get("phase") == cur.get("phase"):
                continue
            # Being cut is its own alert: it is terminal for that agent and it
            # is decided on a number the crown standings never show.
            if cur.get("cut_at") and not was.get("cut_at"):
                out.append(CompEvent(
                    kind="eliminated", severity="warn", icon="💀",
                    title=f"ORO eliminated — {esc(cur.get('name') or ref)}",
                    body=f"raw race score <b>{num(cur.get('raw'))}</b> vs anchor "
                         f"{num(new.get('anchor'), 4)}\n"
                         f"<i>the cut is on RAW score — a good weighted score "
                         f"does not protect an entry.</i>",
                    dedup_key=f"cut:{cur.get('agent_id')}"))
                continue
            out.append(CompEvent(
                kind="our_run", severity="info", icon="🔬",
                title=f"Our agent {esc(cur.get('name') or ref)} → "
                      f"{esc(cur.get('phase') or '?')}",
                body=f"<b>{esc(cur.get('name') or '?')}</b> v{cur.get('ver')}\n"
                     f"{esc(was.get('phase') or '?')} → "
                     f"<b>{esc(cur.get('phase') or '?')}</b>",
                dedup_key=f"{ref}:{cur.get('phase')}"))
        return out

    def _diff_repo(self, old: dict, new: dict) -> list[CompEvent]:
        hot = new.get("repo_hot") or []
        rng = new.get("repo_range")
        if not rng or rng == old.get("repo_range") or not hot:
            return []
        L = [f"<code>{esc(rng)}</code> · {new.get('repo_commits', '?')} commit(s), "
             f"{new.get('repo_n_files', '?')} file(s)", ""]
        for h in hot:
            L.append(f"⚠️ <code>{esc(h['path'])}</code> ×{h['n']}\n   "
                     f"<i>{esc(h['why'])}</i>")
        L += ["", f"https://github.com/{RULES_REPO}/compare/{esc(rng)}"]
        return [CompEvent(kind="repo_impact", severity="critical", icon="📜",
                          title="ORO commit touched the rules",
                          body="\n".join(L), dedup_key=str(rng))]

    # ---- 3. RENDER ----------------------------------------------------------
    def _bar_lines(self, s: dict) -> str:
        if s.get("king_score") is None:
            return ""
        L = ["", "<b>🎯 TO TAKE THE CROWN</b>"]
        L.append(f"weighted ≥ <b>{num(s.get('threshold'), 4)}</b> "
                 f"<i>(crown {num(s.get('king_score'), 4)} + margin "
                 f"{num(s.get('margin'), 4)}, decaying)</i>")
        L.append("<i>…and you cannot get there on debut: 2 of a new agent's 3 "
                 "window slots are seeds.</i>")
        if s.get("tao_day") is not None:
            L.append(f"prize <b>τ{num(s['tao_day'], 1)}/day</b>"
                     + (f" (${num(s.get('usd_day'), 0)})" if s.get("usd_day")
                        else ""))
        return "\n".join(L)

    def _phase_line(self, s: dict) -> str:
        ph = s.get("phase")
        if ph == "qualifying":
            return (f"<b>🟢 QUALIFYING OPEN</b> · closes in "
                    f"<b>{_dur(s.get('mins_to_close'))}</b>")
        if ph == "racing":
            done, tot = s.get("scored_count"), s.get("n_qualifiers")
            prog = f"{done}/{tot}" if done is not None and tot else "?"
            return (f"<b>🔒 RACING</b> · {prog} scored · results in "
                    f"<b>{_dur(_mins_until(s.get('eta')))}</b>\n"
                    f"<i>every score in this race is sealed until it completes.</i>")
        if ph == "complete":
            return (f"<b>✅ COMPLETE</b> · won by "
                    f"{esc(s.get('winner') or '?')} at {num(s.get('winner_score'))}")
        return "<b>—</b>"

    def render_state(self, s: dict) -> str:
        L = [f"<b>SN{self.netuid} · {esc(self.label)}</b> — race "
             f"{s.get('race_no', '?')}", ""]
        L.append(self._phase_line(s))
        L.append("")
        L.append(f"<b>👑 CROWN</b>  {esc(s.get('king_name') or '—')} · "
                 f"<code>{num(s.get('king_score'), 4)}</code>"
                 + (f" · held {num(s.get('days_as_top'), 1)}d"
                    if s.get("days_as_top") is not None else ""))
        L.append(f"<b>📥 FIELD</b>  {s.get('n_qualifiers', '?')} qualifiers · "
                 f"{s.get('n_24h', '?')} submitted in 24h")
        L.append(self._bar_lines(s))
        ours = s.get("ours") or {}
        if ours:
            L += ["", "<b>🔬 OURS</b>"]
            for ref, o in ours.items():
                warn = "" if o.get("registered") else " ⚠️ dereg"
                L.append(f"<code>{esc(o.get('name') or ref)}</code> "
                         f"v{o.get('ver')} · {esc(o.get('phase') or '')}{warn}")
        elif s.get("n_registered"):
            L += ["", f"<b>🔬 OURS</b>  {s['n_registered']} hotkey(s) registered, "
                      f"<i>nothing submitted</i>"]
        else:
            L += ["", "<i>nothing of ours registered on this subnet.</i>"]
        return "\n".join(L)

    def render_info(self, s: dict) -> str:
        L = [f"<b>SN{self.netuid} · {esc(self.label)} — the rules</b>", "",
             "One race a day. Qualifying closes <b>19:00 UTC</b>, the race runs "
             "overnight on 90 hidden problems and completes around 03:00 UTC.", ""]
        L.append(two_col([
            ("race", str(s.get("race_no") or "?")),
            ("phase", str(s.get("phase") or "?")),
            ("field", str(s.get("n_qualifiers") or "?")),
            ("gate", num(s.get("qual_gate"))),
            ("anchor", num(s.get("anchor"), 4)),
            ("cut", str(s.get("n_eliminated") or "—")),
            ("24h subs", str(s.get("n_24h") or "?")),
            ("miners", str(s.get("n_miners") or "?")),
        ], 20))
        L += ["", "<b>Two different numbers decide two different things</b>",
              "• <b>elimination</b> is on <b>raw</b> race score — bottom ~65% cut",
              "• <b>the crown</b> is on <b>weighted</b> = anchor + mean(delta) "
              "over a 3-race window",
              "<i>An agent can rank #2 on weighted and still be eliminated on "
              "raw. A good window does not protect you.</i>",
              "", "<b>Anchor</b> = that race's top-50 mean, so difficulty swings "
              "are absorbed and a delta is comparable across races.",
              self._bar_lines(s)]
        if s.get("q_pending") is not None:
            L += ["", "<b>Queue</b>",
                  two_col([("pending", str(s.get("q_pending"))),
                           ("runs", str(s.get("q_runs") or "?")),
                           ("active", str(s.get("q_active") or "?"))], 16)]
        ch = s.get("_chain") or {}
        if ch:
            L += ["", "<b>Chain</b>",
                  two_col([("reg", "OPEN" if ch.get("registration_allowed") else "closed"),
                           ("cost", f"τ{num(ch.get('burn_tao'), 4)}"),
                           ("uids", f"{ch.get('num_uids')}/{ch.get('max_uids')}"),
                           ("operators", str(ch.get("unique_coldkeys")))], 20)]
        for r in (s.get("_repos") or []):
            L.append(f"📦 <code>{esc(r['repo'])}</code> "
                     f"<code>{esc((r.get('sha') or '')[:10])}</code>")
        return "\n".join(L)

    def render_board(self, s: dict, limit: int = 10) -> str:
        if s.get("sealed"):
            done, tot = s.get("scored_count"), s.get("n_qualifiers")
            return (f"<b>ORO race {s.get('race_no', '?')}</b> — <b>sealed</b>\n\n"
                    f"{done if done is not None else '?'} of {tot or '?'} scored · "
                    f"results in <b>{_dur(_mins_until(s.get('eta')))}</b>\n\n"
                    "<i>Every qualifier's race score, weighted score and rank "
                    "come back null while a race is running — including the "
                    "incumbent's. There is no board to show yet, and the "
                    "leaderboard's own ranking is a different, saturated "
                    "score.</i>")
        board = s.get("board") or []
        if not board:
            return "No completed race to show yet."
        ours = {o.get("hk") for o in (s.get("ours") or {}).values()}
        rows = [f"  {'#':<3}{'weighted':>9}{'raw':>7}  name"]
        for r in board[:limit]:
            mine = r.get("hk") in ours
            # `x` gets a column of its own: appended to the name it read as
            # part of it ("Bolt<1>x"), the same collision the SN62 board had
            # between its approval marker and the cost.
            mark = " x" if r.get("cut") else ""
            rows.append(f"{'+' if mine else ' '} "
                        f"{str(r.get('rank') or '?'):<3}"
                        f"{num(r.get('weighted'), 4):>9}{num(r.get('raw')):>7}  "
                        f"{esc(str(r.get('name') or '')[:12])}{mark}")
        L = [f"<b>ORO race {s.get('race_no', '?')}</b> · "
             f"{s.get('n_field', '?')} finishers, "
             f"{s.get('n_eliminated', '?')} cut",
             diff_block(rows),
             "<i>green + = ours · x = eliminated (on RAW, not on weighted)</i>",
             self._bar_lines(s)]
        return "\n".join(L)

    def render_me(self, s: dict) -> str:
        ours = s.get("ours") or {}
        if not s.get("n_coldkeys"):
            return ("No coldkey configured for SN15.\n"
                    "Add one to <code>my_coldkey</code>, or set "
                    "<code>SN15_COLDKEYS</code>.")
        L = [f"<b>Our SN15 position</b> · race {s.get('race_no', '?')}", ""]
        n_reg, n_ever = s.get("n_registered"), s.get("n_ever")
        if n_reg is None:
            L.append(f"{s['n_coldkeys']} coldkey(s) · <i>metagraph unavailable</i>")
        else:
            line = f"{s['n_coldkeys']} coldkey(s) · <b>{n_reg}</b> registered here"
            if n_ever and n_ever > n_reg:
                line += f" · {n_ever - n_reg} formerly held"
            L.append(line)
        if not ours:
            L.append("")
            L.append("<i>no agent of ours on the platform.</i>")
            if n_reg:
                L.append(f"<b>{n_reg}</b> registered hotkey(s) could submit.")
            L.append(self._bar_lines(s))
            return "\n".join(L)

        for ref, o in ours.items():
            L += ["", f"<code>{esc(o.get('name') or ref)}</code> v{o.get('ver')} · "
                      f"<b>{esc(o.get('phase') or '')}</b>"]
            bits = []
            if o.get("qual") is not None:
                bits.append(f"qualifying {num(o['qual'])}")
            if o.get("raw") is not None:
                bits.append(f"raw {num(o['raw'])}")
            if o.get("weighted") is not None:
                bits.append(f"weighted {num(o['weighted'], 4)}")
            if bits:
                L.append("   " + " · ".join(bits))
            if o.get("raw") is not None and s.get("anchor") is not None:
                L.append(f"   {float(o['raw']) - float(s['anchor']):+.4f} vs anchor")
            if o.get("seeds"):
                L.append(f"   ⚠️ {o['seeds']} of 3 window slots are seeds — "
                         f"the crown is out of reach until they roll off")
            if not o.get("registered"):
                L.append("   ⚠️ <b>hotkey is no longer registered</b> — it "
                         "cannot submit again without re-registering")
        if s.get("n_idle"):
            L.append("")
            L.append(f"<b>💤 IDLE</b>  {s['n_idle']} registered hotkey(s) with "
                     f"nothing submitted")
        L.append(self._bar_lines(s))
        return "\n".join(L)

    def ours_summary(self, s: dict) -> str:
        ours = s.get("ours") or {}
        if not ours:
            return f"{s['n_registered']} idle" if s.get("n_registered") else ""
        racing = [o for o in ours.values() if o.get("in_race")]
        if racing:
            return f"{len(racing)} in race {s.get('race_no', '?')}"
        return f"{len(ours)} agent(s), none racing"

    def deadline_minutes(self, s: dict) -> int | None:
        m = s.get("mins_to_close")
        return int(m) if m is not None and m > 0 else None

    def render_digest(self, s: dict) -> str:
        tail = ""
        if s.get("phase") == "qualifying" and s.get("mins_to_close") is not None:
            tail = f" · closes {_dur(s['mins_to_close'])}"
        elif s.get("phase") == "racing":
            tail = f" · racing {s.get('scored_count', '?')}/{s.get('n_qualifiers', '?')}"
        return (f"<b>SN15</b> {esc(self.label)} — 👑 "
                f"{esc(s.get('king_name') or '—')} "
                f"<code>{num(s.get('king_score'), 4)}</code> · "
                f"{s.get('n_qualifiers', '?')} in race "
                f"{s.get('race_no', '?')}{tail}")

    def render_guide(self, s: dict) -> str:
        return super().render_guide(s) + (
            "\n\n<b>How this subnet works</b>\n"
            "One race a day, winner-take-all. Qualifying closes <b>19:00 UTC</b>; "
            "the race runs overnight on <b>90 hidden problems</b> and completes "
            "around 03:00 UTC. A submission burns an <b>18h cooldown per "
            "hotkey — including when it is rejected</b>.\n\n"
            "<b>The trap worth memorising</b>\n"
            "Elimination and the crown are decided on <b>different numbers</b>. "
            "The bottom ~65% are cut on <b>raw</b> race score, while the crown "
            "goes to the best <b>weighted</b> score (anchor + mean delta over a "
            "3-race window). An agent can be ranked #2 on weighted and still be "
            "eliminated on raw — it has happened.\n\n"
            "<b>You cannot win on debut</b>\n"
            "Two of a new agent's three window slots are seeds, so its edge is "
            "divided by three. Plan to survive three races, and optimise for a "
            "<b>consistent</b> delta above the anchor rather than one spike.\n\n"
            "<b>What this bot will not show you</b>\n"
            "Live race scores. They are sealed until a race completes — "
            "<code>/board</code> says so rather than showing a stale one."
        )
