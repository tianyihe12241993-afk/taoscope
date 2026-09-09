"""SN49 Nepher Robotics — sim-to-real robotics policy tournaments on Isaac Lab.

Miners train navigation/locomotion policies locally, submit a signed agent zip,
and validators score it inside an ephemeral GPU sandbox. Winner takes all.

WHAT MAKES THIS SUBNET AWKWARD, and why the adapter is shaped the way it is:

  1. SEVERAL TOURNAMENTS RUN AT ONCE, each in a DIFFERENT stage. Three were live
     when this was written (contest / reward / completed), so there is no single
     "current round" and `/scores/leaderboard/active` answers 409 rather than
     picking one. Everything here is therefore keyed per tournament id.

  2. `status` IS NOT THE STATE. It reads "active" for every live tournament and
     "approved" for every finished one. The real state machine is `stage`:
     contest -> submit -> evaluation -> review -> reward. Diffing `status` would
     produce nothing; diffing `stage` is the whole point.

  3. SCORES CHANGE MEANING MIDWAY. During the contest the board is scored on the
     PUBLIC eval config; after the contest closes it is rescored privately, and
     only then is it the ranking that pays. The leaderboard says which you are
     looking at (`phase`, `is_final`) — so we carry that flag everywhere a score
     is shown, and alert when it flips. A contest-time 0.39 and a final 0.95 are
     not comparable numbers.

  4. THE LEADERBOARD SCORE FIELD IS `aggregated_score`, NOT `score`. An entry
     also carries a `score` inside each nested completed_evaluations item, so
     `.get("score")` on the row silently returns None and every rank renders as
     zero. Verified against the live API, not inferred.

  5. THE HISTORY AND THE LIVE LIST ARE DIFFERENT ENDPOINTS. `/tournaments/list`
     returns only completed+approved ones; anything in flight appears ONLY at
     `/tournaments/active/list`.

  6. THE SPEC LIES ABOUT AUTH. Most of these paths are marked as requiring a
     key in the published OpenAPI document, yet answer anonymously. They are
     read anonymously here on purpose — but `/weight-commits/latest` genuinely
     does 401, so do not assume the pattern holds for a new endpoint.

WHAT PAYS (nepher-subnet/docs/incentive_mechanism.md, read, not guessed):
  * every NON-reward tournament pays 1% of weight to its current preliminary
    leader, so `preliminary-leader` is an earning position, not a formality;
  * the reward-period tournament's approved winner takes all remaining weight;
  * anything unresolved burns on UID 0.
"""
from __future__ import annotations

import datetime as dt
import logging
import re

from ..base import (CompEvent, SubnetAdapter, changed, esc, fetch_json, num,
                    short, two_col)

log = logging.getLogger("taoscope.comp.sn49")

API = "https://tournament-api.nepher.ai"
DASH = "https://tournament.nepher.ai/tournaments"

# The agents endpoint caps a page at 100 and SILENTLY IGNORES a larger `limit`
# (verified live: `limit=500` came back with `limit: 100`). A single fetch would
# therefore quietly miss everything past the first page -- and our own hotkeys
# with it -- on any tournament with a field bigger than 100.
AGENT_PAGE = 100
MAX_AGENT_PAGES = 6

# stage -> (icon, what is actually happening, sort rank, severity when ENTERED)
#
# Severity is "what would I do differently the moment this happens":
# `submit` locks eligibility (last chance), `reward` moves all the weight.
# Evaluation and review are waiting rooms, so they arrive silently.
STAGES = {
    "contest":    ("🏁", "submissions open",     0, "info"),
    "submit":     ("🔒", "eligibility locking",  1, "warn"),
    "evaluation": ("⏳", "validators scoring",   2, "info"),
    "review":     ("🔍", "admin verifying",      3, "info"),
    "reward":     ("🏆", "paying the winner",    4, "critical"),
    "completed":  ("✅", "finished",             5, "info"),
}

# Countdown buckets. A raw "seconds remaining" changes every poll, so the BUCKET
# is what gets stored and diffed -- only crossing a threshold is allowed to
# speak. Ordered tightest-first.
BUCKETS = ((0, "closed"), (3600, "<1h"), (6 * 3600, "<6h"),
           (24 * 3600, "<24h"), (3 * 86400, "<3d"))
LOUD_BUCKETS = {"<1h": "critical", "<6h": "warn", "<24h": "warn"}


def _now() -> float:
    return dt.datetime.now(dt.timezone.utc).timestamp()


def _left(ts) -> float | None:
    try:
        return float(ts) - _now()
    except (TypeError, ValueError):
        return None


def _fmt(seconds) -> str:
    """Seconds -> '4d 13h' / '6h 12m' / '18m'. Readable at a glance on a phone."""
    if seconds is None:
        return "—"
    if seconds <= 0:
        return "closed"
    d, rem = divmod(int(seconds), 86400)
    h, rem = divmod(rem, 3600)
    m = rem // 60
    if d:
        return f"{d}d {h}h"
    if h:
        return f"{h}h {m}m"
    return f"{m}m"


