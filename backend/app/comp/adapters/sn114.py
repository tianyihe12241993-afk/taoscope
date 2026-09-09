"""SN114 SOMA — context-compression competitions on SWE-bench agent traces.

WHAT MAKES THIS SUBNET AWKWARD, and why the adapter is shaped the way it is:

  * **There is no public JSON API.** `/api/private/frontend/*` is gated by
    `_require_private_network`, so every number here is scraped out of the
    Next.js RSC payload the dashboard ships inside its HTML. One page,
    `/dashboard?comp=<id>`, carries the competition list, the economics block,
    the validator list and the entire per-entry board in 85 KB. The per-miner
    page carries run-level detail and costs **1.7 MB**, which is why it is
    fetched on a TTL and rate-limited to a couple of hotkeys per tick.

  * **One upload per hotkey per competition, and there is no update endpoint.**
    `_ensure_single_upload_for_competition` 409s on the second attempt. So the
    single most valuable thing this bot can say is *"the upload window shuts in
    Xh and N of your hotkeys are still unspent"* — and it only says it while
    that is still actionable.

  * **A mid-stage saving ratio is a LIE.** The platform creates every run row up
    front and bills the baseline before the miner, so an entry part-way through
    a stage reports a huge fake saving. In comp 112 `sn114-9` showed 88.7%
    explore saving with **0 of 630 runs executed**. Every savings figure here is
    therefore gated on completion, and labelled when it is not complete.

  * **A stage-1 score before `screener_passed` is non-null is also a lie** — an
    ungraded row scores −4.000, identical to a row that resolved nothing, so a
    half-finished screen reads catastrophically. We misread that four times by
    hand. `s1_score` is only ever set once the platform has published a verdict.

  * **Agent runs are billed to the MINER'S OpenRouter key.** Full evaluation is
    ~2,910 runs per entry ≈ $32 at the measured ~$0.011/run. In comp 112 our key
    ran dry mid-eval: 998 of 1,940 statused runs failed, run-id-ordered, one
    partial task at the boundary — and every failed run scores **−4.000**. That
    signature is what `_credit_state()` watches for, and it is the one alert on
    this subnet worth waking someone for.

  * **The source review bans people, permanently.** `status = "failed review"`
    is `Miner.miner_banned_status`; a banned miner can never upload again. In
    comp 112 the **top 16 scores on the board were all banned**, every one above
    the best clean entry — so the board must be read with them separated out or
    the bar looks 50% higher than it is.

Our own hotkeys come from `/work/sn114-soma/artifacts/our_hotkeys.json`; entries
are matched by **hotkey**, never by uid, because uids get reassigned.
"""
from __future__ import annotations

import collections
import datetime as dt
import json
import logging
import os
import re
import time
from pathlib import Path

import httpx

from .. import store
from ..base import (CompEvent, SubnetAdapter, changed, esc, num, pct, short,
                    two_col)

log = logging.getLogger("taoscope.comp.sn114")

SITE = "https://thesoma.ai"
PLATFORM = "https://platform.thesoma.ai"

# Measured on our own comp-112 entries: stage 1 was ~$1.6 for 150 runs.
# Used only for order-of-magnitude budgeting; the point is "$30 not $3".
COST_PER_RUN_USD = 0.011
# Comp-112 shape. Re-derived from the live task rows whenever detail is
# available; these are the fallback so /state can budget before a single run
# has been dispatched.
RUNS_STAGE1 = 150
RUNS_STAGE2 = 630
RUNS_EVAL = 2910

# Fraction of statused runs that must be failing, over this many runs, before
# we call the key dry. In comp 112 the real event was 51%; a rival's normal
# background rate was 0.05%. 20% is far outside anything healthy.
CREDIT_FAIL_RATE = 0.20
CREDIT_MIN_RUNS = 30

# --------------------------------------------------------------------------
# Which files in DendriteHQ/SOMA are worth a phone buzz.
#
# The framework's generic repo watcher fires `critical` for ANY commit to a
# watched repo, which is right for a repo that moves twice a month. SOMA's main
# repo landed 12 commits in 15 days and most were `perf(validator)` or
# `hotfix(frontend)` — buzzing for those is how a topic gets muted, and then the
# one that matters arrives silently. So SOMA/main is watched HERE instead, and
# graded by what it touched. Longest prefixes first; first match wins.
# --------------------------------------------------------------------------
RULE_PATHS: tuple[tuple[str, str, str], ...] = (
    # path prefix, class, what it decides
    ("mcp_platform/app/services/incentive_calculator", "money",
     "the score formula, benchmark weights and which stages count"),
    ("mcp_platform/app/api/routes/scoring", "money",
     "the per-task score — the log2 ratio and the resolution bonus"),
    ("mcp_platform/app/services/swebench_orchestrator", "gate",
     "stage-2 advancement and run dispatch"),
    ("mcp_platform/app/services/swebench_screening", "gate",
     "the screening gates"),
    ("mcp_platform/app/api/routes/miner", "gate",
     "upload rules — one-per-competition, registration and the ban check"),
    ("mcp_platform/app/services/script_store", "gate",
     "what an upload is allowed to contain"),
    ("docs/miner", "docs", "the published miner rules"),
    ("mcp_platform/app/services/sandbox", "harness",
     "how a run actually executes — this moves step counts, and steps are the score"),
    ("mcp_platform/app/api/routes/sandbox", "harness", "run dispatch"),
    ("mcp_platform/app/api/routes/validator", "harness", "validator claim/scoring path"),
)
# class -> (severity, icon, headline)
RULE_CLASS = {
    "money":   ("critical", "🚨", "SCORING CHANGED"),
    "gate":    ("critical", "🚨", "A GATE CHANGED"),
    "docs":    ("warn", "📄", "The published rules changed"),
    "harness": ("warn", "🔧", "The run harness changed"),
    "ungraded": ("warn", "📦", "SOMA moved — could not read the diff"),
    "other":   ("info", "📦", "SOMA moved"),
}
RULE_RANK = {"money": 0, "gate": 1, "docs": 2, "harness": 3, "ungraded": 4,
             "other": 5}

# Added patch lines worth calling out by name. A competition-id-keyed special
# case is the exact shape of the two comp-112 hotfixes that decided our result
# — eval-only final scoring, and a hand-written advancer list — and neither was
# announced anywhere. A grep of the added lines is the only warning available.
RULE_NEEDLES: tuple[tuple[str, str], ...] = (
    ("competition_id ==", "a rule keyed to ONE competition id"),
    ("_COMPETITION_", "a competition-specific constant"),
    ("override_hotkeys", "a hand-written hotkey override list"),
    ("BENCHMARK_WEIGHTS", "the benchmark weighting"),
    ("screener_stage", "which stages count toward the final score"),
    ("top_screener_scripts", "how many entries advance"),
    ("stage2_min_advancers", "the stage-2 advancer floor"),
    ("miner_banned_status", "the ban check"),
)

# Hours-remaining thresholds that get an upload-window warning, TIGHTEST first.
# Ascending order matters: at 18h remaining the honest headline is "under 24h",
# not "under 48h", and a poll that skips a bucket must still report the tightest
# one it has actually crossed. Only fired while we still hold an unspent hotkey
# — otherwise the deadline is not something anyone can act on.
WINDOW_BUCKETS = (1, 6, 24, 48)

# phase -> (icon, the words a human uses for it)
PHASE = {
    "unsent":   ("⚪", "not uploaded"),
    "queued":   ("⏳", "queued — not started"),
    "screen1":  ("🔬", "stage 1 screening"),
    "failed1":  ("❌", "FAILED stage 1"),
    "passed1":  ("✅", "passed stage 1"),
    "screen2":  ("🧪", "stage 2 qualification"),
    "eval":     ("🔥", "full evaluation — burning credit"),
    "scored":   ("🏁", "scored"),
    "banned":   ("🚫", "BANNED — failed review"),
    "nokey":    ("🔑", "no OpenRouter key"),
    "out":      ("⬛", "not qualified"),
}
# Terminal for this competition — no more detail fetches, no more transitions.
TERMINAL = {"failed1", "banned", "out", "scored"}
ORDER = {"eval": 0, "screen2": 1, "screen1": 2, "queued": 3, "passed1": 4,
         "scored": 5, "unsent": 6, "nokey": 7, "failed1": 8, "banned": 9,
         "out": 10}

# Statuses seen on a board row. Anything outside this set is logged once, because
# comp 112's vocabulary (scored / not qualified / failed review / no api key) was
# INCOMPLETE -- comp 145 introduced `in queue`, and until it was modelled the
# funnel reported 19 miners as "stage 1 screening" when not one had started.
KNOWN_STATUS = {"scored", "not qualified", "failed review", "no api key",
                "in queue", "screening"}
_seen_status: set = set()

_PUSH = re.compile(r'self\.__next_f\.push\(\[1\s*,\s*"((?:[^"\\]|\\.)*)"')
_DEC = json.JSONDecoder()


# --------------------------------------------------------------------------
# RSC scraping. The dashboard is a Next.js app; its server payload arrives as a
# series of `self.__next_f.push([1,"<chunk>"])` calls whose concatenation is one
# long string containing the JSON the page was rendered from.
# --------------------------------------------------------------------------
async def _fetch_html(url: str, timeout: float = 45.0) -> str | None:
    """GET, returning None on any failure. None means unknown, never empty."""
    try:
        async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as cx:
            r = await cx.get(url, headers={"accept": "text/html"})
            if r.status_code != 200:
                log.debug("fetch %s -> HTTP %s", url, r.status_code)
                return None
            return r.text
    except Exception as exc:  # noqa: BLE001
        log.debug("fetch %s failed: %s", url, exc)
        return None


def _rsc(html: str) -> str:
    out = []
    for m in _PUSH.finditer(html):
        try:
            out.append(json.loads('"' + m.group(1) + '"'))
        except ValueError:
            pass
    return "".join(out)


def _block(text: str, key: str):
    """The JSON value that follows `"key":`, or None.

    Uses the stdlib decoder's raw_decode rather than a hand-rolled bracket
    matcher: the miner page's `sweRunsByTaskId` is 1.4 MB and a Python-level
    per-character scan of it costs a second per poll. This costs 9 ms.
    """
    i = text.find(f'"{key}":')
    if i < 0:
        return None
    j = i + len(key) + 3
    while j < len(text) and text[j] in " \t\r\n":
        j += 1
    try:
        obj, _ = _DEC.raw_decode(text, j)
        return obj
    except ValueError:
        return None


async def _fetch_rsc(url: str, timeout: float = 45.0) -> str | None:
    html = await _fetch_html(url, timeout)
    return _rsc(html) if html else None


# --------------------------------------------------------------------------
def _iso(v) -> dt.datetime | None:
    if not v:
        return None
    try:
        return dt.datetime.fromisoformat(str(v).replace("Z", "+00:00"))
    except ValueError:
        return None


def _hours_until(iso) -> float | None:
    t = _iso(iso)
    if t is None:
        return None
    if t.tzinfo is None:
        t = t.replace(tzinfo=dt.timezone.utc)
    return (t - dt.datetime.now(dt.timezone.utc)).total_seconds() / 3600.0


def _dur(hours: float | None) -> str:
    if hours is None:
        return "—"
    if hours < 0:
        return "closed"
    if hours < 1:
        return f"{int(hours * 60)}m"
    if hours < 48:
        return f"{hours:.1f}h"
    return f"{hours / 24:.1f}d"


