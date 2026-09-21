"""SN62 — Ridges. Coding agents scored on a fixed problem set; continuous queue.

WHAT MAKES THIS SUBNET AWKWARD, and why the adapter is shaped the way it is:

  * There is NO round and NO submission window. Agents are uploaded whenever,
    and each one walks a linear pipeline:

        pre_screening -> screener_1 -> screener_2 -> validator -> approval

    So the countdown that dominates most adapters does not exist here. The
    time-critical fact is instead **the qualify bar**, which moves the moment
    anyone lands an improvement.

  * Emissions are NOT winner-take-all and NOT rank-based (`utils/incentives.py`).
    To bank anything an agent must beat the CURRENT LEADER by one of two routes:

        performance:  (you - leader) / leader          >= perf_threshold (0.03)
        cost:         score >= leader AND
                      (leader_cost - your_cost)/leader_cost >= cost_threshold (0.06)

    On qualifying it banks `(perf_units + cost_units) * time_multiplier`, which
    then decays with a 336h half-life. So you do not need rank 1 to earn — you
    need to keep LANDING improvements, and each one pays for ~2 weeks.

  * `time_multiplier = 1 + sqrt(hours_since_the_last_approval / 12)`. **A stall
    is worth money**: 12h idle = 2.0x, 48h = 3.0x, 7d = 4.7x for whoever breaks
    it. It resets to 1.0 the instant anyone is approved. That makes "nobody has
    been approved for N hours" one of the few genuinely actionable facts here,
    and "someone was just approved" the alert that cancels a plan.

  * Every upload BURNS alpha (`/upload/eval-pricing`) and is never refunded, so
    the price is tracked as a first-class field: it is the per-attempt cost of
    being wrong.

  * `finished` does NOT mean earning. An agent can be scored and still sit at
    `didnt_qualify` — evaluated, below the bar, paid nothing. The renderers say
    so rather than passing the platform's word through.

Cloudflare blocks www.ridges.ai / api.ridges.ai from our IP; the identical read
API is served unauthenticated by agent-upload.ridges.ai, which is what API
points at. `/evaluation-sets/{id}/overview` is blocked even there — do not add it.

Identity comes from the CHAIN, not from a hand-maintained list: our coldkeys
(`my_coldkey`, or `SN62_COLDKEYS`) -> every hotkey they have registered on
netuid 62 (`neuron_live`) -> every agent those hotkeys uploaded
(`/retrieval/agents-by-coldkey`, one request per coldkey for all of them). A
registration made five minutes ago is tracked with nothing typed in, and a
recycled uid can never make us report a stranger's agent as ours.
"""
from __future__ import annotations

import asyncio
import datetime as dt
import logging
import math
import os
import time

import httpx

from ..base import (CompEvent, SubnetAdapter, changed, diff_block, esc, num,
                    pct, short, two_col)

log = logging.getLogger("taoscope.comp.sn62")

API = "https://agent-upload.ridges.ai"

# The read API rate-limits HARD and says nothing about it: three requests in a
# burst is enough to get a 429, there is no Retry-After header and no
# x-ratelimit-* to read, and even 2s apart a request 429s roughly half the time.
# Measured 2026-08-21; refresh.sh has always slept 2s between calls for the same
# reason.
#
# This matters more than it looks. `base.fetch_json` maps every non-200 to None,
# and an adapter that omits a key on None -- which is the rule -- would go
# silently blind on whichever endpoints happened to land inside the throttle.
# The first version of this adapter did exactly that: the smoke test came back
# ALL CHECKS PASSED with the leader, the funnel, the upload price and the whole
# board simply missing from the snapshot, because they were requests 4 through 8
# of a burst of 8.
#
# So every call is spaced, and a 429 is retried rather than treated as an
# outage. A real failure still returns None and still omits its key.
_MIN_INTERVAL_S = 2.5
_RETRY_SLEEP_S = (3.0, 6.0, 10.0)
_last_call = 0.0
_pace = asyncio.Lock()


async def _get(path: str, *, timeout: float = 30.0):
    """Paced, 429-aware GET against the Ridges read API. None = real failure.

    The lock serialises this adapter's own calls: `snapshot()` makes several and
    the pacing is worthless if they overlap. It is not shared with other
    adapters because the throttle is per-host, and no other subnet uses this one.
    """
    global _last_call
    for i, backoff in enumerate((0.0,) + _RETRY_SLEEP_S):
        if backoff:
            await asyncio.sleep(backoff)
        async with _pace:
            gap = _MIN_INTERVAL_S - (time.monotonic() - _last_call)
            if gap > 0:
                await asyncio.sleep(gap)
            _last_call = time.monotonic()
            try:
                async with httpx.AsyncClient(timeout=timeout,
                                             follow_redirects=True) as cx:
                    r = await cx.get(f"{API}/{path.lstrip('/')}",
                                     headers={"accept": "application/json"})
            except Exception as exc:  # noqa: BLE001
                log.debug("sn62 %s failed: %s", path, exc)
                continue
        if r.status_code == 200:
            try:
                return r.json()
            except ValueError:
                return None
        if r.status_code != 429:
            log.debug("sn62 %s -> HTTP %s", path, r.status_code)
            return None
        log.debug("sn62 %s throttled (attempt %d)", path, i + 1)
    log.info("sn62 %s still throttled after %d attempts", path,
             len(_RETRY_SLEEP_S) + 1)
    return None

# The repo that defines the rules. `ridgesai/ridges-bench` carries the verifier
# gates and is the natural second watch, but the GitHub watcher is already over
# its unauthenticated 60/hour budget at twelve repos (see comp/github.py) and
# every repo costs 6 requests an hour whether or not it moved. Add it here once
# TAOSCOPE_GITHUB_TOKEN is set, not before.
RULES_REPO = "ridgesai/ridges"

# Paths in RULES_REPO whose movement changes what a submission is judged by, and
# the one-line reason each matters. A commit that touches none of these gets the
# framework's generic "repo moved" alert and nothing more; one that touches any
# of them gets a second message naming what moved. Prefix match, longest first.
HOT_PATHS: list[tuple[str, str]] = [
    ("utils/incentives.py",     "the reward formula — qualify thresholds, decay, time multiplier"),
    ("api/incentives.py",       "the reward formula, platform side"),
    ("inference_gateway/",      "the model allowlist and the per-run USD budget"),
    ("models/openrouter.py",    "the model allowlist"),
    ("ridges_harbor/",          "the task harness — how a patch is applied and gated"),
    ("validator/",              "how an agent is run and scored"),
    ("execution/",              "the sandbox the agent runs in"),
    ("miners/",                 "the upload CLI and the in-sandbox library set"),
    ("api/",                    "platform routes — thresholds, queue, pricing"),
]

# Pipeline status -> (ordinal, plain-English meaning, terminal?)
# The platform's vocabulary is not self-explanatory: `finished` means evaluated,
# NOT earning, and `didnt_qualify` is a scored agent that banked nothing.
STAGES: dict[str, tuple[int, str, bool]] = {
    "pre_screening":            (1, "pre-screen (LLM hardcoding judge)", False),
    "pre_screening_needs_review": (1, "pre-screen — held for review", False),
    "failed_pre_screening":     (1, "REJECTED by the hardcoding judge", True),
    "screening_1":              (2, "screener 1 running", False),
    "failed_screening_1":       (2, "failed screener 1 (below threshold)", True),
    "screening_2":              (3, "screener 2 running", False),
    "failed_screening_2":       (3, "failed screener 2 (below threshold)", True),
    "evaluating":               (4, "validators running", False),
    "finished":                 (5, "scored — not yet an approval", False),
    "under_review":             (5, "approval review", False),
    "didnt_qualify":            (5, "scored but BELOW the bar — earns nothing", True),
    "rejected":                 (5, "rejected at approval review", True),
    "cancelled":                (5, "cancelled (superseded by a later version)", True),
    "approved":                 (6, "APPROVED — banking emission", False),
    "baseline":                 (6, "platform baseline agent", False),
}