def _bucket(seconds) -> str:
    if seconds is None:
        return ""
    for limit, name in BUCKETS:
        if seconds <= limit:
            return name
    return "open"


def _stamp(ts) -> str:
    try:
        return dt.datetime.fromtimestamp(float(ts), dt.timezone.utc).strftime(
            "%m-%d %H:%MZ")
    except (TypeError, ValueError, OSError):
        return "—"


def _pl(n, word: str) -> str:
    """'1 agent' / '77 agents' -- '1 agents' in an alert reads like a bug."""
    return f"{n} {word}" + ("" if n == 1 else "s")


def _slug(url) -> str:
    """'https://github.com/nepher-ai/eval-nav.git' -> 'nepher-ai/eval-nav'."""
    if not url or not isinstance(url, str):
        return ""
    m = re.search(r"github\.com/([^/]+/[^/#?\s]+)", url)
    if not m:
        return ""
    return m.group(1)[:-4] if m.group(1).endswith(".git") else m.group(1)


async def _our_hotkeys() -> dict[str, int] | None:
    """{hotkey: uid} for every SN49 neuron owned by a coldkey we registered.

    The backend already knows this: `my_coldkey` holds the operator's coldkeys
    and `neuron_live` is refreshed by the chain poller, so a hotkey appears here
    the moment it is registered and disappears when it is deregistered -- no
    file to maintain and no uid to go stale.

    Returns None (not {}) when the query fails, so the caller omits the key
    rather than reporting "you own nothing".
    """
    from ...db import pool
    try:
        rows = await pool().fetch(
            "SELECT n.uid, n.hotkey FROM my_coldkey m"
            "  JOIN neuron_live n ON n.coldkey = m.coldkey"
            " WHERE n.netuid = 49 AND n.hotkey IS NOT NULL")
    except Exception as exc:  # noqa: BLE001
        log.warning("SN49: could not read our hotkeys: %s", exc)
        return None
    return {r["hotkey"]: r["uid"] for r in rows}


async def _agents(tid: str) -> list[dict] | None:
    """Every agent in a tournament, paginated. None if the first page failed."""
    out: list[dict] = []
    for page in range(MAX_AGENT_PAGES):
        d = await fetch_json(f"{API}/api/v1/agents/list/public"
                             f"?tournament_id={tid}&limit={AGENT_PAGE}"
                             f"&offset={page * AGENT_PAGE}")
        if d is None:
            return out or None
        rows = d.get("agents") or []
        out += rows
        total = d.get("total")
        if len(rows) < AGENT_PAGE or (total is not None and len(out) >= total):
            return out
    # Never truncate silently: a capped sweep must say so, or a missing hotkey
    # reads as "we did not submit".
    log.warning("SN49 %s: stopped at %d agents (page cap)", tid[:8], len(out))
    return out


def _mine(agents: list[dict], ours: dict[str, int]) -> dict:
    """Our hotkeys' standing in one tournament, keyed by short hotkey.

    Two different facts, deliberately kept apart, because conflating them is the
    one mistake that makes this view lie:

      * `rank`/`score` -- our BEST scored agent so far.
      * `latest_*`     -- the agent that will ACTUALLY be scored, because only
        the latest upload per miner is evaluated. A miner sitting at #2 on an
        older agent is judged on their newest one, not on that #2.
    """
    out: dict = {}
    for a in agents:
        hk = a.get("miner_hotkey")
        if hk not in ours:
            continue
        e = out.setdefault(hk[:8], {"hk": hk, "uid": ours[hk], "n": 0,
                                    "rank": None, "score": None, "_blk": -1})
        e["n"] += 1
        rank = a.get("rank")
        if rank is not None and (e["rank"] is None or rank < e["rank"]):
            e["rank"], e["score"] = rank, a.get("score")
        blk = a.get("upload_block") or 0
        if blk >= e["_blk"]:
            e.update(_blk=blk,
                     latest_scored=a.get("score") is not None,
                     latest_rank=a.get("rank"),
                     latest_score=a.get("score"),
                     eligible=bool(a.get("is_eligible")),
                     status=a.get("status") or "",
                     sec=a.get("security_status") or "")
    for e in out.values():
        e.pop("_blk", None)
    return out


def _moved(was: dict, cur: dict, key: str) -> bool:
    """The nested-dict analogue of base.changed().

    True only when BOTH sides actually carry the key and it differs. A per
    tournament sub-fetch that failed omits its key, and without this guard that
    omission reads as "the winner was withdrawn" at 3am. Absence is not a
    change -- the same rule as changed(), one level down.
    """
    if key not in was or key not in cur:
        return False
    if was[key] is None or cur[key] is None:
        return False
    return was[key] != cur[key]