def _ago(iso: str | None) -> str:
    t = _iso(iso)
    if t is None:
        return "never"
    if t.tzinfo is None:
        t = t.replace(tzinfo=dt.timezone.utc)
    secs = int((dt.datetime.now(dt.timezone.utc) - t).total_seconds())
    if secs < 90:
        return f"{secs}s ago"
    if secs < 5400:
        return f"{secs // 60}m ago"
    return f"{secs // 3600}h ago"


def _usd(runs) -> str:
    try:
        return f"${float(runs) * COST_PER_RUN_USD:,.0f}"
    except (TypeError, ValueError):
        return "—"


class SN114(SubnetAdapter):
    # ---- identity -----------------------------------------------------------
    netuid = 114
    slug = "soma"
    label = "SOMA"

    # Windows move in hours and the board only changes as runs land. The one
    # time-critical fact is the upload window closing, and 5 min is plenty of
    # resolution for a deadline measured in days.
    poll_seconds = 300

    # SOMA-shared carries the upload signing contract the miner script depends
    # on; SOMA-benchmark defines the agent harness. Both move a couple of times
    # a month, so the framework's blanket `critical` on any commit is exactly
    # right for them.
    #
    # DendriteHQ/SOMA is deliberately NOT here. It is the platform — scoring,
    # gates and the ban check all live in it — but it also carries every
    # frontend and perf commit, 12 in 15 days. It is watched by _rules_watch()
    # below instead, which grades a commit by the files it touched, so a
    # `hotfix(scoring)` buzzes and a `hotfix(frontend)` does not.
    RULES_REPO = "DendriteHQ/SOMA"
    repos: list[str] = [
        "DendriteHQ/SOMA-shared",
        "DendriteHQ/SOMA-benchmark",
    ]

    links = {
        "dashboard": f"{SITE}/dashboard",
        "docs": f"{SITE}/docs",
        "platform": PLATFORM,
        "repo": "https://github.com/DendriteHQ/SOMA",
    }

    watchfile = Path(os.getenv("SN114_WATCHFILE",
                               "/work/sn114-soma/artifacts/our_hotkeys.json"))

    # The miner page is 1.7 MB. Cache it hard and refresh a couple per tick, so
    # a nine-hotkey fleet costs ~3 MB per poll instead of 15 MB.
    DETAIL_TTL_S = 1800
    DETAIL_PER_TICK = 2
    _detail_cache: dict[str, dict] = {}
    _detail_at: dict[str, float] = {}
    # ETags live in comp.github now, shared across adapters. _rules_prev_sha is
    # the baseline the next compare is taken from; None means "not seen yet",
    # which is never news.
    _rules_prev_sha: str | None = None
    _rules_cache: dict = {}
    # {"comp": id, "sha": anchor, "fields": {...}} — the repo as of this
    # competition's upload_start, plus everything accumulated since.
    _round: dict = {}
    # Back-off between attempts to resolve an unresolved round anchor.
    ANCHOR_RETRY_S = 900
    _anchor_try_at: float = 0.0
    # A 304 is not free on GitHub, so the HEAD read is rate-limited to the same
    # cadence as the framework's repo watcher rather than every poll.
    HEAD_TTL_S = 600
    _head_at: float = 0.0

    alerts = {
        "rules": "the SOMA platform repo moved, graded by what it touched — "
                 "scoring and gate changes buzz, frontend and perf do not",
        "dereg": "one of OUR hotkeys fell out of the metagraph and can no "
                 "longer upload",
        "comp_state": "a competition opened, closed for uploads, or finished — "
                      "the upload door is the only irreversible moment here",
        "window": "the upload window is closing and we still hold unspent "
                  "hotkeys (48h / 24h / 6h / 1h)",
        "our_run": "one of our entries changed phase — screening, qualified, "
                   "entered full evaluation, scored, or died at a gate",
        "credit": "our runs are FAILING in bulk — the OpenRouter key is dry and "
                  "every failed run scores −4.000",
        "banned": "a miner failed the source review — permanent, and it moves "
                  "the real top of the board",
        "king_change": "the best CLEAN score changed (banned entries excluded)",
        "king_score": "the leader's score moved",
        "new_entrant": "hotkeys uploaded to the open competition",
        "field": "how many miners are still competing moved materially, or the "
                 "stage-2 cut started/stopped filtering anyone",
        "validators": "no validator is working — nothing is being scored",
    }

    # ---- guide --------------------------------------------------------------
    def render_guide(self, s: dict) -> str:
        base = super().render_guide(s)
        return base + (
            "\n\n<b>How this subnet works</b>\n"
            "You upload one Python <code>compress_messages()</code> that rewrites "
            "an agent's conversation before it is sent to the model. You are paid "
            "for cutting tokens <b>without costing the agent the task</b>.\n\n"
            "<b>One upload per hotkey per competition. There is no update "
            "endpoint</b> — the second POST returns 409 and whatever you sent is "
            "final. Register the hotkey first or the upload 403s.\n\n"
            "Three gates, in order:\n"
            "• <b>stage 1</b> — 5 public tasks × 10 runs × 3 benchmark types. A "
            "quality screen. <b>Pays nothing.</b>\n"
            "• <b>stage 2</b> — 5 more tasks, ~630 runs. Not a threshold, a "
            "<b>rank</b>: top <code>max(60, 20%)</code> advance.\n"
            "• <b>evaluation</b> — 25 tasks, <b>~2,910 runs</b>. This is the only "
            "stage that pays, and the only one that costs real money.\n\n"
            "<b>Runs are billed to YOUR OpenRouter key.</b> ~$0.011/run, so a full "
            "eval is ≈$32 per entry. An unfunded key does not fail loudly: each "
            "dead run scores <b>−4.000</b>, which is far worse than never "
            "entering. This bot watches the failure rate for exactly that.\n\n"
            "<b>Two numbers this bot refuses to show you</b>\n"
            "A saving ratio mid-stage (the baseline is billed before the miner, so "
            "it reads as a huge fake saving — one of ours showed 88.7% with zero "
            "runs executed) and a stage-1 score before the platform publishes a "
            "verdict (ungraded rows sit at −4.000). Both are shown as <code>—</code> "
            "with the run counts instead.\n\n"
            "<b>Your hotkey can be evicted.</b> The subnet sits at 256/256 uids "
            "with registration OPEN, so every registration somebody else pays "
            "for displaces a neuron — and the pool it displaces from is the "
            "ones earning nothing, which is nearly all of ours. Six of our "
            "fifteen were gone before anyone checked. A deregistered hotkey "
            "<b>403s on upload</b>, so it is not a scoring problem, it is a "
            "missing player.\n\n"
            "<b>The board lies unless you split it.</b> <code>failed review</code> "
            "means banned by the source review — permanent, no further uploads. In "
            "comp 112 the <b>top 16 scores were all banned</b>, every one above the "
            "best clean entry. <code>/board</code> separates them."
        )

    # ---- 1. COLLECT ---------------------------------------------------------
    async def seed_watchlist(self) -> list[dict]:
        """Our hotkeys, from the file the miner tooling keeps.

        Only registered ones are returned: a hotkey that has fallen out of the
        metagraph cannot upload (the platform 403s on
        `_ensure_miner_registered`), so tracking it would add a permanently
        grey row to /mine and nothing else.
        """
        try:
            rows = json.loads(self.watchfile.read_text())
        except Exception:  # noqa: BLE001 -- absent file is normal, not an error
            return []
        return [{"ref": r["ss58"], "label": r.get("name", ""), "uid": r.get("uid")}
                for r in rows if r.get("ss58") and r.get("registered", True)]

    async def snapshot(self) -> dict:
        s: dict = {}

        # One 85 KB page carries competitions + economics + validators + the
        # whole board for whichever competition is active.
        t = await _fetch_rsc(f"{SITE}/dashboard")
        if not t:
            return s

        comps = _block(t, "competitions")
        if isinstance(comps, list) and comps:
            swe = [c for c in comps if c.get("competition_type") == "swe"]
            active = next((c for c in swe if c.get("is_active")), None) or swe[0]
            s["comp_id"] = active.get("competition_id")
            s["comp_name"] = active.get("competition_name")
            s["comp_state"] = active.get("state")
            s["upload_start"] = active.get("upload_start")
            s["upload_end"] = active.get("upload_end")
            s["eval_start"] = active.get("evaluation_start")
            s["eval_end"] = active.get("evaluation_end")
            s["n_comps"] = len(swe)
            prev = next((c for c in swe
                         if c.get("state") == "finished"
                         and c.get("competition_id") != s["comp_id"]), None)
            if prev:
                s["prev_comp_id"] = prev.get("competition_id")
                s["prev_comp_name"] = prev.get("competition_name")

        econ = _block(t, "economics")
        if isinstance(econ, dict):
            s["prize_pool_tao"] = econ.get("prize_pool_tao")
            s["reg_cost_tao"] = econ.get("registration_cost_tao")
            s["alpha_price_tao"] = econ.get("alpha_price_tao")
            s["burn_ratio"] = econ.get("burn_ratio")

        vals = _block(t, "validators")
        if isinstance(vals, list):
            live = [v for v in vals if not v.get("is_archive")]
            s["n_validators"] = len(live)
            s["n_validators_working"] = sum(1 for v in live
                                            if v.get("status") == "working")

        board = _block(t, "sweMiners")
        if isinstance(board, list):
            s.update(self._board_fields(board, s.get("comp_state")))

        s.update(await self._rules_watch(s.get("comp_id"), s.get("upload_start")))
        s["ours"] = await self._ours(board if isinstance(board, list) else [],
                                     s.get("comp_id"), s.get("comp_state"))
        s.update(await self._registry(s))
        s.update(self._fleet_fields(s))
        return s

    # ---- the platform repo, graded ------------------------------------------
    async def _rules_watch(self, comp_id, upload_start) -> dict:
        """Watch DendriteHQ/SOMA, graded by what a commit touched, and
        ACCUMULATED FROM THE START OF THE ROUND.

        A poll-to-poll delta is the wrong unit for a decision. What you built a
        candidate against is the platform as it stood when the round opened, and
        a single "scoring changed" message scrolls out of the chat within a day
        of a five-day window. So the useful question, askable at any moment, is
        *what has moved since this competition opened* — and that is what
        /state and /info answer.

        The anchor is resolved from the competition's own `upload_start` rather
        than from whenever this process first looked, so it is identical after a
        restart and correct even if the bot was down when the round opened.

        Cost, per competition: one `?until=<upload_start>` call to find the
        anchor and one `compare` to catch up on whatever already happened, then
        one `compare` per head move. The HEAD read is ETag-conditional and a 304
        is free. The cumulative view is an ACCUMULATOR over those per-move
        compares rather than a second range call each time, which halves the
        budget — and 60/hour unauthenticated is shared with every other adapter.
        """
        from .. import github          # late: avoids an import cycle at module load
        parked = github.blocked_for()
        if parked:
            # The framework has parked on a 403/rate-limit. Continuing to call
            # both stays blind and keeps the budget pinned at zero.
            #
            # Report the blindness. Falling back silently to the last known sha
            # renders as "unchanged since the round opened", which is a claim we
            # cannot make while we cannot see -- the same absence-is-not-a-change
            # rule the diff obeys, applied to the view.
            return dict(self._rules_cache, gh_blocked_s=parked)

        # A conditional HEAD read still costs a request -- a 304 returns no body
        # but does decrement the budget (measured; see comp/github.py). At a
        # 300s poll that is 12/hour spent on a repo that moves twelve times a
        # FORTNIGHT, so it runs on the same 600s cadence as the framework's own
        # repo watcher and serves the cache in between.
        if time.time() - self._head_at < self.HEAD_TTL_S and self._rules_cache:
            return dict(self._rules_cache)
        head, code = await self._gh(f"https://api.github.com/repos/{self.RULES_REPO}"
                                    f"/commits/HEAD", etag_key="head")
        if code in (200, 304):
            self._head_at = time.time()
        if code == 304:
            # NOT the same as a failure. A 304 is GitHub positively stating the
            # sha has not moved, so the last known values are still true and
            # must be carried forward -- omitting them would delete soma_sha
            # from the stored snapshot and blank the platform line in /state
            # after a single quiet poll.
            return dict(self._rules_cache)
        if head is None:
            # A real failure. Omit the fields (absence is not a change) but say
            # the watch is degraded rather than letting /state imply calm.
            return dict(self._rules_cache, gh_blocked_s=-1)
        sha = head.get("sha")
        if not sha:
            return {}
        # Normalise to the abbreviated form the snapshot persists. Comparing a
        # stored 10-char baseline against a fresh 40-char sha would read as a
        # move on the very first poll after a restart. GitHub's compare accepts
        # abbreviated refs, so nothing is lost by working in 10.
        sha = sha[:10]
        commit = head.get("commit") or {}
        out = {
            "gh_blocked_s": 0,
            "soma_sha": sha,
            "soma_subject": (commit.get("message") or "").splitlines()[0][:120],
            "soma_author": ((commit.get("author") or {}).get("name") or "")[:60],
            "soma_at": (commit.get("author") or {}).get("date"),
        }

        # Seed the baseline from the stored snapshot, not just from memory. The
        # framework's own watcher persists its sha in `comp_repo`, so it
        # survives a restart; an in-memory-only baseline would re-baseline on
        # boot and silently swallow a scoring commit that landed while the
        # process was down — which is exactly when one is most likely to.
        stored = {}
        if self._rules_prev_sha is None or self._round.get("comp") != comp_id:
            try:
                stored = await store.last_state(self.netuid)
            except Exception:  # noqa: BLE001
                stored = {}
        if self._rules_prev_sha is None:
            self._rules_prev_sha = stored.get("soma_sha")

        anchor = await self._round_anchor(comp_id, upload_start, sha, stored)
        out.update(anchor)

        prev = self._rules_prev_sha
        self._rules_prev_sha = sha
        if not prev or prev == sha:
            # First sight is a baseline. Grade it as `other` so a restart cannot
            # replay an old scoring commit as news.
            out["soma_class"] = out.get("soma_class") or "other"
            self._rules_cache = out
            return out

        cmp_, _ = await self._gh(f"https://api.github.com/repos/{self.RULES_REPO}"
                                 f"/compare/{prev}...{sha}")
        if cmp_ is None:
            # The sha really did move; we just could not fetch the diff. Say so
            # rather than grading it "other", which is silent -- a rate-limited
            # compare must not turn a scoring commit into no message at all.
            out["soma_class"] = "ungraded"
            out["soma_prev"] = prev
            self._rules_cache = out
            return out
        files = [f.get("filename", "") for f in (cmp_.get("files") or [])]
        subjects = [(c.get("commit") or {}).get("message", "").splitlines()[0][:90]
                    for c in (cmp_.get("commits") or [])]

        worst, hits = "other", []
        for path in files:
            for prefix, cls, what in RULE_PATHS:
                if path.startswith(prefix):
                    if RULE_RANK[cls] < RULE_RANK[worst]:
                        worst = cls
                    hits.append((path, cls, what))
                    break

        # Added lines only: a competition-id-keyed special case is invisible in
        # the subject line and decided our whole comp-112 result.
        flags = []
        for f in (cmp_.get("files") or []):
            added = "\n".join(ln for ln in (f.get("patch") or "").splitlines()
                              if ln.startswith("+"))
            for needle, meaning in RULE_NEEDLES:
                if needle in added and meaning not in flags:
                    flags.append(meaning)

        out.update({
            "soma_class": worst,
            "soma_prev": prev,
            # Ordered by class, NOT alphabetically. Sorting by text put the
            # harness reason first under a "SCORING CHANGED" headline and then
            # truncated the scoring reason off the end entirely — the one line
            # the headline was promising.
            "soma_files": _ranked([(p, c) for p, c, _ in hits], 8),
            "soma_reasons": _ranked([(w, c) for _, c, w in hits], 4),
            "soma_commits": subjects[-8:],
            "soma_n_commits": cmp_.get("total_commits") or len(subjects),
            "soma_flags": flags[:4],
            "soma_compare": (f"https://github.com/{self.RULES_REPO}"
                             f"/compare/{prev}...{sha}"),
        })
        out.update(self._accumulate(worst, hits, flags,
                                    cmp_.get("total_commits") or len(subjects), sha))
        self._rules_cache = out
        return out

    # ---- the round anchor ---------------------------------------------------
    async def _round_anchor(self, comp_id, upload_start, head_sha: str,
                            stored: dict) -> dict:
        """The repo as it stood when THIS competition opened.

        Resolved from `upload_start`, not from first sight, so the answer is the
        same on every restart and is right even if the bot was down when the
        round began. Once known it never changes, so it is resolved once per
        competition and then carried in the snapshot.

        On a competition we have not anchored before, one catch-up `compare`
        seeds the accumulator with whatever already landed this round —
        otherwise a bot started on day three would report "nothing has changed"
        about a round whose scoring had already moved twice.
        """
        if comp_id is None:
            return {}
        if self._round.get("comp") == comp_id and self._round.get("sha"):
            return dict(self._round["fields"])

        # Restart: adopt the stored anchor rather than spending calls again.
        if stored.get("round_comp") == comp_id and stored.get("round_sha"):
            fields = {k: stored[k] for k in
                      ("round_comp", "round_sha", "round_sha_at", "since_n",
                       "since_class", "since_files", "since_reasons",
                       "since_flags", "since_shas", "since_compare")
                      if k in stored}
            self._round = {"comp": comp_id, "sha": stored["round_sha"],
                           "fields": fields}
            return dict(fields)

        # The anchor is a one-time fact, but an UNRESOLVED one would otherwise
        # be retried on every 300s poll — 12 wasted calls an hour out of a
        # 60/hour budget shared with every other adapter, at exactly the moment
        # that budget is already exhausted. Back off so a blind spell costs the
        # next hour less than it cost this one.
        if time.time() - self._anchor_try_at < self.ANCHOR_RETRY_S:
            return {}
        self._anchor_try_at = time.time()

        anchor_sha, anchor_at = head_sha, None
        if upload_start:
            rows, _ = await self._gh(
                f"https://api.github.com/repos/{self.RULES_REPO}"
                f"/commits?until={upload_start}&per_page=1")
            if isinstance(rows, list) and rows:
                anchor_sha = (rows[0].get("sha") or head_sha)[:10]
                anchor_at = ((rows[0].get("commit") or {}).get("author")
                             or {}).get("date")
            elif rows is None:
                # Could not resolve it. Do NOT anchor on head and claim the
                # round has been quiet -- leave it unset and retry next poll.
                return {}

        fields = {"round_comp": comp_id, "round_sha": anchor_sha,
                  "round_sha_at": anchor_at, "since_n": 0,
                  "since_class": "other", "since_files": [],
                  "since_reasons": [], "since_flags": [], "since_shas": []}
        if anchor_sha != head_sha:
            cmp_, _ = await self._gh(
                f"https://api.github.com/repos/{self.RULES_REPO}"
                f"/compare/{anchor_sha}...{head_sha}")
            if cmp_ is not None:
                fields.update(self._grade_range(cmp_))
                fields["since_compare"] = (f"https://github.com/{self.RULES_REPO}"
                                           f"/compare/{anchor_sha}...{head_sha}")
        self._round = {"comp": comp_id, "sha": anchor_sha, "fields": fields}
        return dict(fields)

    @staticmethod
    def _grade_range(cmp_: dict) -> dict:
        """Grade one compare response into cumulative fields."""
        files = [f.get("filename", "") for f in (cmp_.get("files") or [])]
        worst, hits = "other", []
        for path in files:
            for prefix, cls, what in RULE_PATHS:
                if path.startswith(prefix):
                    if RULE_RANK[cls] < RULE_RANK[worst]:
                        worst = cls
                    hits.append((path, cls, what))
                    break
        flags = []
        for f in (cmp_.get("files") or []):
            added = "\n".join(ln for ln in (f.get("patch") or "").splitlines()
                              if ln.startswith("+"))
            for needle, meaning in RULE_NEEDLES:
                if needle in added and meaning not in flags:
                    flags.append(meaning)
        return {
            "since_n": cmp_.get("total_commits") or len(cmp_.get("commits") or []),
            "since_class": worst,
            "since_files": _ranked([(p, c) for p, c, _ in hits], 8),
            "since_reasons": _ranked([(w, c) for _, c, w in hits], 4),
            "since_flags": flags[:4],
            "since_shas": [(c.get("sha") or "")[:10]
                           for c in (cmp_.get("commits") or [])][-40:],
        }

    def _accumulate(self, cls: str, hits: list, flags: list, n: int,
                    head_sha: str) -> dict:
        """Roll one graded move into the since-round-start totals.

        An accumulator rather than a second range call per move: the cumulative
        class is the worst seen since the anchor, the file list is the union,
        and the count is the sum. One `compare` per move covers both views.
        """
        f = dict(self._round.get("fields") or {})
        if not f.get("round_sha"):
            return {}
        if RULE_RANK.get(cls, 9) < RULE_RANK.get(f.get("since_class", "other"), 9):
            f["since_class"] = cls
        f["since_n"] = (f.get("since_n") or 0) + n
        f["since_files"] = _ranked(
            [(p, c) for p, c, _ in hits]
            + [(p, f.get("since_class", "other")) for p in (f.get("since_files") or [])],
            8)
        f["since_reasons"] = _ranked(
            [(w, c) for _, c, w in hits]
            + [(w, f.get("since_class", "other")) for w in (f.get("since_reasons") or [])],
            4)
        f["since_flags"] = list(dict.fromkeys(
            (f.get("since_flags") or []) + list(flags)))[:4]
        f["since_compare"] = (f"https://github.com/{self.RULES_REPO}"
                              f"/compare/{f['round_sha']}...{head_sha}")
        self._round = {"comp": f.get("round_comp"), "sha": f["round_sha"],
                       "fields": f}
        return f

    async def _gh(self, url: str, *, etag_key: str = "") -> tuple:
        """(body, status), via the framework's conditional GitHub client.

        The ETag cache, the token and the rate-limit park all live in
        `comp.github` so that every adapter shares one budget discipline rather
        than each re-implementing it slightly differently.
        """
        from .. import github
        return await github.conditional_json(
            url, cache_key=f"sn114:{etag_key}" if etag_key else url)

    # ---- the metagraph ------------------------------------------------------
    async def _registry(self, s: dict) -> dict:
        """Which of our hotkeys still exist on chain.

        SN114 sits at 256/256 uids with registration OPEN, so every new
        registration EVICTS somebody, and the eviction pool is the neurons with
        no incentive — which is eight of our nine. Six of our fifteen hotkeys
        were already gone before anyone looked, and a deregistered hotkey cannot
        upload at all: the platform 403s on `_ensure_miner_registered`. So this
        is not chain trivia, it is a live inventory of what we can still play.
        """
        from ...db import pool
        ours = s.get("ours") or {}
        if not ours:
            return {}
        try:
            rows = await pool().fetch(
                "SELECT hotkey, uid, incentive, block_at_registration"
                "  FROM neuron_live WHERE netuid=$1", self.netuid)
            zero = await pool().fetchval(
                "SELECT count(*) FROM neuron_live"
                " WHERE netuid=$1 AND coalesce(incentive,0)=0", self.netuid)
        except Exception as exc:  # noqa: BLE001
            log.debug("neuron_live read failed: %s", exc)
            return {}
        if not rows:
            return {}                       # sweep has not run yet: unknown
        live = {r["hotkey"]: r for r in rows if r["hotkey"]}

        gone = []
        for name, o in ours.items():
            r = live.get(o.get("hk"))
            o["registered"] = r is not None
            if r is None:
                gone.append(name)
                continue
            o["uid"] = r["uid"]             # authoritative; the file's uid rots
            o["incentive"] = r["incentive"]
            o["reg_block"] = r["block_at_registration"]
        return {
            "dereg_ours": sorted(gone, key=_natural),
            "n_reg_ours": len(ours) - len(gone),
            "n_zero_incentive": zero,
            "n_neurons": len(rows),
        }

    # ---- board --------------------------------------------------------------
    def _board_fields(self, board: list[dict], comp_state: str | None) -> dict:
        """The field, as a funnel — who is still competing, not just who exists.

        "How many are actually mining this round" is the question every other
        number here is relative to, and the raw row count does not answer it.
        In comp 112, 458 rows: 88 scored, 16 banned, and **353 sitting at
        `not qualified`** — which mid-competition means "has not qualified YET",
        not "is out". Counting those as dead while the round is live would
        report a field a fifth of its real size; counting them as alive after it
        closes would report one five times too big. So the funnel is derived
        from the screener evidence and the competition's own state, and the
        buckets are named for what the miner is doing, not for its status label.

        Rivals cannot be resolved past stage 2: telling "in stage 2" from "in
        full evaluation" needs the 1.7 MB per-miner page, and we only pay that
        for our own hotkeys. So the two are one bucket, honestly labelled.
        """
        s: dict = {}
        s["n_field"] = len(board)
        s["n_scored"] = sum(1 for r in board if r.get("status") == "scored")
        s["n_banned"] = sum(1 for r in board if r.get("status") == "failed review")
        s["n_nokey"] = sum(1 for r in board if r.get("status") == "no api key")
        s["n_unqualified"] = sum(1 for r in board
                                 if r.get("status") == "not qualified")
        s["board_hk"] = sorted({r["hotkey"] for r in board if r.get("hotkey")})

        # ---- the funnel ----
        buckets = collections.Counter()
        for row in board:
            st = row.get("status")
            if st and st not in KNOWN_STATUS and st not in _seen_status:
                _seen_status.add(st)
                log.warning("SN114: unmodelled board status %r — the funnel is "
                            "guessing for these rows", st)
            buckets[self._phase(self._summarise_row(row), comp_state)] += 1
        s["statuses"] = sorted({r.get("status") for r in board if r.get("status")})
        s["phases"] = dict(buckets)
        s["n_queued"] = buckets["queued"]
        s["n_screening"] = buckets["screen1"]
        s["n_qualified"] = buckets["passed1"] + buckets["screen2"]
        # The number the question actually asks for: entries with runs still
        # flowing. Everything else has either finished or died.
        s["n_active"] = s["n_queued"] + s["n_screening"] + s["n_qualified"]
        s["n_out"] = (buckets["failed1"] + buckets["out"] + buckets["banned"]
                      + buckets["nokey"])

        # Does the stage-2 rank actually filter anything? `_select_stage2_advancers`
        # takes min(total, max(60, ceil(20%))), so below 60 passers it is not a
        # gate at all and merely surviving stage 1 is worth an eval slot. That
        # changes whether a candidate has to be good or merely valid.
        #
        # Measured on comp 112's final board: 395 of 458 entries passed stage 1
        # (86%), so the quality screen is a weak filter and the stage-2 RANK is
        # the real one — 79 would have advanced under this rule. 88 actually
        # did, because comp 112's advancers came from a hand-written override
        # file instead. So treat this as the rule, not as a prediction of a
        # competition the operator has intervened in.
        passed = sum(1 for r in board
                     if (((r.get("screener") or {}).get("stage1") or {})
                         .get("screener_passed")) is True)
        s["n_passed1"] = passed
        if passed:
            s["advance_cut"] = min(passed, max(60, -(-passed * 20 // 100)))
            s["cut_binds"] = passed > s["advance_cut"]

        clean = sorted(
            (r for r in board
             if r.get("status") == "scored" and r.get("total_score") is not None),
            key=lambda r: -r["total_score"])
        s["n_ranked"] = len(clean)
        s["board"] = [{
            "rank": i,
            "hk": r.get("hotkey", ""),
            "score": r.get("total_score"),
            "cats": r.get("category_scores") or {},
            "s1": ((r.get("screener") or {}).get("stage1") or {}).get("score"),
            "s2": ((r.get("screener") or {}).get("stage2") or {}).get("score"),
            "submit": r.get("last_submit"),
        } for i, r in enumerate(clean[:20], 1)]
        if clean:
            s["king_hk"] = clean[0].get("hotkey")
            s["king_score"] = clean[0].get("total_score")
            s["n_negative"] = sum(1 for r in clean if (r["total_score"] or 0) < 0)

        banned = sorted(
            (r for r in board
             if r.get("status") == "failed review" and r.get("total_score") is not None),
            key=lambda r: -r["total_score"])
        if banned:
            s["banned_top"] = banned[0].get("total_score")
        # Sorted so the API's row order cannot fabricate a diff.
        s["banned_hk"] = sorted({r["hotkey"] for r in board
                                 if r.get("status") == "failed review" and r.get("hotkey")})
        return s

    @staticmethod
    def _summarise_row(row: dict) -> dict:
        """A board row reduced to what _phase() reads.

        Shared by the field funnel and by our own entries, so a rival and one of
        ours can never be graded by two different rules — which is exactly how a
        leaderboard ends up disagreeing with /mine.
        """
        scr = row.get("screener") or {}
        s1, s2 = scr.get("stage1") or {}, scr.get("stage2") or {}
        e = {"status": row.get("status"), "total": row.get("total_score"),
             "submit": row.get("last_submit"),
             "cats": row.get("category_scores") or {}}
        # A stage-1 score before the platform publishes a verdict is dominated
        # by ungraded rows at -4.000; never record it as a score.
        if s1.get("screener_passed") is not None:
            e["s1_passed"] = bool(s1["screener_passed"])
            e["s1_score"] = s1.get("score")
            e["s1_vfy"] = s1.get("verified_token_savings_ratio")
            e["s1_exp"] = s1.get("explore_token_savings_ratio")
            e["s1_edt"] = s1.get("edit_token_savings_ratio")
        if s2.get("score") is not None:
            e["s2_score"] = s2.get("score")
        return e

    # ---- our entries --------------------------------------------------------
    async def _ours(self, board: list[dict], comp_id, comp_state: str | None) -> dict:
        """Phase and progress per watched hotkey, matched by hotkey not uid.

        The cheap board row answers everything except "has full evaluation
        started" and "are the runs actually executing", which is the pair that
        decides whether money is being spent and whether it is being wasted.
        Those two come from the 1.7 MB miner page, on a TTL.
        """
        rows = {r.get("hotkey"): r for r in board if r.get("hotkey")}
        watched = await store.watched(self.netuid)
        out: dict = {}
        budget = self.DETAIL_PER_TICK
        now = time.time()

        for w in watched:
            hk = w["ref"]
            name = w.get("label") or short(hk)
            row = rows.get(hk)
            entry = {"hk": hk, "uid": w.get("uid"), "name": name}

            if row is None:
                # No row for this competition = nothing uploaded on this hotkey.
                # During `upload` that is the actionable state; afterwards it
                # just means the hotkey sat out.
                entry["phase"] = "unsent"
                out[name] = entry
                continue

            # Exactly the same reduction the field funnel applies to a rival —
            # including the rule that a stage-1 score is withheld until the
            # platform publishes a verdict, because an ungraded row sits at
            # −4.000 and reads as a catastrophe.
            entry.update(self._summarise_row(row))

            detail = self._cached_detail(hk)
            if detail is None and budget > 0 and comp_id is not None \
                    and comp_state != "upload":
                fetched = await self._detail(hk, comp_id)
                if fetched is not None:
                    detail = fetched
                    budget -= 1
                elif now - self._detail_at.get(hk, 0.0) > 1:
                    # 404 during screening is normal: the page only exists once
                    # tasks are seeded. Do not treat it as an outage.
                    self._detail_at[hk] = now
            if detail:
                entry.update(detail)

            entry["phase"] = self._phase(entry, comp_state)
            out[name] = entry
        return out

    @staticmethod
    def _phase(e: dict, comp_state: str | None) -> str:
        """Derive the situation from evidence, never from the status label.

        `status` is the platform's state machine, and it says "not qualified"
        for a hotkey mid-screen as readily as for one that lost. What an
        operator needs to know is which gate the entry is standing at and
        whether it is spending money.
        """
        if e.get("status") == "failed review":
            return "banned"
        if e.get("status") == "no api key":
            return "nokey"
        # Uploaded and accepted, but nothing has run yet. Reporting this as
        # "screening" would claim work the platform has not started -- the same
        # label-vs-situation error the docstring warns about, in reverse.
        if e.get("status") == "in queue" and e.get("s1_passed") is None:
            return "queued"
        if e.get("total") is not None:
            return "scored"
        if e.get("s1_passed") is False:
            return "failed1"        # terminal whatever the round is doing
        # ORDER MATTERS. A finished round has nobody in it: an entry that
        # reached stage 2 and never scored did not stay in stage 2 forever, it
        # ran out of competition. Checking this after the stage tests reported
        # 307 of comp 112's 458 entries as "still competing" three hours after
        # the round closed — the exact lie this funnel exists to avoid.
        if comp_state == "finished":
            return "out"
        if (e.get("runs_eval") or 0) > 0:
            return "eval"
        if e.get("s2_score") is not None or (e.get("runs_s2") or 0) > 0:
            return "screen2"
        if e.get("s1_passed") is True:
            return "passed1"
        return "screen1"

    def _cached_detail(self, hk: str) -> dict | None:
        if time.time() - self._detail_at.get(hk, 0.0) < self.DETAIL_TTL_S:
            return self._detail_cache.get(hk)
        return None

    async def _detail(self, hk: str, comp_id) -> dict | None:
        """Run-level progress for one hotkey. 1.7 MB — call it sparingly."""
        t = await _fetch_rsc(f"{SITE}/dashboard/miner/{comp_id}/{hk}")
        if not t:
            return None
        tasks = _block(t, "sweTasks")
        runs = _block(t, "sweRunsByTaskId")
        if not isinstance(tasks, list) or not isinstance(runs, dict):
            return None

        # TRAP: sweRunsByTaskId keys are "<benchmark_type>:<task_id>" and the
        # SAME task_id appears under all three benchmark types. Building a
        # task_id -> benchmark_type map with setdefault silently collapses all
        # three into whichever was seen first; that mis-attributed 998 run
        # failures to the wrong benchmark on the first hand analysis. stage IS
        # keyed on task_id, so only the stage lookup below is safe.
        stage_of = {t_["task_id"]: t_.get("screener_stage") for t_ in tasks}

        out: dict = {}
        for bucket, want in (("s1", 1), ("s2", 2), ("eval", None)):
            total = done = failed = statused = 0
            dead_tasks = 0
            for key, rr in runs.items():
                try:
                    tid = int(key.rsplit(":", 1)[1])
                except (IndexError, ValueError):
                    continue
                if stage_of.get(tid) != want:
                    continue
                total += len(rr)
                for r in rr:
                    st = r.get("status")
                    if st is not None:
                        statused += 1
                        if st == "failed":
                            failed += 1
                    if r.get("tokens_with_compression") or st == "completed":
                        done += 1
            for t_ in tasks:
                if stage_of.get(t_["task_id"]) != want:
                    continue
                # A task with a billed baseline and zero miner tokens produced
                # nothing at all. Mid-stage that is "not started"; the failure
                # count above is what distinguishes the two.
                if t_.get("tokens_without_compression") and not t_.get("tokens_with_compression"):
                    dead_tasks += 1
            if total:
                out[f"runs_{bucket}"] = total
                out[f"done_{bucket}"] = done
                out[f"failed_{bucket}"] = failed
                out[f"statused_{bucket}"] = statused
                out[f"deadtasks_{bucket}"] = dead_tasks

        summ = _block(t, "sweSummary")
        if isinstance(summ, dict):
            out["task_count"] = summ.get("task_count")
        prof = _block(t, "profile")
        if isinstance(prof, dict):
            out["rank"] = ((prof.get("last_contest") or {}).get("rank"))
            out["source_public"] = bool((prof.get("source_code") or {}).get("available"))
        tot = _block(t, "sweTokenTotals")
        if isinstance(tot, dict) and tot.get("baseline_weighted_tokens"):
            out["tok_base"] = tot.get("baseline_weighted_tokens")
            out["tok_mine"] = tot.get("miner_weighted_tokens")

        self._detail_cache[hk] = out
        self._detail_at[hk] = time.time()
        return out

    # ---- fleet --------------------------------------------------------------
    @staticmethod
    def _fleet_fields(s: dict) -> dict:
        """Roll our entries up into the two numbers a decision hangs on:
        how many hotkeys are still unspent, and how much money is committed."""
        ours = s.get("ours") or {}
        if not ours:
            return {}
        unsent = sorted((n for n, o in ours.items() if o.get("phase") == "unsent"),
                        key=_natural)
        live = [o for o in ours.values() if o.get("phase") not in TERMINAL
                and o.get("phase") != "unsent"]
        runs_done = sum((o.get("done_s1") or 0) + (o.get("done_s2") or 0)
                        + (o.get("done_eval") or 0) for o in ours.values())
        runs_left = sum(
            max(0, (o.get("runs_s1") or 0) - (o.get("done_s1") or 0))
            + max(0, (o.get("runs_s2") or 0) - (o.get("done_s2") or 0))
            + max(0, (o.get("runs_eval") or 0) - (o.get("done_eval") or 0))
            for o in ours.values())
        out = {
            "n_ours": len(ours),
            "n_unsent": len(unsent),
            "unsent": unsent,
            "n_live": len(live),
            "runs_done": runs_done,
            "runs_left": runs_left,
        }
        credit = [n for n, o in ours.items() if _credit_state(o)]
        out["credit_dry"] = sorted(credit, key=_natural)
        best = [(o.get("total"), n) for n, o in ours.items() if o.get("total") is not None]
        if best:
            sc, name = max(best)
            out["our_best"] = sc
            out["our_best_name"] = name
        return out

    # ---- 2. DIFF ------------------------------------------------------------
    def diff(self, old: dict, new: dict) -> list[CompEvent]:
        out: list[CompEvent] = []
        if not old:
            return out
        out += self._diff_rules(old, new)
        out += self._diff_registry(old, new)
        out += self._diff_comp(old, new)
        out += self._diff_board(old, new)
        out += self._diff_ours(old.get("ours") or {}, new.get("ours") or {}, new)
        if changed(old, new, "n_validators_working") \
                and (new.get("n_validators_working") or 0) == 0:
            out.append(CompEvent(
                kind="validators", severity="warn", icon="🛑",
                title="No validator is working",
                body=f"{old.get('n_validators_working')} → "
                     f"<b>0</b> of {new.get('n_validators', '?')}\n"
                     f"<i>Nothing is being scored. A stalled run is the platform, "
                     f"not your script.</i>",
                dedup_key="validators:0"))
        return out

    def _diff_rules(self, old: dict, new: dict) -> list[CompEvent]:
        """One event per SOMA/main move, at the severity its files deserve.

        The headline names the class, not the commit subject: `hotfix(scoring):
        exclude comp 112 stage 2 from final score` was a one-line subject for a
        change that rewrote which stages the final score is computed over, and
        `perf(orchestrator): scale swe dispatch and trim log noise` quietly
        carried a hand-written advancer override in the same commit. What the
        commit touched is more honest than what it says it did.
        """
        if not changed(old, new, "soma_sha"):
            return []
        # `soma_prev` is set ONLY when _rules_watch compared from a baseline it
        # already had. Without it this is first sight of the field, not a move
        # — which is what `old` looks like the first time an adapter upgrade
        # adds a key, and after any state wipe. That fired three identical
        # "None → ec05da4e9a" alerts across three restarts. First sight of a
        # FIELD is a baseline for exactly the same reason first sight of a
        # SNAPSHOT is, and `if not old` does not cover it.
        if not new.get("soma_prev"):
            return []
        cls = new.get("soma_class") or "other"
        sev, icon, head = RULE_CLASS.get(cls, RULE_CLASS["other"])
        n = new.get("soma_n_commits") or 1

        lines = [f"<code>{esc(new['soma_prev'])}</code> → "
                 f"<code>{esc(new['soma_sha'])}</code> · "
                 f"{n} commit{'' if n == 1 else 's'}"]
        for reason in (new.get("soma_reasons") or []):
            lines.append(f"↳ {esc(reason)}")
        for f in (new.get("soma_files") or [])[:5]:
            lines.append(f"<code>{esc(f.rsplit('/', 1)[-1])}</code>")
        for subj in (new.get("soma_commits") or [])[-4:]:
            lines.append(f"· {esc(subj)}")
        if new.get("soma_flags"):
            lines.append("⚠️ <b>added lines mention:</b> "
                         + esc("; ".join(new["soma_flags"])))
        # The cumulative frame is what makes this actionable a week into a
        # round: one move is easy to shrug at, "the third scoring commit since
        # you uploaded" is not.
        since = new.get("since_n") or 0
        if since > n and new.get("round_sha"):
            _, s_icon, s_head = RULE_CLASS.get(new.get("since_class") or "other",
                                               RULE_CLASS["other"])
            lines.append(f"<b>{since} commits since the round opened</b> "
                         f"{s_icon} {esc(s_head.lower())}")
        if cls in ("money", "gate"):
            spent = (new.get("n_ours") or 0) - (new.get("n_unsent") or 0)
            if spent > 0:
                lines.append(f"🚨 <b>{spent} of our hotkeys have ALREADY "
                             f"uploaded to this round</b> — they are running "
                             f"code built against the older rules, and there is "
                             f"no update endpoint.")
            lines.append("<i>Read the diff before the next upload — a comp-112 "
                         "hotfix of exactly this shape decided that whole "
                         "competition, and was announced nowhere.</i>")
        if new.get("soma_compare"):
            lines.append(esc(new["soma_compare"]))
        return [CompEvent(
            kind="rules", severity=sev, icon=icon,
            title=f"{head} — {self.RULES_REPO}",
            body="\n".join(lines),
            dedup_key=f"soma:{new['soma_sha']}",
            detail={"class": cls, "files": new.get("soma_files"),
                    "old": new.get("soma_prev"), "new": new.get("soma_sha")})]

    def _diff_registry(self, old: dict, new: dict) -> list[CompEvent]:
        """Our own hotkeys leaving the metagraph.

        The framework already reports registration opening/closing and operator
        growth for every subnet. Neither says the thing that matters here: at
        256/256 with registration open, somebody else's registration is our
        eviction, and an evicted hotkey cannot upload at all.
        """
        fresh = _fresh(old, new, "dereg_ours")
        if not fresh:
            return []
        ours = new.get("ours") or {}
        unsent = [n for n in fresh if (ours.get(n) or {}).get("phase") == "unsent"]
        body = [", ".join(f"<b>{esc(n)}</b>" for n in fresh),
                f"{new.get('n_reg_ours', '?')} of {new.get('n_ours', '?')} "
                f"hotkeys still registered"]
        if unsent and new.get("comp_state") == "upload":
            body.append(f"<i>{len(unsent)} of them had not uploaded yet — that "
                        f"upload slot is gone unless it is re-registered "
                        f"(τ{num(new.get('reg_cost_tao'), 4)}).</i>")
        else:
            body.append("<i>A deregistered hotkey 403s on upload. Re-register "
                        "it or drop it from the fleet.</i>")
        return [CompEvent(
            kind="dereg", severity="critical", icon="🪫",
            title=f"{len(fresh)} of our hotkeys DEREGISTERED",
            body="\n".join(body),
            dedup_key="dereg:" + ",".join(sorted(fresh)),
            detail={"names": fresh})]

    def _diff_comp(self, old: dict, new: dict) -> list[CompEvent]:
        """The competition is the only clock on this subnet.

        A new competition_id means every hotkey's one upload is available again
        — the single most valuable thing to be told. The state transition out of
        `upload` is the moment that stops being true.
        """
        out = []
        if changed(old, new, "comp_id"):
            hrs = _hours_until(new.get("upload_end"))
            out.append(CompEvent(
                kind="rules", severity="critical", icon="🆕",
                title=f"New competition — {new.get('comp_name') or new.get('comp_id')}",
                body=f"<code>{esc(old.get('comp_id'))}</code> → "
                     f"<code>{esc(new.get('comp_id'))}</code> · "
                     f"state <b>{esc(new.get('comp_state'))}</b>\n"
                     f"uploads close in <b>{_dur(hrs)}</b> "
                     f"({esc(new.get('upload_end'))})\n"
                     f"<i>Every hotkey gets one upload again — one per hotkey per "
                     f"competition, and no update endpoint.</i>",
                dedup_key=f"comp:{new.get('comp_id')}",
                detail={"old": old.get("comp_id"), "new": new.get("comp_id")}))
            return out          # the state change below is implied by the new id

        if changed(old, new, "comp_state"):
            was, is_ = old.get("comp_state"), new.get("comp_state")
            shut = was == "upload"
            out.append(CompEvent(
                kind="comp_state", severity="critical" if shut else "warn",
                icon="🚪" if shut else "🔄",
                title=("Upload window CLOSED" if shut
                       else f"Competition is now {is_}"),
                body=f"<b>{esc(was)}</b> → <b>{esc(is_)}</b> · "
                     f"comp {esc(new.get('comp_id'))}\n"
                     + (f"<i>{new.get('n_unsent', 0)} of our hotkeys never "
                        f"uploaded.</i>" if shut and new.get("n_unsent")
                        else "<i>Evaluation spends the OpenRouter key.</i>"),
                dedup_key=f"state:{new.get('comp_id')}:{is_}"))

        # The deadline itself MOVING. Comp 145's upload_end went from 08-25 to
        # 08-27 mid-round and nothing fired, because the only thing watching it
        # was the countdown-bucket check below — and an extension makes the
        # remaining hours go UP, which no bucket can catch. A shortened window
        # is an emergency; a lengthened one is not, but it still rewrites the
        # plan, so it is reported quietly rather than not at all.
        if changed(old, new, "upload_end") and old.get("upload_end"):
            was_h, now_h = (_hours_until(old["upload_end"]),
                            _hours_until(new["upload_end"]))
            if was_h is not None and now_h is not None:
                shorter = now_h < was_h
                out.append(CompEvent(
                    kind="window", severity="critical" if shorter else "info",
                    icon="⏱" if shorter else "📅",
                    title=(f"Upload window {'SHORTENED' if shorter else 'EXTENDED'} "
                           f"by {_dur(abs(now_h - was_h))}"),
                    body=f"<code>{esc(old['upload_end'])}</code> → "
                         f"<code>{esc(new['upload_end'])}</code>\n"
                         f"{_dur(now_h)} left · {new.get('n_unsent', '?')} of our "
                         f"hotkeys unspent"
                         + ("\n<i>Less time than you planned for.</i>" if shorter
                            else "\n<i>More room to screen an entry and send a "
                                 "second build on another hotkey.</i>"),
                    dedup_key=f"deadline:{new.get('comp_id')}:{new['upload_end']}"))

        # Deadline pressure, but only while it is actionable.
        if new.get("comp_state") == "upload" and new.get("n_unsent"):
            hrs = _hours_until(new.get("upload_end"))
            was = _hours_until(old.get("upload_end")) if old.get("upload_end") else None
            if hrs is not None and was is not None:
                for b in WINDOW_BUCKETS:            # tightest crossed bucket wins
                    if hrs <= b < was and hrs > 0:
                        out.append(CompEvent(
                            kind="window", severity="warn", icon="⏳",
                            title=f"Upload window closes in {_dur(hrs)}",
                            body=f"<b>{new['n_unsent']} of {new.get('n_ours', '?')} "
                                 f"hotkeys still unspent</b>\n"
                                 f"{esc(', '.join((new.get('unsent') or [])[:9]))}\n"
                                 f"<i>One upload per hotkey per competition. "
                                 f"An unused window is a whole cycle lost.</i>",
                            dedup_key=f"window:{new.get('comp_id')}:{b}"))
                        break
        return out

    def _diff_board(self, old: dict, new: dict) -> list[CompEvent]:
        out = []
        if changed(old, new, "king_hk") and old.get("king_hk"):
            out.append(CompEvent(
                kind="king_change", severity="critical", icon="👑",
                title="New leader (clean board)",
                body=f"{esc(short(new['king_hk']))} · "
                     f"<b>{num(new.get('king_score'), 4)}</b>\n"
                     f"was {esc(short(old.get('king_hk')))} · "
                     f"{num(old.get('king_score'), 4)}\n"
                     f"{new.get('n_ranked', '?')} scored · "
                     f"{new.get('n_banned', 0)} banned",
                dedup_key=str(new.get("king_hk")),
                detail={"old": old.get("king_hk"), "new": new.get("king_hk")}))
        elif changed(old, new, "king_score"):
            o, n = old.get("king_score") or 0, new.get("king_score") or 0
            out.append(CompEvent(
                kind="king_score", severity="info",
                icon="📈" if n > o else "📉",
                title=f"Leader's score → {num(n, 4)}",
                body=f"{num(o, 4)} → <b>{num(n, 4)}</b> ({n - o:+.4f})\n"
                     f"same hotkey {esc(short(new.get('king_hk')))}",
                dedup_key=f"{new.get('king_hk')}:{num(n, 4)}"))

        # Bans move the real top of the board, so they are news even when the
        # banned hotkey is a stranger.
        fresh = _fresh(old, new, "banned_hk")
        if fresh:
            ours = {o.get("hk"): n for n, o in (new.get("ours") or {}).items()}
            mine = [ours[h] for h in fresh if h in ours]
            if mine:
                out.append(CompEvent(
                    kind="banned", severity="critical", icon="🚫",
                    title=f"OUR hotkey was BANNED: {', '.join(mine)}",
                    body="\n".join(f"<code>{esc(short(h))}</code>" for h in fresh
                                   if h in ours)
                         + "\n<i>failed review — this miner can never upload "
                           "again, on any competition.</i>",
                    dedup_key="ours:" + ",".join(sorted(mine))))
            else:
                out.append(CompEvent(
                    kind="banned", severity="warn", icon="⚖️",
                    title=f"{len(fresh)} miner{'' if len(fresh) == 1 else 's'} banned",
                    body="\n".join(esc(short(h)) for h in fresh[:8])
                         + f"\n{new.get('n_banned', 0)} banned in total · "
                           f"best banned score {num(new.get('banned_top'), 4)}\n"
                           f"<i>The clean bar is {num(new.get('king_score'), 4)}.</i>",
                    dedup_key=",".join(sorted(fresh))))

        fresh = _fresh(old, new, "board_hk")
        if fresh:
            out.append(CompEvent(
                kind="new_entrant", severity="info", icon="➕",
                title=f"{len(fresh)} uploaded to comp {new.get('comp_id')}",
                body="\n".join(esc(short(h)) for h in fresh[:8])
                     + f"\n<b>{new.get('n_field', '?')}</b> in the field · "
                       f"{new.get('n_active', '?')} still competing",
                dedup_key=",".join(sorted(fresh)),
                detail={"hotkeys": fresh}))
        out += self._diff_field(old, new)
        return out

    def _diff_field(self, old: dict, new: dict) -> list[CompEvent]:
        """How many are actually competing, but only when the answer moves
        enough to change a decision.

        Raw field depth is not news — `new_entrant` above already reports each
        arrival. Two transitions are:

        * the field crossing a 25% band, which is the difference between "a
          handful entered" and "we are up against 458 again";
        * the stage-2 rank starting or stopping BINDING. `_select_stage2_advancers`
          takes `min(total, max(60, ceil(20%)))`, so at 60 or fewer stage-1
          passers every survivor gets an evaluation slot and merely being valid
          is enough. Above it, being good starts to matter. Nothing else about
          the field changes what we would build.
        """
        out = []
        if changed(old, new, "n_active") and "n_active" in old:
            was, now = old.get("n_active") or 0, new["n_active"]
            # Fire on crossing a tenth-of-the-field boundary rather than on a
            # percentage delta. A 25% gate kept a 400 -> 307 collapse silent
            # because 93 fell short of 100, while still being free to fire
            # repeatedly on a small field. Bucketing self-limits instead: at
            # most ~10 of these across a whole competition, and every one of
            # them is a real change in how crowded the round is.
            band = max(5, (new.get("n_field") or now or 10) // 10)
            if now // band != was // band:
                out.append(CompEvent(
                    kind="field", severity="info",
                    icon="📈" if now > was else "📉",
                    title=f"{now} miners still competing",
                    body=f"{was} → <b>{now}</b> active · "
                         f"{new.get('n_field', '?')} entered · "
                         f"{new.get('n_out', 0)} out\n"
                         f"{new.get('n_screening', 0)} in stage 1 · "
                         f"{new.get('n_qualified', 0)} past it · "
                         f"{new.get('n_scored', 0)} scored",
                    dedup_key=f"active:{new.get('comp_id')}:{now // band}"))

        if changed(old, new, "cut_binds"):
            binds = new["cut_binds"]
            passed, cut = new.get("n_passed1", 0), new.get("advance_cut", 0)
            out.append(CompEvent(
                kind="field", severity="warn", icon="🚧" if binds else "🟢",
                title=("Stage 2 is now a real cut" if binds
                       else "Stage 2 no longer filters anyone"),
                body=f"<b>{passed}</b> passed stage 1 · "
                     f"<b>{cut}</b> advance to evaluation\n"
                     + ("<i>Being valid is no longer enough — stage-2 rank "
                        "decides who gets an eval slot.</i>" if binds else
                        "<i>Every stage-1 survivor gets an evaluation slot. "
                        "Passing the quality screen is the whole game.</i>"),
                dedup_key=f"cut:{new.get('comp_id')}:{binds}"))
        return out

    def _diff_ours(self, old: dict, new: dict, snap: dict) -> list[CompEvent]:
        """One event per real change of situation, keyed on PHASE.

        Not on `status`: a hotkey reads "not qualified" throughout screening and
        again after losing, so a status diff fires constantly and means nothing.
        """
        out = []
        for name, cur in new.items():
            was = old.get(name)
            phase = cur.get("phase") or "screen1"
            icon, human = PHASE.get(phase, ("🔄", phase))

            # A ban already gets its own alert from _diff_board, with the board
            # context this one cannot carry. Two buzzes for one event is how a
            # topic gets muted.
            if was is not None and was.get("phase") != phase and phase != "banned":
                sev, action = "good", ""
                if phase in ("failed1", "out"):
                    sev = "warn"
                    action = ("<i>This hotkey is done for this competition. Its "
                              "upload cannot be replaced.</i>")
                elif phase == "nokey":
                    sev = "critical"
                    action = ("<i>No OpenRouter key registered. Every run will "
                              "score −4.000 until it is added.</i>")
                elif phase == "eval":
                    sev = "warn"
                    action = (f"<i>Full evaluation started — ~{RUNS_EVAL:,} runs "
                              f"≈ {_usd(RUNS_EVAL)} of OpenRouter on THIS key. "
                              f"Check the balance now, not when it fails.</i>")
                elif phase == "scored":
                    sev = "good"

                lines = [f"{esc(PHASE.get(was.get('phase'), ('', '?'))[1])} → "
                         f"<b>{esc(human)}</b>"]
                if phase in ("passed1", "screen2", "eval") and cur.get("s1_score") is not None:
                    line = f"stage 1 <b>{num(cur['s1_score'], 4)}</b>"
                    # Only when the ratios came with it: three em-dashes in a row
                    # reads as a broken message, not as a missing figure.
                    if cur.get("s1_vfy") is not None:
                        line += (f" · vfy {pct(cur['s1_vfy'])}"
                                 f" · exp {pct(cur.get('s1_exp'))}"
                                 f" · edt {pct(cur.get('s1_edt'))}")
                    lines.append(line)
                if phase == "scored":
                    lines.append(f"<b>{num(cur.get('total'), 4)}</b>"
                                 + (f" · rank {cur['rank']}" if cur.get("rank") else "")
                                 + f" · clean bar {num(snap.get('king_score'), 4)}")
                if action:
                    lines.append(action)
                out.append(CompEvent(
                    kind="our_run", severity=sev, icon=icon,
                    title=f"{name} → {human}",
                    body="\n".join(lines),
                    dedup_key=f"{name}:{phase}",
                    detail={"hotkey": cur.get("hk"), "phase": phase}))

            # The credit alarm is independent of any phase change: a key can go
            # dry in the middle of a stage and nothing else about the entry
            # moves. Bucketed so it re-fires only when it gets materially worse.
            cs = _credit_state(cur)
            if cs and (was is None or _credit_state(was) != cs):
                bucket, rate, failed, statused, where = cs
                out.append(CompEvent(
                    kind="credit", severity="critical", icon="🔥",
                    title=f"{name}: {pct(rate, 0)} of runs are FAILING",
                    body=f"<b>{failed:,} of {statused:,}</b> statused "
                         f"{esc(where)} runs failed\n"
                         f"<i>This is the dry-OpenRouter-key signature. Every "
                         f"failed run scores −4.000 and is never retried — top "
                         f"the key up now.</i>",
                    dedup_key=f"credit:{name}:{where}:{bucket}",
                    detail={"hotkey": cur.get("hk"), "rate": rate}))
        return out

    # ---- 3. RENDER ----------------------------------------------------------
    def _ours_sorted(self, s: dict) -> list[tuple[str, dict]]:
        return sorted((s.get("ours") or {}).items(),
                      key=lambda kv: (ORDER.get(kv[1].get("phase"), 9),
                                      _natural(kv[0])))

    @staticmethod
    def _window_line(s: dict) -> str:
        state = s.get("comp_state")
        if state == "upload":
            return (f"<b>🚪 UPLOADS OPEN</b> · closes in "
                    f"<b>{_dur(_hours_until(s.get('upload_end')))}</b>")
        if state == "evaluation":
            return (f"<b>🔥 EVALUATION</b> · ends in "
                    f"{_dur(_hours_until(s.get('eval_end')))}")
        return f"<b>⬛ {esc(str(state or 'unknown')).upper()}</b>"

    def render_state(self, s: dict) -> str:
        L = [f"<b>SN{self.netuid} · {esc(self.label)}</b>",
             f"comp <code>{esc(s.get('comp_id'))}</code> "
             f"{esc(s.get('comp_name') or '')} · updated "
             f"{esc(_ago(s.get('_fetched_at')))}", "",
             self._window_line(s), ""]

        # Our fleet first: on this subnet the only irreversible act is ours.
        n_ours = s.get("n_ours") or 0
        if n_ours:
            L.append(f"<b>🎟 OUR FLEET</b>  {s.get('n_unsent', 0)} of {n_ours} "
                     f"hotkeys unspent")
            if s.get("unsent"):
                L.append(f"   <i>{esc(', '.join(s['unsent'][:9]))}</i>")
            if s.get("runs_left"):
                L.append(f"   {s.get('runs_done', 0):,} runs billed · "
                         f"<b>{s['runs_left']:,} left ≈ {_usd(s['runs_left'])}</b> "
                         f"of OpenRouter")
            if s.get("credit_dry"):
                L.append(f"   🔥 <b>KEY LOOKS DRY: "
                         f"{esc(', '.join(s['credit_dry']))}</b>")
            L.append("")

        if s.get("king_hk"):
            L += [f"<b>👑 CLEAN BAR</b>  <code>{num(s.get('king_score'), 4)}</code>",
                  f"{esc(short(s['king_hk']))} · {s.get('n_ranked', '?')} scored"
                  + (f" · <b>{s['n_negative']} of them negative</b>"
                     if s.get("n_negative") else "")]
            if s.get("n_banned"):
                L.append(f"   ⚖️ {s['n_banned']} banned"
                         + (f", best of them {num(s.get('banned_top'), 4)}"
                            if s.get("banned_top") is not None else "")
                         + " — <i>excluded above</i>")
        elif s.get("n_field"):
            L.append(f"<b>👑 CLEAN BAR</b>  nothing scored yet")
        L.append(f"<b>📥 FIELD</b>  <b>{s.get('n_active', 0)} still competing</b>"
                 f" — of {s.get('n_field', 0)} entered")
        if s.get("n_field"):
            parts = []
            if s.get("n_queued"):
                parts.append(f"{s['n_queued']} queued")
            parts += [f"{s.get('n_screening', 0)} in stage 1",
                      f"{s.get('n_qualified', 0)} past it",
                      f"{s.get('n_scored', 0)} scored",
                      f"{s.get('n_out', 0)} out"]
            L.append("   " + " · ".join(parts))
            if s.get("advance_cut"):
                L.append(f"   <i>{s.get('n_passed1')} passed stage 1, "
                         f"{s['advance_cut']} advance</i>"
                         + ("" if s.get("cut_binds")
                            else " — <i>everyone who passes gets in</i>"))
        elif s.get("comp_state") == "upload":
            ch = s.get("_chain") or {}
            L.append(f"   <i>nobody has uploaded yet — the board fills as they "
                     f"do. {ch.get('unique_coldkeys', '?')} operators hold a uid, "
                     f"which is the only advance warning of the field size.</i>")
        if s.get("n_validators") is not None:
            L.append(f"<b>🧑‍⚖️ VALIDATORS</b>  "
                     f"{s.get('n_validators_working', '?')}/{s['n_validators']} working")
        if s.get("n_reg_ours") is not None:
            line = (f"<b>🪪 REGISTERED</b>  {s['n_reg_ours']}/{s.get('n_ours', '?')} "
                    f"of ours on chain")
            if s.get("dereg_ours"):
                line += f" · ❌ <b>{esc(', '.join(s['dereg_ours']))}</b>"
            L.append(line)
            if s.get("n_zero_incentive"):
                L.append(f"   <i>{s['n_zero_incentive']} of {s.get('n_neurons', '?')} "
                         f"neurons earn nothing — that is the eviction pool, and "
                         f"we are in it</i>")
        if s.get("soma_sha"):
            L.append(f"<b>📦 PLATFORM</b>  <code>{esc(s['soma_sha'])}</code>")
            # What moved SINCE THE ROUND OPENED is the number that decides
            # anything: a candidate is built against the rules as they stood at
            # upload_start, and a per-move alert has long since scrolled away.
            n = s.get("since_n")
            if n:
                _, icon, head = RULE_CLASS.get(s.get("since_class") or "other",
                                               RULE_CLASS["other"])
                L.append(f"   <b>{n} commit{'' if n == 1 else 's'} since the round "
                         f"opened</b> {icon} {esc(head.lower())}")
                for reason in (s.get("since_reasons") or [])[:2]:
                    L.append(f"   ↳ {esc(reason)}")
                if s.get("since_flags"):
                    L.append(f"   ⚠️ <i>{esc('; '.join(s['since_flags'][:2]))}</i>")
            elif s.get("gh_blocked_s"):
                L.append("   ⚠️ <i>GitHub rate-limited — cannot see whether the "
                         "platform moved. Set TAOSCOPE_GITHUB_TOKEN.</i>")
            elif s.get("round_sha"):
                L.append("   <i>unchanged since the round opened</i>")
            else:
                L.append(f"   <i>{esc((s.get('soma_subject') or '')[:60])}</i>")
        L += ["", "<b>🔬 OUR ENTRIES</b>"]
        if not (s.get("ours") or {}):
            L.append("<i>none — <code>/watch &lt;ss58&gt; [name]</code></i>")
        for name, o in self._ours_sorted(s):
            icon, human = PHASE.get(o.get("phase"), ("🔄", "?"))
            line = f"{icon} <code>{esc(name)}</code> uid {o.get('uid') or '—'} · {esc(human)}"
            if o.get("total") is not None:
                line += f" · <b>{num(o['total'], 4)}</b>"
            elif o.get("s1_score") is not None:
                line += f" · s1 {num(o['s1_score'], 4)}"
            L.append(line)
            prog = _progress(o)
            if prog:
                L.append(f"   {esc(prog)}")
        return "\n".join(L)

    def render_info(self, s: dict) -> str:
        ch = s.get("_chain") or {}
        L = [f"<b>SN{self.netuid} · {esc(self.label)} — the rules</b>", "",
             "<b>Competition</b>",
             two_col([("id", esc(s.get("comp_id") or "?")),
                      ("state", esc(s.get("comp_state") or "?")),
                      ("uploads", _dur(_hours_until(s.get("upload_end")))),
                      ("eval ends", _dur(_hours_until(s.get("eval_end"))))], 22),
             f"opened <code>{esc(s.get('upload_start') or '—')}</code>",
             f"closes <code>{esc(s.get('upload_end') or '—')}</code>", "",
             "<b>Money</b>",
             two_col([("prize", f"τ{num(s.get('prize_pool_tao'), 1)}"),
                      ("register", f"τ{num(s.get('reg_cost_tao'), 4)}"),
                      ("α price", f"τ{num(s.get('alpha_price_tao'), 6)}"),
                      ("burn", pct(s.get("burn_ratio"), 0))], 22), "",
             "<b>The three gates</b>",
             two_col([("stage 1", f"~{RUNS_STAGE1} runs"),
                      ("stage 2", f"~{RUNS_STAGE2} runs"),
                      ("eval", f"~{RUNS_EVAL:,} runs"),
                      ("eval cost", f"≈{_usd(RUNS_EVAL)}")], 22),
             "stage 1 is a quality screen and <b>pays nothing</b>. Stage 2 is a "
             "<b>rank</b>, not a threshold: top <code>max(60, 20%)</code> advance. "
             "Final score = evaluation + stage 2; stage 1 is excluded.",
             "", "<b>Scoring</b>",
             "<code>r = clamp(log2(T_base/T_miner), −2, +2)</code> per task, so "
             "the ratio term saturates at 75% saving. Resolution adds "
             "<code>(y−x)/(n−x)</code> where <code>n</code> is the task's run "
             "count — at 26–47 runs a task that is a far smaller lever than the "
             "token ratio. Runs are billed to <b>your</b> OpenRouter key at "
             f"≈${COST_PER_RUN_USD:.3f} each.",
             ""]

        if s.get("prev_comp_id"):
            L.append(f"<b>Last competition</b> — {esc(s.get('prev_comp_name') or '')} "
                     f"(<code>{esc(s['prev_comp_id'])}</code>)")
            L.append("<i>/board that competition from the dashboard for its bar.</i>")
            L.append("")

        if ch:
            L += ["<b>Chain</b>",
                  two_col([("reg", "OPEN" if ch.get("registration_allowed") else "closed"),
                           ("cost", f"τ{num(ch.get('burn_tao'), 4)}"),
                           ("uids", f"{ch.get('num_uids')}/{ch.get('max_uids')}"),
                           ("operators", str(ch.get("unique_coldkeys"))),
                           ("emission", pct(ch.get("emission_share"))),
                           ("τ/day", num((ch.get("realized_tao_per_hour") or 0) * 24, 2))],
                          22), ""]

        if s.get("soma_sha"):
            L.append(f"📦 <code>{esc(self.RULES_REPO)}</code> "
                     f"<code>{esc(s['soma_sha'])}</code>")
            L.append(f"   <i>{esc((s.get('soma_subject') or '')[:64])}</i>")
        if s.get("gh_blocked_s"):
            L += ["", "⚠️ <b>Platform watch is degraded</b> — GitHub is "
                      "rate-limiting us (60/hr unauthenticated, shared across "
                      "every tracked subnet). Set <code>TAOSCOPE_GITHUB_TOKEN</code> "
                      "for 5,000/hr."]
        if s.get("round_sha"):
            n = s.get("since_n") or 0
            _, icon, head = RULE_CLASS.get(s.get("since_class") or "other",
                                           RULE_CLASS["other"])
            L += ["", f"<b>Since this round opened</b> "
                      f"(<code>{esc(s['round_sha'])}</code>"
                      + (f", {esc((s.get('round_sha_at') or '')[:10])}"
                         if s.get("round_sha_at") else "") + ")"]
            if not n:
                L.append("nothing has moved — you are competing under the same "
                         "platform you built against.")
            else:
                L.append(f"{n} commit{'' if n == 1 else 's'} {icon} "
                         f"<b>{esc(head.lower())}</b>")
                for reason in (s.get("since_reasons") or []):
                    L.append(f"  ↳ {esc(reason)}")
                for f_ in (s.get("since_files") or [])[:6]:
                    L.append(f"  <code>{esc(f_.rsplit('/', 1)[-1])}</code>")
                for flag in (s.get("since_flags") or []):
                    L.append(f"  ⚠️ {esc(flag)}")
                if s.get("since_compare"):
                    L.append(esc(s["since_compare"]))
        for r in (s.get("_repos") or []):
            L.append(f"📦 <code>{esc(r['repo'])}</code> "
                     f"<code>{esc((r.get('sha') or '')[:10])}</code>")
            if r.get("subject"):
                L.append(f"   <i>{esc((r['subject'] or '')[:64])}</i>")
        if s.get("_repos") or s.get("soma_sha"):
            L.append("")
        L += [f'<a href="{esc(v)}">{esc(k)}</a>' for k, v in self.links.items()]
        return "\n".join(L)

    def render_board(self, s: dict, limit: int = 10) -> str:
        board = s.get("board") or []
        if not board:
            state = s.get("comp_state")
            if state == "upload":
                ch = s.get("_chain") or {}
                return (f"Nothing scored yet — comp {esc(s.get('comp_id'))} is still "
                        f"taking uploads.\n\n"
                        f"<b>{s.get('n_field', 0)}</b> have uploaded · closes in "
                        f"<b>{_dur(_hours_until(s.get('upload_end')))}</b>\n"
                        f"<b>{ch.get('unique_coldkeys', '?')}</b> operators hold a "
                        f"uid"
                        + (f" · <b>{ch.get('earning_coldkeys')}</b> of them earn"
                           if ch.get("earning_coldkeys") is not None else "")
                        + "\n<i>Until miners upload, the operator count is the only "
                          "advance signal of how big this field will be.</i>")
            return "Board unavailable."
        ours = {o.get("hk") for o in (s.get("ours") or {}).values()}
        rows = [f"{'#':<3}{'score':>8} {'vfy':>7} {'edt':>7} {'exp':>7}"]
        for r in board[:limit]:
            c = r.get("cats") or {}
            mark = "◀" if r.get("hk") in ours else ""
            rows.append(f"{r['rank']:<3}{num(r.get('score'), 4):>8} "
                        f"{num(c.get('swebench_verified'), 3):>7} "
                        f"{num(c.get('swe_explorer_edit'), 3):>7} "
                        f"{num(c.get('swe_explorer_explore'), 3):>7}{mark}")
        L = [f"<b>SN{self.netuid} · comp {esc(s.get('comp_id'))}</b> — "
             f"{s.get('n_ranked', len(board))} scored · "
             f"{s.get('n_active', 0)} still competing of "
             f"{s.get('n_field', '?')} entered",
             "", "<pre>" + "\n".join(rows) + "</pre>"]
        if s.get("n_negative"):
            L.append(f"<b>{s['n_negative']}</b> of {s.get('n_ranked')} scored "
                     f"<b>below zero</b> — most compressors cost more than they save "
                     f"on the evaluation set.")
        if s.get("n_banned"):
            L.append(f"⚖️ <b>{s['n_banned']} entries are banned</b> "
                     f"(<code>failed review</code>) and are excluded above"
                     + (f"; the best of them scored {num(s.get('banned_top'), 4)}, "
                        f"above this whole table" if (s.get("banned_top") or -9) >
                        (s.get("king_score") or 9) else ""))
        return "\n".join(L)

    def render_me(self, s: dict) -> str:
        ours = self._ours_sorted(s)
        if not ours:
            return ("Nothing tracked yet.\n"
                    "<code>/watch &lt;hotkey_ss58&gt; [name]</code>, or write "
                    "<code>artifacts/our_hotkeys.json</code> in the miner "
                    "workspace.")
        L = [f"<b>Our SN{self.netuid} hotkeys</b> · comp "
             f"<code>{esc(s.get('comp_id'))}</code> · "
             f"{s.get('n_unsent', 0)} unspent"]
        if s.get("n_reg_ours") is not None:
            L.append(f"{s['n_reg_ours']}/{s.get('n_ours', '?')} registered · "
                     f"a hotkey with no incentive is in the eviction pool "
                     f"({s.get('n_zero_incentive', '?')} of "
                     f"{s.get('n_neurons', '?')} neurons) while the subnet is "
                     f"full and registration is open")
        L.append("")
        for name, o in ours:
            icon, human = PHASE.get(o.get("phase"), ("🔄", "?"))
            L.append(f"{icon} <code>{esc(name)}</code> · uid <b>{o.get('uid') or '—'}</b>"
                     f" · <b>{esc(human)}</b>")
            reg = o.get("registered")
            # An inline marker, not a sentence: the explanation belongs in the
            # one summary line above, not repeated on eight of nine rows.
            if reg is None:
                regtag = ""
            elif not reg:
                regtag = " · ❌ <b>DEREGISTERED</b>"
            elif not (o.get("incentive") or 0):
                regtag = " · 🪪 registered <i>(no incentive)</i>"
            else:
                regtag = " · 🪪 registered · earning"
            L.append(f"   <code>{esc(short(o.get('hk'), 10, 4))}</code>{regtag}"
                     + (f" · uploaded {esc(_ago(o.get('submit')))}"
                        if o.get("submit") else ""))
            if o.get("s1_score") is not None:
                L.append(f"   stage 1 <b>{num(o['s1_score'], 4)}</b> "
                         f"{'PASS' if o.get('s1_passed') else 'FAIL'} · "
                         f"vfy {pct(o.get('s1_vfy'))} · exp {pct(o.get('s1_exp'))} · "
                         f"edt {pct(o.get('s1_edt'))}")
            elif o.get("phase") == "screen1":
                L.append("   stage 1 <i>still screening — no verdict yet, and a "
                         "score read now would be meaningless</i>")
            if o.get("s2_score") is not None:
                L.append(f"   stage 2 <b>{num(o['s2_score'], 4)}</b>"
                         + ("" if _stage_complete(o, "s2")
                            else " <i>(incomplete — not a final figure)</i>"))
            if o.get("total") is not None:
                L.append(f"   <b>final {num(o['total'], 4)}</b>"
                         + (f" · rank {o['rank']}" if o.get("rank") else ""))
                c = o.get("cats") or {}
                if c:
                    L.append(two_col([("vfy", num(c.get("swebench_verified"), 3)),
                                      ("edt", num(c.get("swe_explorer_edit"), 3)),
                                      ("exp", num(c.get("swe_explorer_explore"), 3))],
                                     13))
            for bucket, tag in (("s1", "stage 1"), ("s2", "stage 2"), ("eval", "eval")):
                if not o.get(f"runs_{bucket}"):
                    continue
                total = o[f"runs_{bucket}"]
                done = o.get(f"done_{bucket}") or 0
                failed = o.get(f"failed_{bucket}") or 0
                line = (f"   {tag}: {done:,}/{total:,} runs · "
                        f"{_usd(done)} billed")
                if failed:
                    line += f" · <b>{failed:,} FAILED</b>"
                L.append(line)
            if _saving(o) is not None:
                L.append(f"   pooled saving <b>{pct(_saving(o))}</b> "
                         f"<i>(all runs executed)</i>")
            elif o.get("tok_base"):
                L.append("   pooled saving <i>— withheld: runs still executing, so "
                         "the baseline is billed and the miner is not</i>")
            if o.get("source_public"):
                L.append("   ⚠️ <i>source code is PUBLIC on the dashboard</i>")
            L.append("")
        return "\n".join(L).rstrip()

    def ours_summary(self, s: dict) -> str:
        bits = []
        if s.get("credit_dry"):
            bits.append(f"🔥 key dry: {', '.join(s['credit_dry'])}")
        if s.get("n_unsent"):
            bits.append(f"{s['n_unsent']} hotkeys unspent")
        if s.get("n_mine"):
            bits.append(f"{s['n_mine']} in flight")
        return " · ".join(bits)

    def render_digest(self, s: dict) -> str:
        head = self._window_line(s).replace("<b>", "").replace("</b>", "")
        tail = ""
        if s.get("credit_dry"):
            tail = f" · 🔥 <b>key dry: {esc(', '.join(s['credit_dry']))}</b>"
        elif s.get("n_unsent"):
            tail = f" · <b>{s['n_unsent']} hotkeys unspent</b>"
        return (f"<b>SN{self.netuid}</b> {esc(self.label)} — {esc(head)} · "
                f"👑 <code>{num(s.get('king_score'), 4)}</code> · "
                f"<b>{s.get('n_active', 0)} competing</b>"
                f"/{s.get('n_field', 0)} entered{tail}")


# --------------------------------------------------------------------------
def _ranked(pairs: list[tuple[str, str]], limit: int) -> list[str]:
    """Dedupe, most-consequential class first, stable inside a class."""
    seen, out = set(), []
    for value, cls in sorted(pairs, key=lambda kv: (RULE_RANK.get(kv[1], 9), kv[0])):
        if value not in seen:
            seen.add(value)
            out.append(value)
    return out[:limit]


def _fresh(old: dict, new: dict, key: str) -> list:
    """Items in new[key] that were not in old[key] — [] on first sight.

    `changed()` protects against a key VANISHING (an outage). This protects
    against one APPEARING: the first time an adapter upgrade adds a field, or
    the first poll after a state wipe, `old` has no such key and every item in
    it reads as new. That is how three identical "None -> ec05da4e9a" alerts
    reached the chat across three restarts. First sight of a field is a
    baseline for the same reason first sight of a snapshot is, and the
    `if not old` guard at the top of diff() does not cover it.
    """
    if key not in old or key not in new:
        return []
    was = set(old.get(key) or [])
    return [x for x in (new.get(key) or []) if x not in was]


def _natural(name: str) -> tuple:
    """sn114-4 before sn114-10. Plain string order puts sn114-100 first."""
    return tuple(int(p) if p.isdigit() else p for p in re.split(r"(\d+)", name))


def _stage_complete(o: dict, bucket: str) -> bool:
    total, done = o.get(f"runs_{bucket}"), o.get(f"done_{bucket}")
    return bool(total) and done is not None and done >= total


def _saving(o: dict):
    """Pooled saving, or None while it would be a lie.

    Only meaningful once every run has executed: the platform bills the baseline
    for rows the miner has not reached yet, so a mid-stage figure is inflated by
    exactly the work not yet done.
    """
    base, mine = o.get("tok_base"), o.get("tok_mine")
    if not base or mine is None:
        return None
    live = [b for b in ("s1", "s2", "eval") if o.get(f"runs_{b}")]
    if not live or not all(_stage_complete(o, b) for b in live):
        return None
    return 1.0 - (mine / base)


def _credit_state(o: dict):
    """(bucket, rate, failed, statused, where) when the runs look starved.

    In comp 112 our advancing entry failed 998 of 1,940 statused evaluation runs
    while every rival failed 0–3 of 1,940. The failures were ordered by run_id
    with a single partially-completed task at the boundary — the signature of a
    resource cutoff, not of a code bug, and our own notes had recorded $10.93
    left on the OpenRouter account twelve days earlier.

    Bucketed to a decile so the alert re-fires when it gets materially worse and
    not on every poll.
    """
    for bucket in ("eval", "s2", "s1"):
        statused = o.get(f"statused_{bucket}") or 0
        failed = o.get(f"failed_{bucket}") or 0
        if statused < CREDIT_MIN_RUNS:
            continue
        rate = failed / statused
        if rate >= CREDIT_FAIL_RATE:
            return (int(rate * 10), rate, failed, statused, bucket)
    return None


def _progress(o: dict) -> str:
    """The one line of run progress worth putting under a /state row."""
    for bucket, tag in (("eval", "eval"), ("s2", "stage 2"), ("s1", "stage 1")):
        total = o.get(f"runs_{bucket}")
        if not total:
            continue
        done = o.get(f"done_{bucket}") or 0
        failed = o.get(f"failed_{bucket}") or 0
        out = f"{tag} {done:,}/{total:,} runs · {_usd(done)} billed"
        if failed:
            out += f" · {failed:,} failed"
        return out
    return ""