# time_multiplier marks worth waking up for. tm = 1 + sqrt(hours/12), so these
# are 12h / 27h / 48h / 108h of nobody landing anything.
TM_MARKS = (2.0, 2.5, 3.0, 4.0)

def _stage_meaning(stage: str | None) -> str:
    return (STAGES.get(stage or "") or (0, stage or "unknown", False))[1]


def _is_terminal(stage: str | None) -> bool:
    return (STAGES.get(stage or "") or (0, "", False))[2]


def _hours_since(iso: str | None) -> float | None:
    if not iso:
        return None
    try:
        when = dt.datetime.fromisoformat(str(iso).replace("Z", "+00:00"))
    except ValueError:
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=dt.timezone.utc)
    return max(0.0, (dt.datetime.now(dt.timezone.utc) - when).total_seconds() / 3600.0)


def _dur(hours: float | None) -> str:
    if hours is None:
        return "—"
    if hours < 1:
        return f"{int(hours * 60)}m"
    if hours < 48:
        return f"{hours:.1f}h"
    return f"{hours / 24:.1f}d"


def _usd(v, d: int = 4) -> str:
    try:
        return f"${float(v):.{d}f}"
    except (TypeError, ValueError):
        return "—"


class SN62(SubnetAdapter):
    # ---- identity -----------------------------------------------------------
    netuid = 62
    # our own deregistration alert is richer than the generic one
    covers = frozenset({"dereg"})
    slug = "ridges"
    label = "Ridges"

    # There is no window to miss, but the qualify bar can move at any moment and
    # an upload decided against a stale bar is alpha burned for nothing. 5 min.
    poll_seconds = 300

    repos: list[str] = [RULES_REPO]

    links = {
        "dashboard": "https://www.ridges.ai",
        "api": API,
        "repo": f"https://github.com/{RULES_REPO}",
    }

    alerts = {
        "rules": "a new evaluation set opened, or a threshold in the scoring "
                 "rules moved — every plan built against the old one is stale",
        "king_change": "the leader changed, which moves the bar every upload "
                       "is judged against",
        "bar": "the qualify bar moved (leader score or leader cost)",
        "approved": "someone banked emission — the stall multiplier just reset "
                    "to 1.0x and the bar went up",
        "stall": "nobody has been approved for long enough that the payout "
                 "multiplier for breaking the stall crossed a mark",
        "price": "the alpha burned per upload attempt changed",
        "threat": "a rival reached the validator stage — the bar may move "
                  "within the hour",
        "field": "the field grew — more agents in the pipeline",
        "weights": "the set of hotkeys the chain is actually paying changed",
        "repo_impact": "a commit touched a file that defines the rules, not "
                       "just the platform",
        "our_run": "one of our agents was accepted or changed pipeline stage",
        "dereg": "a hotkey of ours left the metagraph — whatever it submitted "
                 "still scores and earns nothing",
    }

    def __init__(self) -> None:
        # set_id -> the problem breakdown for that set. Refetched only when the
        # set changes, which is the only time it can change.
        self._problems_for: tuple[int | None, dict] = (None, {})
        # The heavy leaderboard, refetched only when the cheap set summary says
        # the field actually moved. `_board_key` is that summary's fingerprint.
        self._board: list[dict] = []
        self._board_key: tuple | None = None
        self._board_at: float = 0.0
        # Last RULES_REPO sha we have already explained. None = not yet seen,
        # so the first sight is a baseline and costs no GitHub request.
        self._sha_seen: str | None = None
        # key -> (fetched_at, value) for the slow-moving sources. See _slow().
        self._cache: dict[str, tuple[float, dict]] = {}
        # Last upload quote worth announcing. See PRICE_BAND.
        self._price_ref: float | None = None

    # The rules, the upload price and the chain weights change on the order of
    # days, and the API throttles at roughly one request every few seconds. So
    # they are refreshed on their own cadence and served from cache in between,
    # which keeps a normal poll down to the three calls that actually move.
    SLOW_TTL_S = 1800

    async def _slow(self, key: str, fn) -> dict:
        """Cached slow-moving source. A failed refresh keeps the last good value.

        Carrying forward is correct HERE and only here: these are rules, not
        observations, so the last value we successfully read is still the rule
        until we read a different one. Blanking them on a throttled request
        would delete the thresholds from /state and, worse, make the NEXT
        successful read look like the rules had just changed.
        """
        at, val = self._cache.get(key, (0.0, {}))
        if val and time.time() - at < self.SLOW_TTL_S:
            return dict(val)
        fresh = await fn()
        if fresh:
            self._cache[key] = (time.time(), fresh)
            return dict(fresh)
        return dict(val)

    # ---- 1. COLLECT ---------------------------------------------------------
    async def snapshot(self) -> dict:
        s: dict = {}

        info = await _get("scoring/latest-set-info")
        set_id = None
        if info and info.get("latest_set_id") is not None:
            set_id = int(info["latest_set_id"])
            s["comp_id"] = set_id
            s["set_opened"] = info.get("latest_set_created_at")

        s.update(await self._bar())
        s.update(await self._slow("thresholds", self._thresholds))
        s.update(await self._slow("price", self._price))
        s.update(await self._slow("weights", self._weights))

        if set_id is not None:
            s.update(await self._set_detail(set_id))
            s.update(await self._problems(set_id))
            board = await self._maybe_board(set_id, s)
            if board:
                s["board"] = board[:20]
                s["n_scored"] = len([r for r in board if r.get("score") is not None])
                # Only hotkeys that actually SCORED. The full roster churns by
                # dozens an hour (a third of uploads are rejected by the
                # hardcoding judge alone), and none of that churn is news.
                s["scored_hk"] = sorted({r["hk"] for r in board
                                         if r.get("hk") and r.get("score") is not None})
                # Rivals the validators are running right now: the only agents
                # that can move the bar in the next hour.
                s["evaluating"] = [
                    {"name": r.get("name"), "hk": r.get("hk"), "ver": r.get("ver")}
                    for r in board if r.get("stage") == "evaluating"][:10]
                s["n_evaluating"] = len(s["evaluating"])

        s.update(await self._repo_impact())
        s["ours"] = await self._ours(set_id)
        s.update(await self._mine_meta(s["ours"]))
        return s

    # -- the bar ---------------------------------------------------------------
    async def _bar(self) -> dict:
        """The two numbers an upload has to beat, and what a stall is worth.

        This is the whole subnet in one endpoint: the leader's score and cost,
        the two qualify thresholds, and the live time multiplier.
        """
        ns = await _get("retrieval/network-statistics")
        if not ns:
            return {}
        out: dict = {}
        top, cost = ns.get("top_score"), ns.get("top_cost")
        perf_t, cost_t = ns.get("perf_threshold"), ns.get("cost_threshold")
        if top is not None:
            out["top_score"] = top
        if cost is not None:
            out["top_cost"] = cost
        if perf_t is not None:
            out["perf_threshold"] = perf_t
        if cost_t is not None:
            out["cost_threshold"] = cost_t
        if top is not None and perf_t is not None:
            out["bar_score"] = float(top) * (1.0 + float(perf_t))
        if cost is not None and cost_t is not None:
            out["bar_cost"] = float(cost) * (1.0 - float(cost_t))

        if ns.get("last_approval"):
            out["last_approval"] = ns["last_approval"]
        tm = ns.get("time_multiplier")
        if tm is not None:
            out["time_multiplier"] = tm
            # Bucketed. tm rises continuously, so diffing it raw would fire an
            # event every single poll and say nothing.
            marks = [m for m in TM_MARKS if float(tm) >= m]
            out["tm_mark"] = max(marks) if marks else 0.0
        return out

    async def _thresholds(self) -> dict:
        sc = await _get("scoring/screener-info")
        if not sc:
            return {}
        out = {}
        for src, dst in (("screener_1_threshold", "scr1_bar"),
                         ("screener_2_threshold", "scr2_bar"),
                         ("prune_threshold", "prune_bar"),
                         ("screener_1_average_score", "scr1_avg"),
                         ("screener_2_average_score", "scr2_avg"),
                         ("validator_average_score", "val_avg")):
            if sc.get(src) is not None:
                out[dst] = sc[src]
        # Queue waits move every poll and no decision turns on a 30-second
        # change, so they are rendered but never diffed.
        for src, dst in (("screener_1_average_wait_time", "scr1_wait"),
                         ("screener_2_average_wait_time", "scr2_wait"),
                         ("validator_average_wait_time", "val_wait")):
            if sc.get(src) is not None:
                out[dst] = sc[src]
        return out

    # A move in the upload burn only matters if it is big enough to change a
    # budget decision. Measured 2026-08-21 the quote fell 2.1172 -> 2.0637 alpha
    # in 40 minutes on its own: the platform prices the upload in USD and
    # converts at the live alpha price, so the alpha figure drifts continuously.
    # Diffed raw it would have fired every poll forever. 10% is the band.
    PRICE_BAND = 0.10

    async def _price(self) -> dict:
        """Alpha burned per upload attempt. Burned, never refunded.

        `upload_alpha` is the live quote, for display. `upload_ref` is the last
        quote big enough to be worth announcing, and is the only one diffed --
        hysteresis rather than rounding, so a price sitting on a bucket boundary
        cannot flap an alert back and forth.
        """
        pr = await _get("upload/eval-pricing")
        if not pr or pr.get("amount_alpha_rao") is None:
            return {}
        alpha = float(pr["amount_alpha_rao"]) / 1e9
        ref = self._price_ref
        if not ref or abs(alpha - ref) / ref >= self.PRICE_BAND:
            self._price_ref = alpha         # first sight re-baselines silently
            ref = alpha
        return {"upload_alpha": alpha, "upload_ref": ref}

    async def _weights(self) -> dict:
        """Who the chain is actually paying, straight from the platform."""
        w = await _get("scoring/weights")
        if not isinstance(w, dict) or not w:
            return {}
        rows = sorted(w.items(), key=lambda kv: -(kv[1] or 0))
        return {
            "weights": [{"hk": hk, "w": val} for hk, val in rows[:8]],
            "earning_hk": sorted(w.keys()),
            "n_earning": len(w),
        }

    async def _set_detail(self, set_id: int) -> dict:
        """The dashboard's own summary — 1 KB, and it carries the whole funnel."""
        d = await _get(f"evaluation-sets/{set_id}")
        if not isinstance(d, dict) or d.get("id") is None:
            return {}
        out: dict = {"comp_name": d.get("competition_name"),
                     "set_started": d.get("competition_start_date"),
                     "set_ended": d.get("competition_end_date")}
        sub = d.get("submissions") or {}
        for src, dst in (("total_agents", "n_agents"), ("unique_miners", "n_miners"),
                         ("approved_emission_count", "n_approved"),
                         ("hardcoded_rejection_rate", "hardcode_rate")):
            if sub.get(src) is not None:
                out[dst] = sub[src]
        stages = sub.get("pipeline") or []
        if stages:
            out["funnel"] = [{"stage": r.get("stage"), "n": r.get("count"),
                              "rate": r.get("pass_rate")} for r in stages]
        sc = d.get("scores") or {}
        if sc.get("best") is not None:
            out["best_score"] = sc["best"]
        if sc.get("average") is not None:
            out["avg_score"] = sc["average"]
        top = d.get("top_agent") or {}
        if top.get("agent_id"):
            out["king_id"] = top["agent_id"]
            out["king_name"] = top.get("name")
            out["king_ver"] = top.get("version_num")
            out["king_score"] = top.get("final_score")
        eff = d.get("efficiency") or {}
        for src, dst in (("average_agent_cost_usd", "avg_cost"),
                         ("average_agent_runtime_seconds", "avg_runtime")):
            if eff.get(src) is not None:
                out[dst] = eff[src]
        prev = d.get("vs_previous_set") or {}
        if prev.get("top_score_delta") is not None:
            out["vs_prev_delta"] = prev["top_score_delta"]
        return out

    async def _problems(self, set_id: int) -> dict:
        """The shape of the live problem set. Refetched only when the set moves.

        Worth 80 KB exactly once per set, because a new set is the moment every
        piece of intel we hold goes stale and the first question is always "what
        is in it": how many validator problems (the score's denominator), how
        hard, and which benchmark family.
        """
        cached_id, cached = self._problems_for
        if cached_id == set_id and cached:
            return dict(cached)
        rows = await _get("evaluation-sets/all-latest-set-problems",
                                timeout=45.0)
        if not isinstance(rows, list) or not rows:
            return dict(cached) if cached_id == set_id else {}
        groups: dict[str, int] = {}
        diffs: dict[str, int] = {}
        fams: dict[str, int] = {}
        for r in rows:
            groups[str(r.get("set_group"))] = groups.get(str(r.get("set_group")), 0) + 1
            spec = r.get("execution_spec") or {}
            d = str(spec.get("problem_difficulty") or "?")
            diffs[d] = diffs.get(d, 0) + 1
            f = str(r.get("benchmark_family") or "?")
            fams[f] = fams.get(f, 0) + 1
        out = {
            "n_problems": len(rows),
            # The score's denominator: final_score = solved / validator problems.
            "n_validator_problems": groups.get("validator"),
            "groups": dict(sorted(groups.items())),
            "difficulty": dict(sorted(diffs.items())),
            "family": max(fams, key=lambda k: fams[k]) if fams else None,
        }
        self._problems_for = (set_id, out)
        return dict(out)

    async def _maybe_board(self, set_id: int, s: dict) -> list[dict]:
        """The leaderboard, but only when the cheap summary says it moved.

        The board is 220 KB and grows with the set; the summary that tells us
        whether anything in it changed is 1 KB. So fetch it when the agent
        count, the best score or the approval count moves — and at most every
        30 min regardless, so a quiet field still refreshes cost and stage.

        A cached board is carried forward, never omitted: it is positively
        unchanged, not unknown.
        """
        key = (s.get("n_agents"), s.get("best_score"), s.get("n_approved"),
               s.get("king_id"))
        stale = time.time() - self._board_at > 1800
        if self._board and key == self._board_key and not stale:
            return self._board

        rows = await _get(f"evaluation-sets/{set_id}/leaderboard",
                                timeout=45.0)
        if not isinstance(rows, list) or not rows:
            return self._board          # keep the last good one; never blank it

        board = []
        for r in rows:
            cs = r.get("competition_state") or {}
            board.append({
                "rank": cs.get("rank"),
                "id": r.get("agent_id"),
                "hk": r.get("miner_hotkey") or "",
                "name": r.get("name") or "",
                "ver": r.get("version_num"),
                # competition_state.status is the richer of the two status
                # fields: it is the only one that distinguishes an approved
                # agent from one that merely finished.
                "stage": cs.get("status") or r.get("status"),
                "score": cs.get("final_score"),
                "cost": cs.get("average_cost_usd"),
                "runtime": cs.get("average_runtime_seconds"),
                "approved": bool(cs.get("approved")),
                "banked": cs.get("initial_reward_score"),
            })
        board.sort(key=lambda r: (r["rank"] if r.get("rank") is not None else 9999,
                                  -(r.get("score") or 0)))
        self._board, self._board_key, self._board_at = board, key, time.time()
        return board

    # -- our own agents --------------------------------------------------------
    #
    # Identity comes from the CHAIN, not from a list anybody has to maintain:
    # our coldkeys, then every hotkey those coldkeys have registered on netuid
    # 62, then every agent those hotkeys have uploaded. That is the whole of
    # "mine", it survives a hotkey being recycled, and a registration made five
    # minutes ago is tracked with no code change and nothing typed in.
    #
    # It also costs less than the watchlist it replaces:
    # `/retrieval/agents-by-coldkey` returns {hotkey: [agent, ...]} for EVERY
    # hotkey under a coldkey in ONE request, where per-hotkey lookups would be
    # 23 of them against an API that throttles at one every few seconds.

    async def _coldkeys(self) -> list[str]:
        """Our coldkeys. The `my_coldkey` table is the app's own answer already.

        `SN62_COLDKEYS` (comma-separated) overrides it, so the adapter still
        works in a deployment where nobody has filled that table in.
        """
        env = os.getenv("SN62_COLDKEYS", "").strip()
        if env:
            return [c.strip() for c in env.split(",") if c.strip()]
        from ...db import pool
        rows = await pool().fetch("SELECT DISTINCT coldkey FROM my_coldkey")
        return sorted(r["coldkey"] for r in rows if r["coldkey"])

    async def _registered(self, coldkeys: list[str]) -> dict | None:
        """hotkey -> uid for our coldkeys' neurons on this subnet.

        None means UNKNOWN -- the chain poller has not populated the metagraph
        for this netuid. That is NOT the same as "we have nothing registered",
        and conflating the two would report every hotkey we own as deregistered
        the first time the chain poller missed a beat.
        """
        if not coldkeys:
            return {}
        from ...db import pool
        if not await pool().fetchval(
                "SELECT 1 FROM neuron_live WHERE netuid=$1 LIMIT 1", self.netuid):
            return None
        rows = await pool().fetch(
            "SELECT uid, hotkey FROM neuron_live"
            " WHERE netuid=$1 AND coldkey = ANY($2::text[])", self.netuid, coldkeys)
        return {r["hotkey"]: r["uid"] for r in rows if r["hotkey"]}

    async def _ours(self, set_id: int | None) -> dict:
        """Every agent of ours in the CURRENT set, keyed by hotkey.

        Prior sets are counted, not listed: a set is a different competition, and
        a set-26 agent says nothing about where we stand today.
        """
        coldkeys = await self._coldkeys()
        if not coldkeys:
            return {}
        reg = await self._registered(coldkeys)

        out: dict = {}
        for ck in coldkeys:
            d = await _get(f"retrieval/agents-by-coldkey?miner_coldkey={ck}")
            if not isinstance(d, dict):
                continue
            for hk, agents in d.items():
                for a in agents if isinstance(agents, list) else []:
                    cs = a.get("competition_state") or {}
                    if set_id is not None and cs.get("set_id") != set_id:
                        continue
                    key = short(hk, 8, 4)
                    prev = out.get(key)
                    # One hotkey can hold several versions in a set; the newest
                    # upload is the one that represents it.
                    if prev and (prev.get("ver") or 0) >= (a.get("version_num") or 0):
                        continue
                    stage = cs.get("status") or a.get("status")
                    out[key] = {
                        "label": "", "name": a.get("name"),
                        "hk": hk, "coldkey": ck,
                        "uid": (reg or {}).get(hk),
                        # None = the metagraph was unreadable; False = we read it
                        # and this hotkey is not in it, so the agent earns
                        # nothing no matter what it scores.
                        "registered": None if reg is None else (hk in reg),
                        "ver": a.get("version_num"),
                        "agent_id": a.get("agent_id"),
                        "stage": stage,
                        "phase": (STAGES.get(stage or "") or (0,))[0],
                        "review": cs.get("approval_review_status"),
                        "score": cs.get("final_score"),
                        "cost": cs.get("average_cost_usd"),
                        "rank": cs.get("rank"),
                        "approved": bool(cs.get("approved")),
                        "banked": cs.get("initial_reward_score"),
                        "set_id": cs.get("set_id"),
                    }
        return out

    async def _mine_meta(self, ours: dict) -> dict:
        """Counts that describe our POSITION rather than any one agent."""
        coldkeys = await self._coldkeys()
        out: dict = {"n_coldkeys": len(coldkeys)}
        if not coldkeys:
            return out
        reg = await self._registered(coldkeys)
        if reg is None:                     # unknown -> omit the key, never a 0
            return out
        out["n_registered"] = len(reg)
        out["our_hotkeys"] = sorted(reg)
        submitted = {o.get("hk") for o in ours.values()}
        # Registered hotkeys with nothing in the current set: capacity we are
        # holding and not using. On a subnet where every upload burns alpha
        # that is a budget line, not a curiosity.
        out["n_idle"] = len([h for h in reg if h not in submitted])
        return out

    async def _repo_impact(self) -> dict:
        """Name the files behind a commit, but only for a commit that happened.

        The framework's watcher already announces that RULES_REPO moved and
        links the compare. What it cannot say is whether the commit touched the
        scoring rules or a README — and that is the whole difference between
        "re-read incentives.py before the next upload" and "ignore".

        So this reads the sha the watcher has already stored and calls GitHub
        exactly once per REAL commit: no commit, no request. That matters
        because the watcher is already over the 60/hour unauthenticated budget
        (comp/github.py), and a TTL-based poll here would spend 12 more an hour
        to learn nothing.
        """
        from .. import github, store

        sha = None
        for row in await store.repos_for(self.netuid):
            if row.get("repo") == RULES_REPO:
                sha = row.get("sha")
                break
        if not sha:
            return {}
        if self._sha_seen is None:
            # First sight is a baseline, in this direction too.
            self._sha_seen = sha
            return {}
        if sha == self._sha_seen:
            return {}

        base, head = self._sha_seen, sha
        # Give up on this commit either way: the generic repo alert has already
        # gone out with a compare link, and retrying would spend a request per
        # poll for as long as GitHub stays unhappy.
        self._sha_seen = sha
        data, code = await github.conditional_json(
            f"https://api.github.com/repos/{RULES_REPO}/compare/{base}...{head}",
            cache_key=f"sn62:compare:{base}", timeout=30.0)
        if not isinstance(data, dict):
            log.info("sn62: compare %s...%s unavailable (HTTP %s)",
                     base[:8], head[:8], code)
            return {}
        files = [f.get("filename") or "" for f in (data.get("files") or [])]
        hot: list[dict] = []
        for path, why in HOT_PATHS:
            hit = [f for f in files if f.startswith(path)]
            if hit and not any(h["path"] == path for h in hot):
                hot.append({"path": path, "why": why, "n": len(hit)})
        return {
            "repo_range": f"{base[:10]}...{head[:10]}",
            "repo_files": sorted(files)[:40],
            "repo_n_files": len(files),
            "repo_commits": len(data.get("commits") or []),
            "repo_hot": hot,
        }

    # ---- 2. DIFF ------------------------------------------------------------
    def diff(self, old: dict, new: dict) -> list[CompEvent]:
        out: list[CompEvent] = []
        if not old:
            return out                      # first sight is a baseline

        out += self._diff_rules(old, new)
        out += self._diff_bar(old, new)
        out += self._diff_field(old, new)
        out += self._diff_repo(old, new)
        out += self._diff_ours(old, new)
        return out

    # -- the rules changing ----------------------------------------------------
    def _diff_rules(self, old: dict, new: dict) -> list[CompEvent]:
        out = []
        if changed(old, new, "comp_id"):
            L = [f"set <code>{esc(old.get('comp_id'))}</code> → "
                 f"<b>{esc(new.get('comp_id'))}</b>"]
            if new.get("comp_name"):
                L.append(esc(str(new["comp_name"])))
            if new.get("n_validator_problems"):
                L.append(f"{new.get('n_problems', '?')} problems · "
                         f"<b>{new['n_validator_problems']}</b> scored by validators")
            if new.get("difficulty"):
                L.append(" · ".join(f"{k} ×{v}" for k, v in new["difficulty"].items()))
            L.append("<i>every score, threshold and problem note we hold is "
                     "now stale — re-pull intel before uploading.</i>")
            out.append(CompEvent(
                kind="rules", severity="critical", icon="🆕",
                title=f"New evaluation set on SN{self.netuid}",
                body="\n".join(L), dedup_key=str(new.get("comp_id"))))

        # The qualify formula itself. These four numbers ARE the rules; a move
        # in any of them re-prices every decision made against the old ones.
        rule_keys = (("perf_threshold", "performance delta needed", pct),
                     ("cost_threshold", "cost delta needed", pct),
                     ("scr1_bar", "screener 1 threshold", num),
                     ("scr2_bar", "screener 2 threshold", num),
                     ("prune_bar", "prune threshold", num))
        moved = [(k, lbl, f) for k, lbl, f in rule_keys if changed(old, new, k)]
        if moved:
            out.append(CompEvent(
                kind="rules", severity="critical", icon="📐",
                title="SN62 scoring thresholds moved",
                body="\n".join(f"{esc(lbl)}: <code>{f(old.get(k))}</code> → "
                               f"<b>{f(new.get(k))}</b>" for k, lbl, f in moved)
                     + "\n<i>the qualify formula changed — recheck any planned "
                       "upload against it.</i>",
                dedup_key=",".join(f"{k}={new.get(k)}" for k, _, _ in moved)))

        if changed(old, new, "upload_ref"):
            o, n = old.get("upload_ref"), new.get("upload_ref")
            direction = "↑" if (n or 0) > (o or 0) else "↓"
            out.append(CompEvent(
                kind="price", severity="warn", icon="💸",
                title=f"Upload burn {direction} {num(n, 4)} alpha",
                body=f"<code>{num(o, 4)}</code> → <b>{num(n, 4)}</b> alpha per "
                     f"upload attempt "
                     f"({pct((float(n) - float(o)) / float(o)) if o else '—'})"
                     f"\n<i>burned on submit, never refunded.</i>",
                dedup_key=f"{n:.6f}" if n is not None else "?"))
        return out

    # -- the bar moving --------------------------------------------------------
    def _diff_bar(self, old: dict, new: dict) -> list[CompEvent]:
        out = []
        bar = self._bar_lines(new)

        leader_moved = changed(old, new, "king_id") and old.get("king_id")
        if leader_moved:
            out.append(CompEvent(
                kind="king_change", severity="critical", icon="👑",
                title="New SN62 leader",
                body=f"<b>{esc(new.get('king_name'))}</b> v{new.get('king_ver')} "
                     f"at <b>{num(new.get('king_score'))}</b>\n"
                     f"was {esc(old.get('king_name'))} v{old.get('king_ver')} "
                     f"at {num(old.get('king_score'))}\n\n" + bar,
                dedup_key=str(new.get("king_id"))))
        elif changed(old, new, "top_score") or changed(old, new, "top_cost"):
            # Same leader, new numbers: a re-evaluation or an approval moved the
            # bar under everyone without the crown changing hands.
            out.append(CompEvent(
                kind="bar", severity="warn", icon="🎯",
                title="SN62 qualify bar moved",
                body=f"leader score {num(old.get('top_score'))} → "
                     f"<b>{num(new.get('top_score'))}</b> · cost "
                     f"{_usd(old.get('top_cost'))} → <b>{_usd(new.get('top_cost'))}</b>"
                     f"\n\n" + bar,
                dedup_key=f"{new.get('top_score')}:{new.get('top_cost')}"))

        # An approval resets the stall multiplier to 1.0 and raises the bar.
        # It is the single event most likely to cancel a plan in flight.
        if changed(old, new, "n_approved"):
            try:
                grew = int(new["n_approved"]) > int(old["n_approved"])
            except (TypeError, ValueError):
                grew = False
            if grew:
                out.append(CompEvent(
                    kind="approved", severity="warn", icon="✅",
                    title=f"An SN62 agent was approved ({new['n_approved']} total)",
                    body="Someone banked emission.\n"
                         "<i>the stall multiplier just reset to 1.00x</i>\n\n" + bar,
                    dedup_key=f"approved:{new['n_approved']}"))

        # Bucketed stall. Only an upward crossing is news: it is the payout
        # multiplier waiting for whoever breaks the stall.
        if changed(old, new, "tm_mark"):
            try:
                rising = float(new["tm_mark"]) > float(old["tm_mark"])
            except (TypeError, ValueError):
                rising = False
            if rising and float(new["tm_mark"]) > 0:
                hrs = _hours_since(new.get("last_approval"))
                out.append(CompEvent(
                    kind="stall", severity="good", icon="⏱",
                    title=f"SN62 stall bonus ≥ {num(new['tm_mark'], 1)}x",
                    body=f"nobody approved for <b>{_dur(hrs)}</b> · multiplier "
                         f"now <b>{num(new.get('time_multiplier'), 2)}x</b>\n"
                         f"<i>the longer the board sits still, the more the next "
                         f"qualifying improvement banks.</i>\n\n" + bar,
                    dedup_key=f"tm:{new.get('last_approval')}:{new['tm_mark']}"))
        return out

    # -- the field -------------------------------------------------------------
    def _diff_field(self, old: dict, new: dict) -> list[CompEvent]:
        out = []

        # Rivals the validators are running right now. They have already cleared
        # both 0.76 screeners, so these are the only agents that can move the
        # bar within the hour.
        if changed(old, new, "n_evaluating"):
            was = old.get("n_evaluating") or 0
            now_ = new.get("n_evaluating") or 0
            if now_ > was:
                names = ", ".join(esc(f"{r.get('name')} v{r.get('ver')}")
                                  for r in (new.get("evaluating") or [])[:5])
                out.append(CompEvent(
                    kind="threat", severity="info", icon="⚔️",
                    title=f"{now_} rival(s) at the validator stage",
                    body=(names or "—") + "\n<i>past both screeners; the bar can "
                          "move within the hour.</i>",
                    dedup_key=",".join(sorted(r.get("hk") or ""
                                              for r in (new.get("evaluating") or [])))))

        # New agents that actually reached a score. The raw roster churns by
        # dozens an hour and about a third never clears the hardcoding judge, so
        # only a scored newcomer is worth a line.
        if changed(old, new, "scored_hk"):
            fresh = [h for h in new["scored_hk"]
                     if h not in set(old.get("scored_hk") or [])]
            if fresh:
                by_hk = {r.get("hk"): r for r in (new.get("board") or [])}
                lines = []
                for h in fresh[:8]:
                    r = by_hk.get(h) or {}
                    lines.append(f"{esc(r.get('name') or short(h))} · "
                                 f"{num(r.get('score'))} · {_usd(r.get('cost'))}")
                out.append(CompEvent(
                    kind="field", severity="info", icon="📈",
                    title=f"{len(fresh)} new scored agent(s) on SN62",
                    body="\n".join(lines) or "—",
                    dedup_key=",".join(sorted(fresh))))

        # Who the chain actually pays. Membership, not the weights themselves:
        # the numbers drift every epoch and none of that drift is a decision.
        if changed(old, new, "earning_hk"):
            was = set(old.get("earning_hk") or [])
            now_ = set(new.get("earning_hk") or [])
            added, gone = sorted(now_ - was), sorted(was - now_)
            if added or gone:
                by_hk = {w["hk"]: w["w"] for w in (new.get("weights") or [])}
                L = [f"+ <code>{esc(short(h))}</code> {pct(by_hk.get(h), 2)}"
                     for h in added[:6]]
                L += [f"− <code>{esc(short(h))}</code>" for h in gone[:6]]
                out.append(CompEvent(
                    kind="weights", severity="info", icon="⚖️",
                    title=f"SN62 earning set changed ({new.get('n_earning')} paid)",
                    body="\n".join(L),
                    dedup_key=",".join(sorted(now_))))
        return out

    # -- the repo --------------------------------------------------------------
    def _diff_repo(self, old: dict, new: dict) -> list[CompEvent]:
        """A second message, and only when the commit touched the rules.

        The framework already announced that the repo moved. This adds the one
        thing that decides whether to stop and read it: WHICH of the files that
        define the competition were in the diff.

        This is the ONE place in this adapter that must not go through
        `changed()`, and the reason is worth stating because it reads like a
        rule violation. `changed()` returns False whenever a key is absent from
        EITHER side -- first sight is a baseline in both directions -- and
        `repo_range` is absent from the stored snapshot on every tick except the
        single one right after a commit. Routed through `changed()` this event
        could therefore never fire at all, and would have failed silently: no
        error, no log line, just a rules commit that never got explained.

        Presence IS the signal here, safely, because `_repo_impact()` has
        already done its own baselining -- it emits the key only for a commit it
        watched happen, never on first sight of the repo.
        """
        hot = new.get("repo_hot") or []
        rng = new.get("repo_range")
        if not rng or rng == old.get("repo_range"):
            return []
        if not hot:
            return []                       # ordinary commit: one alert is enough
        L = [f"<code>{esc(new.get('repo_range'))}</code> · "
             f"{new.get('repo_commits', '?')} commit(s), "
             f"{new.get('repo_n_files', '?')} file(s)", ""]
        for h in hot:
            L.append(f"⚠️ <code>{esc(h['path'])}</code> ×{h['n']}\n   "
                     f"<i>{esc(h['why'])}</i>")
        L.append("")
        L.append(f"https://github.com/{RULES_REPO}/compare/"
                 f"{esc(new.get('repo_range') or '')}")
        return [CompEvent(
            kind="repo_impact", severity="critical", icon="📜",
            title="SN62 commit touched the rules",
            body="\n".join(L), dedup_key=str(new.get("repo_range")))]

    # -- our own agents --------------------------------------------------------
    def _diff_ours(self, old: dict, new: dict) -> list[CompEvent]:
        out: list[CompEvent] = []

        # A hotkey of ours leaving the metagraph. Worth waking someone for:
        # scoring and EARNING are separate gates, so a deregistered hotkey goes
        # on being evaluated and pays nothing, and the dashboard shows no sign
        # of it. Only a FALL is reported -- registering more is not an alarm.
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
                    title=f"{lost} of our SN62 hotkeys deregistered",
                    body="\n".join(f"<code>{esc(short(h))}</code>" for h in gone[:8])
                         + f"\n{old['n_registered']} → "
                           f"<b>{new['n_registered']}</b> registered\n"
                           f"<i>anything they submitted still scores and earns "
                           f"nothing.</i>",
                    dedup_key=",".join(gone) or str(new["n_registered"])))

        ours_now = new.get("ours") or {}
        ours_was = old.get("ours") or {}
        for ref, cur in ours_now.items():
            was = ours_was.get(ref)
            # A hotkey of ours appearing with an agent on it is an upload we
            # made. Only news once we already had a picture to compare against
            # -- otherwise the first poll after a deploy announces the whole
            # back catalogue at once.
            if was is None:
                if ours_was:
                    out.append(CompEvent(
                        kind="our_run", severity="info", icon="📤",
                        title=f"Upload accepted — {esc(cur.get('name') or ref)}",
                        body=f"<b>{esc(cur.get('name') or '?')}</b> "
                             f"v{cur.get('ver')} on <code>{esc(short(cur.get('hk')))}</code>"
                             f"\nnow at <b>{esc(_stage_meaning(cur.get('stage')))}</b>",
                        dedup_key=f"new:{cur.get('agent_id')}"))
                continue
            if was.get("stage") == cur.get("stage"):
                continue
            stage = cur.get("stage")
            dead = _is_terminal(stage)
            won = stage == "approved"
            L = [f"<b>{esc(cur.get('name') or cur.get('label') or ref)}</b> "
                 f"v{cur.get('ver')}",
                 f"{esc(_stage_meaning(was.get('stage')))} → "
                 f"<b>{esc(_stage_meaning(stage))}</b>"]
            if cur.get("score") is not None:
                L.append(f"score <b>{num(cur['score'])}</b> · "
                         f"cost {_usd(cur.get('cost'))}")
            if won and cur.get("banked") is not None:
                L.append(f"banked <b>{num(cur['banked'], 3)}</b> reward units")
            if stage == "didnt_qualify":
                L.append("<i>evaluated, below the bar, earns nothing — the next "
                         "attempt needs a new build, not a retry.</i>")
            out.append(CompEvent(
                kind="our_run",
                severity="good" if won else ("warn" if dead else "info"),
                icon="🏆" if won else ("💀" if dead else "🔬"),
                title=f"Our agent {esc(cur.get('name') or ref)} → "
                      f"{esc(str(stage))}",
                body="\n".join(L), dedup_key=f"{ref}:{stage}"))
        return out

    # ---- 3. RENDER ----------------------------------------------------------
    def _bar_lines(self, s: dict) -> str:
        """The two routes to qualifying, in the numbers an upload is judged by."""
        if s.get("top_score") is None:
            return "<i>qualify bar unavailable.</i>"
        L = ["<b>🎯 TO QUALIFY</b> (either route)"]
        if s.get("bar_score") is not None:
            need = ""
            n = s.get("n_validator_problems")
            if n:
                solved = math.ceil(float(s["bar_score"]) * int(n) - 1e-9)
                need = f" = <b>{solved}/{n}</b> problems"
            L.append(f"• score ≥ <b>{num(s['bar_score'], 4)}</b>{need}  "
                     f"<i>(+{pct(s.get('perf_threshold'), 0)} on "
                     f"{num(s.get('top_score'))})</i>")
        if s.get("bar_cost") is not None:
            L.append(f"• or score ≥ <b>{num(s.get('top_score'))}</b> at cost ≤ "
                     f"<b>{_usd(s['bar_cost'])}</b>  "
                     f"<i>(−{pct(s.get('cost_threshold'), 0)} on "
                     f"{_usd(s.get('top_cost'))})</i>")
        if s.get("time_multiplier") is not None:
            L.append(f"× <b>{num(s['time_multiplier'], 2)}</b> stall multiplier "
                     f"({_dur(_hours_since(s.get('last_approval')))} since the "
                     f"last approval)")
        return "\n".join(L)

    def render_state(self, s: dict) -> str:
        L = [f"<b>SN{self.netuid} · {esc(self.label)}</b>"]
        if s.get("comp_name"):
            L.append(f"<i>{esc(s['comp_name'])}</i>")
        L.append("")
        L.append(f"<b>👑 LEADER</b>  {esc(s.get('king_name') or '—')} "
                 f"v{s.get('king_ver')} · <code>{num(s.get('king_score'))}</code> "
                 f"· {_usd(s.get('top_cost'))}")
        L.append("")
        L.append(self._bar_lines(s))
        L.append("")
        L.append(f"<b>📥 FIELD</b>  {s.get('n_agents', '?')} agents · "
                 f"{s.get('n_miners', '?')} miners · "
                 f"<b>{s.get('n_approved', '?')}</b> approved")
        if s.get("n_evaluating"):
            L.append(f"⚔️ {s['n_evaluating']} at the validator stage right now")
        if s.get("upload_alpha") is not None:
            L.append(f"💸 upload burns <b>{num(s['upload_alpha'], 4)}</b> alpha "
                     f"<i>(never refunded)</i>")
        ours = s.get("ours") or {}
        n_reg = s.get("n_registered")
        if ours:
            live = [o for o in ours.values() if not _is_terminal(o.get("stage"))]
            L += ["", f"<b>🔬 OURS</b>  {len(live)} in flight of {len(ours)} "
                      f"in this set"]
            for ref, o in ours.items():
                warn = " ⚠️ not registered" if o.get("registered") is False else ""
                L.append(f"<code>{esc(o.get('name') or ref)}</code> v{o.get('ver')} "
                         f"· {esc(_stage_meaning(o.get('stage')))}"
                         + (f" · {num(o.get('score'))}"
                            if o.get("score") is not None else "") + warn)
            if s.get("n_idle"):
                L.append(f"<i>+ {s['n_idle']} registered hotkey(s) idle</i>")
        elif n_reg:
            L += ["", f"<b>🔬 OURS</b>  <b>{n_reg}</b> hotkey(s) registered, "
                      f"<i>nothing uploaded to this set</i>"]
        else:
            L += ["", "<i>nothing of ours registered on this subnet.</i>"]
        return "\n".join(L)

    def render_info(self, s: dict) -> str:
        L = [f"<b>SN{self.netuid} · {esc(self.label)} — the rules</b>", "",
             "No rounds and no window: agents are uploaded continuously and walk "
             "<code>pre_screening → screener_1 → screener_2 → validator → "
             "approval</code>. Emission is <b>not</b> rank-based — an agent banks "
             "only by beating the current leader, and each qualifying "
             "improvement then decays with a <b>336h half-life</b>.", ""]
        L.append(two_col([
            ("set", esc(s.get("comp_id") or "?")),
            ("problems", str(s.get("n_problems") or "?")),
            ("scored on", str(s.get("n_validator_problems") or "?")),
            ("agents", str(s.get("n_agents") or "?")),
            ("miners", str(s.get("n_miners") or "?")),
            ("approved", str(s.get("n_approved") or "?")),
            ("scr1 bar", num(s.get("scr1_bar"))),
            ("scr2 bar", num(s.get("scr2_bar"))),
            ("scr1 avg", num(s.get("scr1_avg"))),
            ("scr2 avg", num(s.get("scr2_avg"))),
            ("val avg", num(s.get("val_avg"))),
            ("burn", f"{num(s.get('upload_alpha'), 4)}α"),
        ], 20))
        L += ["", self._bar_lines(s)]

        funnel = s.get("funnel") or []
        if funnel:
            # The API's `pass_rate` is CUMULATIVE survival from `total`, not the
            # stage's own pass rate: screener_1 shows 0.39 when 67 of the 116
            # agents that reached it -- 58% -- got through. Labelling that column
            # "pass" would be a straight misreading of the field, so both are
            # shown: `all` as published, `step` derived from consecutive counts.
            rows = [f"{'stage':<11}{'n':>5}{'all':>6}{'step':>6}"]
            prev = None
            for f in funnel:
                n = f.get("n")
                step = "—"
                if prev not in (None, 0) and isinstance(n, int):
                    step = pct(n / prev, 0)
                rows.append(f"{str(f.get('stage')).replace('_emission', '')[:11]:<11}"
                            f"{str(n):>5}{pct(f.get('rate'), 0):>6}{step:>6}")
                if isinstance(n, int):
                    prev = n
            L += ["", "<b>Pipeline</b> <i>(all = of every upload, step = of the "
                  "stage before)</i>", "<pre>" + "\n".join(rows) + "</pre>"]
        if s.get("hardcode_rate") is not None:
            L.append(f"<i>{pct(s['hardcode_rate'])} of uploads are rejected by "
                     f"the hardcoding judge before any evaluation.</i>")

        waits = [(k, s.get(v)) for k, v in (("scr1", "scr1_wait"),
                                            ("scr2", "scr2_wait"),
                                            ("val", "val_wait"))]
        waits = [(k, v) for k, v in waits if v is not None]
        if waits:
            L += ["", "<b>Queue wait</b>",
                  two_col([(k, _dur(float(v) / 3600.0)) for k, v in waits], 14)]

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
        """The scored ladder, with our own rows coloured.

        Rendered through `diff_block`, so the leading character of each row is
        what a highlighting client paints: `+` (green) marks ours, everything
        else is plain. See base.diff_block for why the marker is a real column
        and not styling.
        """
        board = s.get("board") or []
        if not board:
            return "Leaderboard unavailable."
        ours = s.get("ours") or {}
        rows = [f"  {'#':<3}{'score':>6}{'cost':>8}  name"]
        shown = 0
        matched: set = set()
        for r in board[:limit]:
            if r.get("score") is None:
                continue
            # Matched on EITHER key: agent_id catches this exact upload, hotkey
            # catches an earlier version of ours still sitting on the ladder.
            mine = False
            for ref, o in ours.items():
                if ((o.get("agent_id") and o["agent_id"] == r.get("id"))
                        or (o.get("hk") and o["hk"] == r.get("hk"))):
                    mine = True
                    matched.add(ref)
            # esc() BEFORE the block: agent names are miner-controlled free text
            # and a <pre> is still HTML-parsed, so one stray `<` would make
            # Telegram reject the whole message rather than mis-render one row.
            name = esc(str(r.get("name") or "")[:13])
            rows.append(f"{'+' if mine else ' '} "
                        f"{str(r.get('rank') or '?'):<3}{num(r.get('score')):>6}"
                        f"{_usd(r.get('cost'), 3):>8}  {name}"
                        f"{' $' if r.get('approved') else ''}")
            shown += 1
        if shown:
            L = [f"<b>SN62 set {esc(s.get('comp_id'))}</b> · "
                 f"{s.get('n_agents', '?')} agents, "
                 f"{s.get('n_scored', '?')} scored",
                 diff_block(rows),
                 "<i>green + = ours · $ = approved and earning</i>"]
        else:
            # Early in a set nobody has a score yet. Falling through rather than
            # returning here matters: ours is in the pipeline at exactly that
            # moment, and an early return would hide it behind "no board".
            L = [f"<b>SN62 set {esc(s.get('comp_id'))}</b> · "
                 f"{s.get('n_agents', '?')} agents",
                 "<i>nothing in this set has been scored yet.</i>"]

        # An agent still in the pipeline has no score and therefore no row. Say
        # where it is instead of letting "show me my agents" come back empty
        # because ours has not reached the ladder yet.
        missing = [o for ref, o in ours.items() if ref not in matched]
        if missing:
            L.append("")
            for o in missing:
                where = (f"#{o['rank']}, below the top {limit}"
                         if o.get("rank") else _stage_meaning(o.get("stage")))
                L.append(f"<b>+ {esc(o.get('name') or '?')}</b> v{o.get('ver')} — "
                         f"{esc(where)}")
        L += ["", self._bar_lines(s)]
        return "\n".join(L)

    def render_me(self, s: dict) -> str:
        """Our position, derived entirely from what our coldkeys hold on chain.

        Three things this must not blur, because each one is a different
        decision: a hotkey that is registered and idle is capacity we could use
        today; a hotkey that submitted and died is a burned attempt; and an
        agent whose hotkey is no longer registered EARNS NOTHING however well it
        scores, which is the one case that looks fine and is not.
        """
        ours = s.get("ours") or {}
        n_reg = s.get("n_registered")
        if not s.get("n_coldkeys"):
            return ("No coldkey configured for SN62.\n"
                    "Add one to <code>my_coldkey</code>, or set "
                    "<code>SN62_COLDKEYS</code> to a comma-separated list.\n"
                    "<i>Everything under it is then tracked automatically — "
                    "hotkeys, uploads and all.</i>")

        L = [f"<b>Our SN62 position</b> · set {esc(s.get('comp_id'))}", ""]
        head = f"{s['n_coldkeys']} coldkey(s)"
        if n_reg is not None:
            head += f" · <b>{n_reg}</b> hotkey(s) registered here"
        else:
            head += " · <i>metagraph unavailable</i>"
        L.append(head)

        if not ours:
            L.append("")
            L.append(f"<i>nothing uploaded to set {esc(s.get('comp_id'))} yet.</i>")
            if n_reg:
                L.append(f"<b>{n_reg}</b> registered hotkey(s) are idle and "
                         f"could upload now.")
            L += ["", self._bar_lines(s)]
            return "\n".join(L)

        # Live first, then dead: the ones that still have a decision attached.
        rows = sorted(ours.values(),
                      key=lambda o: (_is_terminal(o.get("stage")),
                                     -(o.get("phase") or 0)))
        live = [o for o in rows if not _is_terminal(o.get("stage"))]
        dead = [o for o in rows if _is_terminal(o.get("stage"))]

        for title, group in (("🔬 IN FLIGHT", live), ("💀 FINISHED", dead)):
            if not group:
                continue
            L += ["", f"<b>{title}</b>"]
            for o in group:
                uid = f"uid {o['uid']}" if o.get("uid") is not None else "—"
                L.append(f"<code>{esc(o.get('name') or '?')}</code> "
                         f"v{o.get('ver')} · {uid} · "
                         f"<b>{esc(_stage_meaning(o.get('stage')))}</b>")
                if o.get("score") is not None:
                    gap = ""
                    if s.get("bar_score") is not None:
                        gap = (f" · {float(o['score']) - float(s['bar_score']):+.4f}"
                               f" vs bar")
                    L.append(f"   score {num(o['score'])} · "
                             f"cost {_usd(o.get('cost'))}{gap}")
                if o.get("banked") is not None:
                    L.append(f"   banked {num(o['banked'], 3)} reward units")
                if o.get("registered") is False:
                    # The failure mode that looks like success. Scoring and
                    # earning are separate gates and only one of them is on the
                    # dashboard.
                    L.append("   ⚠️ <b>hotkey is NOT registered on SN62</b> — "
                             "this agent earns nothing whatever it scores")

        if s.get("n_idle"):
            L += ["", f"<b>💤 IDLE</b>  {s['n_idle']} registered hotkey(s) with "
                      f"nothing in set {esc(s.get('comp_id'))}"]
        L += ["", self._bar_lines(s)]
        return "\n".join(L)

    def ours_summary(self, s: dict) -> str:
        ours = s.get("ours") or {}
        n_reg = s.get("n_registered")
        if not ours:
            # Registered but idle still counts as having something at stake:
            # it is capacity we are paying to hold and not using.
            return f"{n_reg} hotkeys idle" if n_reg else ""
        live = [o for o in ours.values() if not _is_terminal(o.get("stage"))]
        best = max((o.get("score") or 0) for o in ours.values())
        if live:
            return f"{len(live)} in flight · best {num(best)}"
        idle = f" · {s['n_idle']} idle" if s.get("n_idle") else ""
        return f"{len(ours)} done, none live · best {num(best)}{idle}"

    def render_digest(self, s: dict) -> str:
        tail = ""
        if s.get("time_multiplier") is not None:
            tail = f" · stall {num(s['time_multiplier'], 2)}x"
        return (f"<b>SN62</b> {esc(self.label)} — 👑 "
                f"{esc(s.get('king_name') or '—')} "
                f"<code>{num(s.get('king_score'))}</code> · bar "
                f"<code>{num(s.get('bar_score'), 4)}</code> · "
                f"{s.get('n_agents', '?')} agents{tail}")

    def render_guide(self, s: dict) -> str:
        return super().render_guide(s) + (
            "\n\n<b>How this subnet pays</b>\n"
            "There is <b>no round and no deadline</b>. An agent banks emission "
            "only by beating the current leader on one of two routes:\n"
            "• score <b>+3%</b> over the leader, or\n"
            "• score <b>≥</b> the leader at <b>−6%</b> cost.\n"
            "What it banks is multiplied by <b>1 + √(hours since the last "
            "approval / 12)</b> and then decays with a <b>336h (14 day) "
            "half-life</b>. So this is a <i>keep landing improvements</i> game, "
            "not a <i>hold rank 1</i> game — and a long stall is worth real "
            "money to whoever breaks it.\n\n"
            "<b>What the words mean</b>\n"
            "<code>finished</code> = evaluated, <b>not</b> earning. "
            "<code>didnt_qualify</code> = scored and below the bar, earns "
            "nothing. Only <code>approved</code> banks.\n\n"
            "<b>Cost of being wrong</b>\n"
            "Every upload burns alpha on submit and is <b>never refunded</b>, "
            "plus your own OpenRouter spend across every evaluation run."
        )