class SN49(SubnetAdapter):
    # ---- identity -----------------------------------------------------------
    netuid = 49
    slug = "nepher"
    label = "Nepher Robotics"

    # A contest runs ~12 days and the fastest-moving fact is a stage boundary
    # defined by block height, so 5 min is ample resolution for every alert
    # here, including the closing-window buckets.
    poll_seconds = 300

    # A commit to the eval repo changes how every agent is SCORED, and a commit
    # to the task repo changes what the robot is asked to do -- both land before
    # any dashboard or announcement says so. nepher-subnet carries the incentive
    # mechanism itself (weight split, eligibility, the tournament cycle).
    #
    # task_gh is per-tournament, so this static list goes stale the moment a new
    # task launches. snapshot() detects exactly that and alerts, rather than
    # letting the watcher quietly stop covering the live task.
    repos: list[str] = [
        "nepher-ai/nepher-subnet",
        "nepher-ai/eval-nav",
        "nepher-ai/task-humanoid-run-jump",
    ]

    links = {
        "dashboard": DASH,
        "api": API,
        "docs": "https://docs.nepher.ai",
    }

    alerts = {
        "new_tournament": "a new tournament opened — a fresh field and a fresh "
                          "winner-take-all prize",
        "stage": "a tournament changed stage (contest → submit → evaluation → "
                 "review → reward)",
        "deadline": "the submission deadline is close (<24h / <6h / <1h)",
        "score_final": "the board was rescored privately — the public ranking "
                       "you were reading is superseded",
        "winner": "a winner was approved and now takes all remaining weight",
        "prelim_leader": "the preliminary leader changed — that position earns "
                         "1% of weight while the tournament runs",
        "field": "the field grew past another 25 agents",
        "task_repo": "a live tournament uses a repo this bot is not watching",
        "our_agent": "one of OUR hotkeys uploaded, changed rank, gained or lost "
                     "eligibility, or was flagged by the security scan",
    }

    # ---- 1. COLLECT ---------------------------------------------------------
    async def snapshot(self) -> dict:
        s: dict = {}

        active = await fetch_json(f"{API}/api/v1/tournaments/active/list")
        if not active or active.get("tournaments") is None:
            return s                      # source down -> say nothing at all
        live = active["tournaments"]

        s["n_active"] = len(live)
        s["active_ids"] = sorted(t["id"] for t in live if t.get("id"))

        ours = await _our_hotkeys()
        if ours is not None:
            # NOT `ours`: that key name already means {ref: {stage, score...}}
            # elsewhere in this codebase, and the devtools render it that way.
            s["our_hotkeys"] = ours

        tours: dict = {}
        for t in live:
            if t.get("id"):
                tours[t["id"][:8]] = await self._tour(t, ours)
        s["tours"] = tours

        # Which repos the live tournaments are actually defined by, minus the
        # ones we already watch. Non-empty means the static `repos` list above
        # has drifted and the watcher is blind to the current task.
        # Only tournaments still IN PLAY count: a finished tournament's task
        # repo can no longer change anything we would do, and listing it makes
        # the warning permanent furniture instead of a signal that a NEW task
        # just launched.
        watched = {r.partition("@")[0].lower() for r in self.repos}
        seen = {_slug(t.get(k)) for t in live
                if t.get("stage") not in ("reward", "completed")
                for k in ("task_gh", "eval_gh")}
        s["unwatched_repos"] = sorted(r for r in seen if r and r.lower() not in watched)
        return s

    async def _tour(self, t: dict, ours: dict[str, int] | None = None) -> dict:
        """One tournament, flattened. Every optional key is omitted when absent."""
        tid = t["id"]
        st = t.get("statistics") or {}
        d: dict = {
            "id": tid,
            "task": t.get("task_name") or "",
            "title": t.get("title") or "",
            "stage": t.get("stage") or "",
            "ver": t.get("tournament_version"),
            "task_gh": _slug(t.get("task_gh")),
            "eval_gh": _slug(t.get("eval_gh")),
        }
        if t.get("current_eval_phase"):
            d["eval_phase"] = t["current_eval_phase"]

        for src, key in (("submit_window_start_time", "lock_ts"),
                         ("contest_end_time", "end_ts"),
                         ("evaluation_end_time", "eval_end_ts"),
                         ("reward_start_time", "reward_ts"),
                         ("reward_end_time", "reward_end_ts")):
            if t.get(src):
                d[key] = t[src]
        if d.get("end_ts"):
            d["bucket"] = _bucket(_left(d["end_ts"]))

        for src, key in (("agents_count", "agents"), ("participants_count", "miners"),
                         ("eligible_count", "eligible"),
                         ("validator_count", "validators"),
                         ("top_score", "top"), ("average_score", "avg"),
                         ("score_phase", "phase")):
            if st.get(src) is not None:
                d[key] = st[src]
        if d.get("agents") is not None:
            # Diffed instead of the raw count: 77 -> 78 is not news, and an
            # alert every poll is how a topic gets muted.
            d["agents_step"] = int(d["agents"]) // 25

        if t.get("winner_hotkey"):
            d["winner_hk"] = t["winner_hotkey"]
        if t.get("winner_score") is not None:
            d["winner_score"] = t["winner_score"]
        if t.get("winner_approved") is not None:
            d["winner_approved"] = bool(t["winner_approved"])

        # The 1%-of-weight position while the tournament runs. A null leader is
        # a real answer ("nobody leads yet"), not a failed fetch, so it is
        # recorded as "" rather than omitted.
        lead = await fetch_json(f"{API}/api/v1/tournaments/{tid}/preliminary-leader")
        if lead:
            d["lead_hk"] = lead.get("leader_hotkey") or ""
            if lead.get("leader_score") is not None:
                d["lead_score"] = lead["leader_score"]

        lb = await fetch_json(f"{API}/api/v1/scores/leaderboard/{tid}?limit=20")
        if lb and lb.get("entries") is not None:
            rows = lb["entries"]
            d["is_final"] = bool(lb.get("is_final"))
            d["lb_phase"] = lb.get("phase") or ""
            d["n_board"] = lb["total"] if lb.get("total") is not None else len(rows)
            d["board"] = [{
                "rank": r.get("rank"),
                "hk": r.get("miner_hotkey") or "",
                # `aggregated_score` -- NOT `score`. See the module docstring.
                "score": r.get("aggregated_score"),
                "evals": r.get("num_evaluations"),
            } for r in rows[:20]]

        if ours:
            ags = await _agents(tid)
            if ags is not None:
                d["mine"] = _mine(ags, ours)
        return d

    # ---- 2. DIFF ------------------------------------------------------------
    def diff(self, old: dict, new: dict) -> list[CompEvent]:
        out: list[CompEvent] = []
        if not old:
            return out                     # first sight is a baseline
        now_t = new.get("tours") or {}
        if not now_t:
            return out                     # nothing fetched -> nothing to say
        was_t = old.get("tours") or {}

        if changed(old, new, "active_ids"):
            known = set(old.get("active_ids") or [])
            for tid in (i for i in new["active_ids"] if i not in known):
                d = now_t.get(tid[:8]) or {}
                out.append(CompEvent(
                    kind="new_tournament", severity="critical", icon="🆕",
                    title=f"New SN49 tournament — {d.get('task') or tid[:8]}",
                    body=f"{esc(d.get('title') or '')}\n"
                         f"stage <b>{esc(d.get('stage') or '?')}</b> · deadline "
                         f"{_stamp(d.get('end_ts'))}\n"
                         f"<i>winner takes all remaining weight</i>",
                    dedup_key=tid,
                ))

        for ref, cur in now_t.items():
            was = was_t.get(ref)
            if not was:
                continue                   # covered by new_tournament above
            task = esc(cur.get("task") or ref)

            if _moved(was, cur, "stage"):
                icon, meaning, _, sev = STAGES.get(cur["stage"], ("•", "", 9, "info"))
                body = (f"{esc(was['stage'])} → <b>{esc(cur['stage'])}</b> — "
                        f"{meaning}")
                if cur["stage"] == "submit":
                    body += ("\n<i>eligibility snapshot locks: registered + "
                             "submitted, only your latest agent is scored</i>")
                elif cur["stage"] == "reward":
                    body += (f"\nwinner <code>{esc(short(cur.get('winner_hk')))}</code>"
                             f" · {num(cur.get('winner_score'), 4)}"
                             f"\n<i>all remaining weight goes here until "
                             f"{_stamp(cur.get('reward_end_ts'))}</i>")
                out.append(CompEvent(
                    kind="stage", severity=sev, icon=icon,
                    title=f"{task} → {cur['stage']}",
                    body=body, dedup_key=f"{ref}:{cur['stage']}",
                ))

            # Deadline. Only the tight buckets speak, and only while there is
            # still something to submit into.
            if (_moved(was, cur, "bucket") and cur["bucket"] in LOUD_BUCKETS
                    and cur.get("stage") in ("contest", "submit")):
                out.append(CompEvent(
                    kind="deadline", severity=LOUD_BUCKETS[cur["bucket"]], icon="⏳",
                    title=f"{task} closes {cur['bucket']}",
                    body=f"submissions end {_stamp(cur.get('end_ts'))} "
                         f"(in {_fmt(_left(cur.get('end_ts')))})\n"
                         f"field {_pl(cur.get('agents', 0), 'agent')} · "
                         f"{_pl(cur.get('miners', 0), 'miner')}",
                    dedup_key=f"{ref}:{cur['bucket']}",
                ))

            # The public board being replaced by the private one is the moment
            # the ranking starts meaning something.
            if _moved(was, cur, "is_final") and cur["is_final"]:
                # preliminary-leader goes null once a tournament finishes, so
                # the winner is the name to show; fall back while it is still
                # being approved.
                top_hk = cur.get("winner_hk") or cur.get("lead_hk")
                out.append(CompEvent(
                    kind="score_final", severity="warn", icon="🔐",
                    title=f"{task} rescored — final ranking",
                    body=f"top <code>{esc(short(top_hk))}</code> · "
                         f"<b>{num(cur.get('top'), 4)}</b>\n"
                         f"<i>the public-phase ranking is superseded</i>",
                    dedup_key=f"{ref}:final",
                ))

            if _moved(was, cur, "winner_approved") and cur["winner_approved"]:
                out.append(CompEvent(
                    kind="winner", severity="critical", icon="🏆",
                    title=f"{task} winner approved",
                    body=f"<code>{esc(short(cur.get('winner_hk')))}</code> · "
                         f"<b>{num(cur.get('winner_score'), 4)}</b>\n"
                         f"<i>takes all remaining weight this reward period</i>",
                    dedup_key=f"{ref}:{cur.get('winner_hk')}",
                ))

            # Silent by design: it churns, but it is the 1%-earning seat.
            if _moved(was, cur, "lead_hk") and cur["lead_hk"]:
                phase = "final" if cur.get("is_final") else "public"
                out.append(CompEvent(
                    kind="prelim_leader", severity="info", icon="👑",
                    title=f"{task} leader → {short(cur['lead_hk'])}",
                    body=f"<code>{esc(short(was.get('lead_hk')))}</code> → "
                         f"<code>{esc(short(cur['lead_hk']))}</code> at "
                         f"<b>{num(cur.get('lead_score'), 4)}</b> ({phase})\n"
                         f"<i>this seat earns 1% of weight while the "
                         f"tournament runs</i>",
                    dedup_key=f"{ref}:{cur['lead_hk']}",
                ))

            # ---- our own hotkeys ------------------------------------------
            # These are the only alerts about US, so they are never suppressed
            # into a summary: each hotkey speaks for itself.
            for hk8, m in (cur.get("mine") or {}).items():
                wm = (was.get("mine") or {}).get(hk8)
                if not wm:
                    continue
                who = f"uid {m.get('uid')} {short(m.get('hk'))}"

                if _moved(wm, m, "sec") and m["sec"] not in ("clean", ""):
                    out.append(CompEvent(
                        kind="our_agent", severity="critical", icon="🛡",
                        title=f"Our {who} flagged by security",
                        body=f"{task}\nsecurity <b>{esc(m['sec'])}</b>\n"
                             f"<i>a flagged agent can be disqualified</i>",
                        dedup_key=f"{ref}:{hk8}:sec:{m['sec']}",
                    ))

                if _moved(wm, m, "eligible") and cur.get("stage") != "contest":
                    gained = bool(m["eligible"])
                    out.append(CompEvent(
                        kind="our_agent",
                        severity="good" if gained else "warn",
                        icon="✅" if gained else "🚫",
                        title=f"Our {who} "
                              f"{'is now eligible' if gained else 'is NOT eligible'}",
                        body=f"{task}\n"
                             + ("<i>it will be evaluated</i>" if gained else
                                "<i>it will NOT be scored unless this is "
                                "fixed before the window closes</i>"),
                        dedup_key=f"{ref}:{hk8}:elig:{gained}",
                    ))

                if _moved(wm, m, "n") and m["n"] > wm["n"]:
                    out.append(CompEvent(
                        kind="our_agent", severity="good", icon="📤",
                        title=f"Our {who} uploaded an agent",
                        body=f"{task}\n{_pl(m['n'], 'agent')} total\n"
                             f"<i>only the latest is scored</i>",
                        dedup_key=f"{ref}:{hk8}:n:{m['n']}",
                    ))

                if _moved(wm, m, "rank"):
                    better = m["rank"] < wm["rank"]
                    out.append(CompEvent(
                        kind="our_agent", severity="info",
                        icon="📈" if better else "📉",
                        title=f"Our {who} best rank #{wm['rank']} → #{m['rank']}",
                        body=f"{task}\n{num(m.get('score'), 4)}"
                             + ("" if cur.get("is_final") else " <i>(public)</i>"),
                        dedup_key=f"{ref}:{hk8}:rank:{m['rank']}",
                    ))

            if _moved(was, cur, "agents_step") and cur["agents_step"] > was["agents_step"]:
                out.append(CompEvent(
                    kind="field", severity="info", icon="📥",
                    title=f"{task} field past {cur['agents_step'] * 25} agents",
                    body=f"{_pl(cur.get('agents', 0), 'agent')} · "
                         f"{_pl(cur.get('miners', 0), 'miner')}",
                    dedup_key=f"{ref}:{cur['agents_step']}",
                ))

        if changed(old, new, "unwatched_repos") and new["unwatched_repos"]:
            out.append(CompEvent(
                kind="task_repo", severity="warn", icon="📦",
                title="A live tournament uses an unwatched repo",
                body="\n".join(f"<code>{esc(r)}</code>" for r in new["unwatched_repos"])
                     + "\n<i>add it to SN49.repos so commits are alerted</i>",
                dedup_key=",".join(new["unwatched_repos"]),
            ))
        return out

    # ---- 3. RENDER ----------------------------------------------------------
    def _ordered(self, s: dict) -> list[dict]:
        """Live tournaments, most actionable first."""
        return sorted((s.get("tours") or {}).values(),
                      key=lambda d: (STAGES.get(d.get("stage"), ("", "", 9, ""))[2],
                                     d.get("task") or ""))

    def _select(self, s: dict, *, active_only: bool = False) -> list[dict]:
        """The tournaments this view should show.

        `_pick` is the topic's own narrowing, injected by the command layer;
        empty means "everything". `active_only` additionally drops finished
        tournaments -- what /mine wants, since a completed round is a result,
        not something you can still act on.
        """
        pick = set(s.get("_pick") or [])
        out = []
        for d in self._ordered(s):
            if active_only and d.get("stage") == "completed":
                continue
            if pick and (d.get("id") or "")[:8] not in pick:
                continue
            out.append(d)
        return out

    def pick_options(self, s: dict) -> list[dict]:
        opts = []
        for d in self._ordered(s):
            what, left = self._clock(d)
            note = d.get("stage") or ""
            if what and left:
                note += f" · {what} in {left}"
            opts.append({
                "key": (d.get("id") or "")[:8],
                "label": d.get("task") or (d.get("id") or "")[:8],
                "note": note,
                "mine": bool(d.get("mine")),
                "active": d.get("stage") != "completed",
            })
        return opts

    def _clock(self, d: dict) -> tuple[str, str]:
        """(what the next deadline IS, how long) — named for the stage it is in."""
        stage = d.get("stage")
        if stage in ("contest", "submit"):
            return "submissions close", _fmt(_left(d.get("end_ts")))
        if stage in ("evaluation", "review"):
            return "results due", _fmt(_left(d.get("eval_end_ts")))
        if stage == "reward":
            return "reward ends", _fmt(_left(d.get("reward_end_ts")))
        return "", ""

    def render_state(self, s: dict) -> str:
        """The high-level answer: what is happening on SN49 right now."""
        tours = self._select(s)
        if not tours:
            if self._ordered(s):
                return ("Your <code>/pick</code> selection matches nothing here.\n"
                        "<code>/pick all</code> to show everything again.")
            return (f"<b>SN{self.netuid} · {esc(self.label)}</b>\n\n"
                    "No live tournament data yet — <code>/poll</code> to force a "
                    "refresh.")
        paying = [d for d in tours if d.get("stage") == "reward"]
        L = [f"<b>SN{self.netuid} · {esc(self.label)}</b>",
             f"<i>{_pl(len(tours), 'tournament')} live"
             + (f" · {len(paying)} paying now" if paying else "") + "</i>"]

        for d in tours:
            icon, meaning, _, _ = STAGES.get(d.get("stage"), ("•", "", 9, ""))
            L.append("")
            L.append(f"{icon} <b>{esc(d.get('task') or d.get('id', '')[:8])}</b>"
                     + (f" <i>v{d['ver']}</i>" if d.get("ver") else ""))
            what, left = self._clock(d)
            head = meaning + (f" · {what} in <b>{left}</b>" if what and left else "")
            L.append(head)

            if d.get("stage") in ("reward", "completed") and d.get("winner_hk"):
                L.append(f"🏆 winner <code>{esc(short(d['winner_hk']))}</code> · "
                         f"<b>{num(d.get('winner_score'), 4)}</b>"
                         + ("" if d.get("winner_approved") else " <i>(pending)</i>"))
            elif d.get("lead_hk"):
                L.append(f"👑 leader <code>{esc(short(d['lead_hk']))}</code> · "
                         f"<b>{num(d.get('lead_score'), 4)}</b>")
            elif d.get("lead_hk") == "":
                L.append("👑 no leader yet")

            if d.get("agents") is not None:
                bits = [_pl(d["agents"], "agent")]
                if d.get("miners") is not None:
                    bits.append(_pl(d["miners"], "miner"))
                if d.get("eligible"):
                    bits.append(f"{d['eligible']} eligible")
                L.append("📥 " + " · ".join(bits))

            # Never let a public-phase number be mistaken for the real ranking.
            if d.get("is_final") is False:
                L.append("⚠️ <i>public-phase score — not the final ranking</i>")
        return "\n".join(L)

    def render_info(self, s: dict) -> str:
        """The rules — how this subnet actually pays."""
        L = [f"<b>SN{self.netuid} · {esc(self.label)} — the rules</b>", "",
             "<b>Tournament cycle</b>",
             "<code>contest → submit → evaluation → review → reward</code>",
             "• <b>contest</b> — train locally, submit a signed agent",
             "• <b>submit</b> — eligibility snapshot locks; you must be "
             "registered <i>and</i> submitted. Only your latest agent scores",
             "• <b>evaluation</b> — validators run it in an Isaac Lab sandbox; "
             "scores aggregate stake-weighted, ties broken by earliest upload",
             "• <b>review</b> — admin verifies the top agent",
             "• <b>reward</b> — validators point weight at the winner, hourly",
             "",
             "<b>Weight split</b>",
             "• every <b>non-reward</b> tournament pays <b>1%</b> to its current "
             "preliminary leader",
             "• the <b>reward</b> tournament's approved winner takes <b>all the "
             "rest</b>",
             "• unresolved — no winner, or winner not in the metagraph — "
             "<b>burns on UID 0</b>"]

        for d in self._ordered(s):
            L += ["", f"<b>{esc(d.get('task') or '?')}</b> · {esc(d.get('stage') or '?')}",
                  two_col([("lock", _stamp(d.get("lock_ts"))),
                           ("close", _stamp(d.get("end_ts"))),
                           ("scored", _stamp(d.get("eval_end_ts"))),
                           ("reward", _stamp(d.get("reward_ts")))], 20)]

        repos = s.get("_repos") or []
        if repos:
            L += ["", "<b>Watched repos</b>"]
            for r in repos:
                L.append(f"📦 <code>{esc(r['repo'])}</code> "
                         f"<code>{esc((r.get('sha') or '')[:10])}</code> "
                         f"{esc((r.get('subject') or '')[:48])}")
        if s.get("unwatched_repos"):
            L += ["", "⚠️ <b>In play but NOT watched</b>"]
            L += [f"<code>{esc(r)}</code>" for r in s["unwatched_repos"]]
        L += ["", f'<a href="{DASH}">dashboard</a>']
        return "\n".join(L)

    def render_board(self, s: dict, limit: int = 10) -> str:
        """Standings, with our own rows highlighted.

        Telegram's HTML has no colour markup at all -- the tag list is
        <b> <i> <u> <s> <code> <pre> <a> <blockquote> and nothing else. The one
        way to get real colour is a syntax-highlighted code block, so the table
        is emitted as `language-diff`: clients that highlight paint a `+` line
        green, which makes our rows stand out from the field.

        Clients that do NOT highlight fall back to plain monospace, so the `+`
        and the trailing uid have to carry the meaning on their own -- the
        colour is a bonus, never the only signal.
        """
        tours = [d for d in self._select(s) if d.get("board")]
        if not tours:
            return "No published board yet."
        d = tours[0]
        ours = set(s.get("our_hotkeys") or {})
        uids = dict(s.get("our_hotkeys") or {})

        rows = ["  #   score    miner"]
        hits = 0
        for r in d["board"][:limit]:
            hk = r.get("hk") or ""
            mine = hk in ours
            hits += mine
            rows.append(f"{'+' if mine else ' '} {str(r.get('rank') or '-'):<3} "
                        f"{num(r.get('score'), 4):>7}  {short(hk, 6, 4)}"
                        + (f"  u{uids[hk]}" if mine else ""))

        head = (f"<b>{esc(d.get('task') or '?')}</b> · "
                f"{_pl(d.get('n_board', 0), 'agent')} scored"
                f"\nphase <b>{esc(d.get('lb_phase') or '?')}</b>"
                + ("" if d.get("is_final") else " <i>(not final)</i>"))
        body = ('<pre><code class="language-diff">' + "\n".join(rows)
                + "</code></pre>")
        tail = (f"\n<i>+ = ours ({hits} in the top {limit})</i>" if hits else
                f"\n<i>none of our {len(ours)} hotkeys are in the top {limit}</i>")
        if not d.get("is_final"):
            tail += ("\n<i>one miner can hold several rows — only the latest "
                     "agent each is scored for the final ranking</i>")
        others = [x for x in tours[1:] if x.get("board")]
        if others:
            tail += "\n\n<i>also live: " + " · ".join(
                f"{esc(x.get('task') or '?')} "
                f"{num((x.get('board') or [{}])[0].get('score'), 3)}"
                for x in others) + "</i>"
        return head + "\n" + body + tail

    def render_me(self, s: dict) -> str:
        """Our hotkeys' status — what /mine answers."""
        ours = s.get("our_hotkeys")
        if ours is None:
            return "Could not read our hotkeys from the backend just now."
        if not ours:
            return ("No SN49 hotkey is registered to a coldkey we track.\n"
                    "Add the coldkey in the web UI and it appears here.")

        L = [f"<b>Our SN49 hotkeys</b> — {len(ours)} registered", ""]
        submitted: set[str] = set()

        active = self._select(s, active_only=True)
        if not active:
            return ("No <b>active</b> tournament to report on right now"
                    + (" for your <code>/pick</code> selection."
                       if s.get("_pick") else ".")
                    + "\n<i>Finished rounds are in </i><code>/board</code><i>.</i>")
        for d in active:
            mine = d.get("mine")
            if not mine:
                continue
            submitted |= {m["hk"] for m in mine.values() if m.get("hk")}
            icon, _, _, _ = STAGES.get(d.get("stage"), ("•", "", 9, ""))
            what, left = self._clock(d)
            L.append(f"{icon} <b>{esc(d.get('task') or '?')}</b> · "
                     f"{esc(d.get('stage') or '?')}"
                     + (f" · {what} in {left}" if what and left else ""))

            for m in sorted(mine.values(), key=lambda x: (x.get("rank") or 9999,
                                                          x.get("uid") or 0)):
                L.append(f"<b>uid {m.get('uid')}</b> <code>"
                         f"{esc(short(m.get('hk')))}</code> · "
                         f"{_pl(m.get('n', 0), 'agent')}")
                tag = "final" if d.get("is_final") else "public"
                if m.get("rank") is not None:
                    L.append(f"   best <b>#{m['rank']}</b> · {num(m.get('score'), 4)} "
                             f"<i>({tag})</i>")
                else:
                    L.append("   <i>no scored agent yet</i>")
                # The number that will actually decide this tournament for us.
                if m.get("latest_rank") is not None:
                    L.append(f"   <b>counts: #{m['latest_rank']}</b> · "
                             f"{num(m.get('latest_score'), 4)} "
                             f"<i>(latest — the only one scored)</i>")

                # Only the latest upload is evaluated, so a good older score is
                # not what we will be judged on. Say so explicitly.
                bits = []
                if m.get("status"):
                    bits.append(esc(m["status"]))
                bits.append("scored" if m.get("latest_scored") else "not scored yet")
                # The eligible flag is only populated when the submit window
                # snapshots the metagraph. During the contest it is False for
                # EVERY miner, so rendering a bare "not eligible" would cry
                # wolf at every hotkey for the whole contest.
                if d.get("stage") == "contest":
                    bits.append("eligibility locks " + _stamp(d.get("lock_ts")))
                else:
                    bits.append("eligible ✅" if m.get("eligible")
                                else "<b>NOT eligible</b> ❌")
                L.append(f"   latest: {' · '.join(bits)}")
                if m.get("sec") and m["sec"] != "clean":
                    L.append(f"   ⚠️ security <b>{esc(m['sec'])}</b>")
                # Only the latest upload is evaluated, so a better older agent
                # is not worth anything -- this is the trap worth shouting about.
                if m.get("rank") is not None and not m.get("latest_scored"):
                    L.append("   ⚠️ <i>that best score is an OLDER agent — only "
                             "your latest upload gets scored</i>")
                elif (m.get("latest_rank") is not None and m.get("rank") is not None
                        and m["latest_rank"] > m["rank"]):
                    L.append(f"   ⚠️ <i>your latest agent ranks WORSE than your "
                             f"best (#{m['latest_rank']} vs #{m['rank']}) — the "
                             f"latest is the one that counts</i>")
            L.append("")

        idle = sorted((uid for hk, uid in ours.items() if hk not in submitted))
        if idle:
            L.append(f"💤 <b>no agent submitted</b> ({len(idle)}): "
                     + ", ".join(f"uid {u}" for u in idle))
        if not submitted:
            L.append("<i>Nothing of ours is on any live board.</i>")
        return "\n".join(L).rstrip()

    def render_guide(self, s: dict) -> str:
        return super().render_guide(s) + (
            "\n\n<b>How SN49 works</b>\n"
            "Robotics policy tournaments. You train a policy locally in Isaac "
            "Lab, submit a signed agent zip, and validators score it in an "
            "ephemeral GPU sandbox. <b>Winner takes all.</b>\n\n"
            "<b>Several tournaments run at once</b>, each at a different point "
            "in <code>contest → submit → evaluation → review → reward</code>. "
            "Every non-reward tournament pays <b>1%</b> of weight to its "
            "preliminary leader; the reward tournament's winner takes all the "
            "rest; anything unresolved burns on UID 0.\n\n"
            "<b>Two numbers that look alike and are not</b>\n"
            "During the contest the board is scored on the <i>public</i> eval "
            "config. After it closes everything is rescored <i>privately</i>, "
            "and only that ranking pays. <code>/state</code> marks a "
            "public-phase score explicitly — never compare one against a final."
        )

    def render_digest(self, s: dict) -> str:
        tours = self._ordered(s)
        if not tours:
            return f"<b>SN{self.netuid}</b> {esc(self.label)} — no data yet"
        d = tours[0]
        what, left = self._clock(d)
        return (f"<b>SN{self.netuid}</b> {esc(self.label)} — "
                f"{len(tours)} live · <b>{esc(d.get('task') or '?')}</b> "
                f"{esc(d.get('stage') or '?')}"
                + (f" · {what} in {left}" if what and left else ""))
