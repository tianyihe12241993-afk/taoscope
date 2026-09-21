"""SN67 Harnyx -- deep-research agents, one cascade per day.

WHAT THIS SUBNET ACTUALLY IS
----------------------------
Miners upload a Python agent. Every day at 15:00Z a batch closes and the whole
field is run against the same tasks. There are two stages inside one batch:

    qualifying  ~10 tasks over the WHOLE field (250+ artifacts) -- a selection
                tournament. Surviving it is not winning anything.
    main        20 tasks over 1-4 artifacts -- the qualifying winner plus the
                previous incumbent. THIS is where the crown is contested.

Dethroning is a cascade of three doors, checked in order:

    score_margin      +0.10 ABSOLUTE over the incumbent
    cost_reduction    <=0.90x   (requires score non-regressing)
    runtime_reduction <=0.90x   (requires score non-regressing)

So an agent can lose on score and still take the crown by being cheaper -- our
only crown to date came through the runtime door.

WHY THIS ADAPTER LOOKS DIFFERENT FROM THE OTHERS
------------------------------------------------
1. The API is MCP JSON-RPC over POST, not REST, so `fetch_json` (GET-only)
   cannot be used. `_rpc()` below is a small async client for it.

2. `get_miner` OMITS any artifact not yet bound to a batch. During an open or
   still-initializing window it therefore returns NOTHING you just submitted,
   and a tracker built on it reports an empty window as if nothing landed.
   `get_latest_submissions` (latest row per miner, network-wide) is the only
   view of the open window -- and since cascade order IS submission order, its
   ordering is also the running cascade-position list.

3. The uid on a submission row is the uid AT SUBMISSION TIME, frozen there. It
   is NOT a liveness check. On 2026-08-20 two of our hotkeys were deregistered
   while their artifacts sat in a sealed batch, and every receipt still read
   uid 192 / uid 53. `our_hk` below is resolved from OUR OWN chain tables
   instead, so a hotkey falling off the chain is a critical alert rather than
   something noticed days later.

4. `status` is not progress, the same trap SN100 has. A batch sits in
   `checking_for_duplicates` for ~13h after cutoff before it prepares tasks,
   then completes ~40 min later. What is real is `stage_progress`: per-validator
   resolved/total counts and an ETA. We report THAT, and a batch that has been
   "initializing" for three hours produces no alert at all.
"""
from __future__ import annotations

import asyncio
import datetime as dt
import json
import logging
from pathlib import Path

import httpx

from ..base import (CompEvent, SubnetAdapter, changed, esc, num, short,
                    two_col)

log = logging.getLogger("taoscope.comp.sn67")

API = "https://api.harnyx.ai/mcp"
SITE = "https://harnyx.ai"

# Read-only mount of the mining workspace (docker-compose: /home/dev/work:/work:ro).
# The ACTIVE workspace moved to sn67-new on 2026-08-22. This still pointed at
# sn67-harnyx, whose receipts froze on 08-21, so every `our_entry` alert since
# has been titled with an 8-char hash instead of a build name. The submitter
# (`sn67-new/tools/autosubmit.py`) appends a receipt per firing, so the names
# are live again from here.
WORK = Path("/work/sn67-new")
RECEIPTS = WORK / "runs" / "SUBMISSIONS.json"

# The daily cutoff, UTC. Everything about this subnet keys off it.
CUTOFF_HOUR = 15

# At or below this many healthy validators, a batch is in trouble. Above it,
# the count is noise: SN67's quorum flapped 4<->5 all day every day, 219 alerts
# in seven days -- 72% of everything this topic had ever posted, half of them
# buzzing a phone for a number that healed itself within the hour. Nothing about
# it was actionable; we cannot fix someone else's validator, and the live count
# is already on the /state deploy-pin line for anyone who wants it.
#
# A COLLAPSE is different. Each healthy validator is one judge draw and a task's
# score is the median across them, so a quorum this low means a batch that
# either stalls or is scored on too few draws to believe. That changes whether a
# result is worth acting on, which is the only thing that earns an alert.
VAL_FLOOR = 2


class _MCP:
    """Minimal async Streamable-HTTP MCP client.

    One session is negotiated and reused; any failure drops it so the next call
    re-initialises rather than retrying forever against a dead session id.
    Every method returns None on failure -- never {} -- so that snapshot() can
    OMIT the key and `changed()` can keep an outage silent.
    """

    def __init__(self, url: str) -> None:
        self.url = url
        self._sid: str | None = None

    async def _post(self, cx: httpx.AsyncClient, payload: dict):
        headers = {"Content-Type": "application/json",
                   "Accept": "application/json, text/event-stream"}
        if self._sid:
            headers["mcp-session-id"] = self._sid
        r = await cx.post(self.url, json=payload, headers=headers)
        sid = r.headers.get("mcp-session-id")
        if sid:
            self._sid = sid
        # A JSON-RPC *notification* has no reply, and the server answers it
        # with 202 Accepted and an empty body. Treating anything but 200 as a
        # failure kills the handshake on its second call, before any tool runs.
        if r.status_code not in (200, 202):
            raise RuntimeError(f"HTTP {r.status_code}")
        raw = r.text
        if not raw.strip():
            return None
        # Streamable HTTP may answer with SSE frames instead of a JSON body.
        if raw.lstrip().startswith("event:") or "\ndata: " in raw or raw.startswith("data: "):
            last = None
            for line in raw.splitlines():
                if line.startswith("data: "):
                    last = json.loads(line[6:])
            return last
        return json.loads(raw)

    async def _init(self, cx: httpx.AsyncClient) -> None:
        await self._post(cx, {"jsonrpc": "2.0", "id": 1, "method": "initialize",
                              "params": {"protocolVersion": "2025-06-18",
                                         "capabilities": {},
                                         "clientInfo": {"name": "taoscope",
                                                        "version": "1.0"}}})
        await self._post(cx, {"jsonrpc": "2.0", "method": "notifications/initialized"})

    @staticmethod
    def _unwrap(resp):
        if not resp:
            return None
        if resp.get("error"):
            return None
        result = resp.get("result") or {}
        for part in result.get("content") or []:
            if part.get("type") == "text":
                try:
                    return json.loads(part["text"])
                except ValueError:
                    return part["text"]
        return result.get("structuredContent") or None

    async def call(self, name: str, args: dict | None = None, *, timeout: float = 45.0):
        try:
            async with httpx.AsyncClient(timeout=timeout) as cx:
                if not self._sid:
                    await self._init(cx)
                resp = await self._post(cx, {"jsonrpc": "2.0", "id": 2,
                                             "method": "tools/call",
                                             "params": {"name": name,
                                                        "arguments": args or {}}})
            data = self._unwrap(resp)
            # The platform answers some queries with {"error": {...}} INSIDE a
            # 200 -- e.g. get_champion -> weights_unavailable. That is an
            # unknown, not an empty result.
            if isinstance(data, dict) and data.get("error"):
                log.debug("sn67 %s -> %s", name, data["error"].get("code"))
                return None
            return data
        except Exception as exc:  # noqa: BLE001
            log.debug("sn67 %s failed: %s", name, exc)
            self._sid = None
            return None


def _iso(v: str | None) -> dt.datetime | None:
    if not v:
        return None
    try:
        return dt.datetime.fromisoformat(str(v).replace("Z", "+00:00"))
    except ValueError:
        return None


def _ms(v) -> str:
    """4,793,362ms is unreadable on a phone. -> '79m 53s'."""
    try:
        total = int(float(v) // 1000)
    except (TypeError, ValueError):
        return "—"
    h, rem = divmod(total, 3600)
    m, sec = divmod(rem, 60)
    return f"{h}h {m:02d}m" if h else f"{m}m {sec:02d}s"


# Platform stage -> something that fits a phone column. "running" is the only
# one that costs wall-clock; the rest are the batch queueing in front of it.
_STAGE = {
    "preparing_tasks": "preparing",
    "checking_for_duplicates": "dedup check",
    "running_and_scoring_tasks": "RUNNING",
    "finalizing_results": "finalizing",
    "initializing": "initializing",
}


def _stage(v: str | None) -> str:
    return _STAGE.get(v or "", (v or "—"))


# What `stage_progress.validators[].total_count` is COUNTING depends on the
# stage, and the two differ by an order of magnitude: 272 artifacts during the
# duplicate check, 2,670 task-runs once scoring starts (272 artifacts x ~10
# qualifying tasks). Labelling both "field" reported a field 10x its real size.
_UNIT = {
    "checking_for_duplicates": "artifacts",
    "running_and_scoring_tasks": "runs",
}


def _unit(stage: str | None) -> str:
    return _UNIT.get(stage or "", "items")


def _usd(v) -> str:
    try:
        return f"${float(v):,.2f}"
    except (TypeError, ValueError):
        return "—"


class SN67(SubnetAdapter):
    # ---- identity -----------------------------------------------------------
    netuid = 67
    # our own deregistration alert is richer than the generic one
    covers = frozenset({"dereg"})
    slug = "harnyx"
    label = "Harnyx"

    # One batch a day. Five minutes is plenty for the batch state machine, and
    # keeps us far under the API's rate limit (it 429s on bursts): steady state
    # is 3 calls per poll, ~36/hour.
    poll_seconds = 300

    # The subnet's own repo. `feat(emission): adopt version 9 champion and
    # novelty policy` landed here at 07:51Z on 2026-08-20, hours before that
    # day's cutoff, with nothing watching it.
    repos: list[str] = ["harnyx/harnyx"]

    links = {"site": SITE, "api": API}

    alerts = {
        "deploy": "the validator build judging us changed -- merged is not "
                  "deployed, and this is the deploy",
        "batch_done": "a race finished; our scores and the crown",
        "king_change": "the champion artifact changed hands",
        "our_entry": "one of our artifacts entered the open window, with its "
                     "cascade position",
        "our_hotkey": "a hotkey of ours left the chain -- anything it submitted "
                      "is on a dead slot",
        "batch_stage": "the batch moved to a stage that actually spends time",
        "validators": "the healthy validator quorum COLLAPSED -- too few "
                      "judges left for a score to mean much. Routine flapping "
                      "is not reported; /state has the live count",
    }

    def __init__(self) -> None:
        self._mcp = _MCP(API)

    # ---- 1. COLLECT ---------------------------------------------------------
    async def snapshot(self) -> dict:
        s: dict = {}

        batches = await self._mcp.call("list_miner_task_batches", {"limit": 6})
        rows = (batches or {}).get("batches") or []
        live = next((b for b in rows if b.get("status") != "completed"), None)
        done = next((b for b in rows if b.get("status") == "completed"), None)

        if live:
            s["batch_id"] = live.get("batch_id")
            s["batch_status"] = live.get("status")
            s["batch_cutoff"] = live.get("cutoff_at")
            prog = live.get("stage_progress") or {}
            # The stage the batch is REALLY in, and how far through it is.
            # `status` alone reads "initializing" for hours and says nothing.
            s["batch_stage"] = prog.get("stage") or live.get("status")
            if prog.get("slowest_validator_percent_complete") is not None:
                s["batch_pct"] = round(float(prog["slowest_validator_percent_complete"]), 1)
            if prog.get("eta_seconds") is not None:
                s["batch_eta_min"] = int(float(prog["eta_seconds"]) // 60)
            vs = prog.get("validators") or []
            if vs:
                # total_count is the artifact count for this batch -- the field
                # size, available long before artifact_count is populated.
                s["batch_field"] = max(int(v.get("total_count") or 0) for v in vs)
                s["batch_validators"] = len(vs)

        if done:
            s["last_batch_id"] = done.get("batch_id")
            s["last_completed_at"] = done.get("completed_at")
            s["last_field"] = done.get("artifact_count")
            s["last_champion"] = done.get("champion_artifact_id")

        vals = await self._mcp.call("get_validators")
        if vals and vals.get("validator_endpoints"):
            eps = vals["validator_endpoints"]
            h = vals.get("validator_health") or {}
            s["val_healthy"] = h.get("healthy")
            s["val_unhealthy"] = h.get("unhealthy")
            s["val_unknown"] = h.get("unknown")
            # THE DEPLOY PIN. Only healthy validators judge anything, and they
            # move together: on 2026-08-20 all five healthy ones ran
            # 20260820.post5 while the three unhealthy ones sat on builds from
            # April, May and July. Taking the modal version across the healthy
            # set is therefore the version that is actually scoring us -- which
            # is the thing to watch, not whatever landed on main.
            healthy = [e for e in eps if e.get("health_status") == "healthy"]
            if healthy:
                vers = [e.get("validator_version") for e in healthy if e.get("validator_version")]
                if vers:
                    s["val_version"] = max(set(vers), key=vers.count)
                revs = [e.get("source_revision") for e in healthy if e.get("source_revision")]
                if revs:
                    s["val_revision"] = max(set(revs), key=revs.count)
            # OUTSIDE the `if healthy` above, and that is the whole point: a
            # quorum of zero is a MEASURED zero, not an unknown, and it is the
            # single most important value this field ever takes. Guarded by
            # `if healthy` it was simply absent, `changed()` saw no change, and
            # the collapse alert below could never fire at the one moment it is
            # for. The pin stays inside the guard -- with nothing healthy the
            # version really IS unknown, and absence is the correct answer.
            s["val_quorum"] = len(healthy)
            s["val_table"] = sorted(
                [{"hk": e.get("hotkey") or "", "ver": e.get("validator_version") or "",
                  "rev": (e.get("source_revision") or "")[:10],
                  "health": e.get("health_status") or "",
                  "name": ((e.get("chain_identity") or {}).get("name") or "")[:22]}
                 for e in eps], key=lambda r: (r["health"] != "healthy", r["name"]))

        await self._open_window(s)
        await self._ours(s)
        return s

    # ---- our hotkeys, from OUR OWN chain tables -----------------------------
    async def _our_hotkeys(self) -> dict[str, int]:
        """{hotkey_ss58: uid} for every SN67 slot our coldkeys hold.

        Resolved by joining `my_coldkey` to `neuron_live`, which the chain
        collector already refreshes every 15 minutes. Two reasons not to keep a
        hardcoded hotkey list here: it drifts (the miner's own tracker silently
        omitted a hotkey for a full day of submissions because its map was
        hand-maintained), and a deregistered hotkey simply vanishes from
        neuron_live, which is exactly the signal we want.
        """
        from ...db import pool
        try:
            rows = await pool().fetch(
                "SELECT n.uid, n.hotkey FROM neuron_live n"
                " WHERE n.netuid=$1 AND n.coldkey IN (SELECT coldkey FROM my_coldkey)",
                self.netuid)
        except Exception as exc:  # noqa: BLE001
            log.debug("sn67 hotkey lookup failed: %s", exc)
            return {}
        return {r["hotkey"]: r["uid"] for r in rows if r["hotkey"]}

    def _versions(self) -> dict[str, str]:
        """artifact_id -> build name, straight from the miner's own receipts.

        Nothing is typed in twice, and the file is small. A missing or broken
        file is not an error -- the ids just render as hash prefixes.
        """
        try:
            data = json.loads(RECEIPTS.read_text())
        except Exception:  # noqa: BLE001
            return {}
        subs = data.get("submissions") if isinstance(data, dict) else data
        out = {}
        for r in subs or []:
            if r.get("artifact_id"):
                out[r["artifact_id"]] = r.get("version") or ""
        return out

    async def _open_window(self, s: dict) -> None:
        """Everything in the window that has not closed yet.

        get_latest_submissions is one row per miner (the latest), network-wide
        and in submission order -- which is cascade order. This is the ONLY
        endpoint that can see an open window; get_miner cannot.
        """
        data = await self._mcp.call("get_latest_submissions")
        rows = (data or {}).get("rows")
        if not rows:
            return
        cutoff = _last_cutoff()
        window = sorted([r for r in rows if (_iso(r.get("submitted_at")) or cutoff) > cutoff],
                        key=lambda r: r["submitted_at"])
        s["open_n"] = len(window)
        s["open_cutoff"] = _next_cutoff().isoformat()

        ours = await self._our_hotkeys()
        s["our_hk"] = sorted(ours)
        s["our_uids"] = {h: u for h, u in sorted(ours.items())}
        names = self._versions()
        mine = []
        for i, r in enumerate(window, 1):
            if r.get("miner_hotkey_ss58") not in ours:
                continue
            mine.append({"pos": i, "uid": r.get("uid"),
                         "art": (r.get("artifact_id") or "")[:8],
                         "at": r.get("submitted_at"),
                         "name": names.get(r.get("artifact_id"), "")})
        s["open_ours"] = mine

    async def _ours(self, s: dict) -> None:
        """Our results in the last COMPLETED batch.

        Only refetched when the completed batch id actually moves -- the board
        is immutable once a batch finishes, and re-pulling it every five
        minutes is how you meet the rate limiter.
        """
        from .. import store
        prev = await store.last_state(self.netuid)
        bid = s.get("last_batch_id")
        if not bid:
            return
        if prev.get("last_batch_id") == bid and prev.get("board"):
            for k in ("board", "board_uids", "champ_uid", "champ_artifact",
                      "champ_score", "champ_cost", "champ_ms", "n_main", "ours_last"):
                if k in prev:
                    s[k] = prev[k]
            return

        names = self._versions()
        main = await self._mcp.call("list_miner_task_batch_artifact_comparisons",
                                    {"batch_id": bid, "evaluation_stage": "main"})
        rows = [r for r in ((main or {}).get("artifact_comparisons") or [])
                if r.get("selection_metrics")]
        if rows:
            board = []
            for r in rows:
                m = r["selection_metrics"] or {}
                board.append({
                    "uid": r.get("miner_uid"),
                    "art": (r.get("artifact_id") or "")[:8],
                    "score": m.get("selection_score"),
                    "cost": m.get("actual_selection_median_cost_usd"),
                    "ms": m.get("selection_median_elapsed_ms"),
                    "champ": bool(r.get("is_champion")),
                    "inc": bool(r.get("is_incumbent")),
                    "name": names.get(r.get("artifact_id"), ""),
                })
            board.sort(key=lambda r: (r["score"] is None, -(r["score"] or 0)))
            s["board"] = board
            s["n_main"] = len(board)
            s["board_uids"] = sorted({r["uid"] for r in board if r["uid"] is not None})
            champ = next((r for r in board if r["champ"]), None)
            if champ:
                s["champ_uid"] = champ["uid"]
                s["champ_artifact"] = champ["art"]
                s["champ_score"] = champ["score"]
                s["champ_cost"] = champ["cost"]
                s["champ_ms"] = champ["ms"]

        # Our own qualifying scores in that batch. get_miner IS the right call
        # here -- the batch is closed, so its artifacts are bound and visible.
        ours: dict = {}
        for hk in (s.get("our_hk") or []):
            d = await self._mcp.call("get_miner", {"miner_hotkey_ss58": hk, "limit": 4})
            for sub in (d or {}).get("submissions") or []:
                a = sub.get("artifact") or {}
                for c in sub.get("batch_contexts") or []:
                    if (c.get("batch_summary") or {}).get("batch_id") != bid:
                        continue
                    for st in c.get("stages") or []:
                        ours[(a.get("artifact_id") or "")[:8]] = {
                            "uid": a.get("uid"),
                            "hk": hk,
                            "stage": st.get("evaluation_stage"),
                            "score": st.get("final_score"),
                            "name": names.get(a.get("artifact_id"), ""),
                        }
            await asyncio.sleep(1.5)   # the API 429s on bursts
        if ours:
            s["ours_last"] = ours

    # ---- 2. DIFF ------------------------------------------------------------
    def diff(self, old: dict, new: dict) -> list[CompEvent]:
        out: list[CompEvent] = []
        if not old:
            return out

        # THE DEPLOY PIN. A commit on main changes nothing; this is the moment
        # the build that scores us actually changed.
        if changed(old, new, "val_version") or changed(old, new, "val_revision"):
            out.append(CompEvent(
                kind="deploy", severity="critical", icon="🛠",
                title="Validator build changed",
                body=(f"<code>{esc(old.get('val_version'))}</code> → "
                      f"<code>{esc(new.get('val_version'))}</code>\n"
                      f"rev <code>{esc((old.get('val_revision') or '')[:10])}</code> → "
                      f"<code>{esc((new.get('val_revision') or '')[:10])}</code>\n"
                      f"{new.get('val_quorum')} healthy validators on it\n"
                      "<i>this is the build that will score the next batch</i>"),
                dedup_key=f"{new.get('val_version')}:{new.get('val_revision')}",
            ))

        # A race finished. This is the one alert worth waking up for.
        if changed(old, new, "last_batch_id"):
            L = []
            for ref, o in (new.get("ours_last") or {}).items():
                L.append(f"<code>{esc(ref)}</code> {esc(o.get('name') or '')} "
                         f"uid {o.get('uid')} · {esc(o.get('stage'))} "
                         f"<b>{num(o.get('score'))}</b>")
            body = "\n".join(L) or "<i>nothing of ours in this batch</i>"
            if new.get("champ_uid") is not None:
                body += (f"\n\n👑 uid {new['champ_uid']} "
                         f"<code>{esc(new.get('champ_artifact'))}</code> "
                         f"{num(new.get('champ_score'))} · "
                         f"{_usd(new.get('champ_cost'))} · {_ms(new.get('champ_ms'))}")
            out.append(CompEvent(
                kind="batch_done", severity="warn", icon="🏁",
                title=f"Race finished — {new.get('last_field') or '?'} artifacts",
                body=body, dedup_key=str(new.get("last_batch_id")),
            ))

        if changed(old, new, "champ_uid") and old.get("champ_uid") is not None:
            out.append(CompEvent(
                kind="king_change", severity="critical", icon="👑",
                title="The crown moved",
                body=(f"uid {old.get('champ_uid')} → <b>uid {new.get('champ_uid')}</b>\n"
                      f"{num(new.get('champ_score'))} · {_usd(new.get('champ_cost'))} · "
                      f"{_ms(new.get('champ_ms'))}"),
                dedup_key=str(new.get("champ_uid")),
            ))

        # A hotkey of ours left the chain. Everything it has in a sealed batch
        # is sitting on a dead slot, and no receipt anywhere says so.
        if changed(old, new, "our_hk"):
            gone = [h for h in (old.get("our_hk") or []) if h not in set(new["our_hk"])]
            added = [h for h in new["our_hk"] if h not in set(old.get("our_hk") or [])]
            if gone:
                out.append(CompEvent(
                    kind="our_hotkey", severity="critical", icon="💀",
                    title=f"{len(gone)} of our hotkeys deregistered",
                    body="\n".join(f"<code>{esc(short(h))}</code> "
                                   f"(was uid {(old.get('our_uids') or {}).get(h, '?')})"
                                   for h in gone) +
                         f"\n\n{len(new['our_hk'])} slots left\n"
                         "<i>anything it submitted is on a dead slot</i>",
                    dedup_key=",".join(sorted(gone)),
                ))
            if added:
                out.append(CompEvent(
                    kind="our_hotkey", severity="good", icon="🔑",
                    title=f"{len(added)} new hotkey registered",
                    body="\n".join(f"<code>{esc(short(h))}</code> "
                                   f"uid {(new.get('our_uids') or {}).get(h, '?')}"
                                   for h in added),
                    dedup_key=",".join(sorted(added)),
                ))

        # One of ours entered the open window. Position matters enough to say.
        was = {r["art"] for r in (old.get("open_ours") or [])}
        for r in (new.get("open_ours") or []):
            if r["art"] in was:
                continue
            out.append(CompEvent(
                kind="our_entry", severity="good", icon="📤",
                title=f"Submitted — {r.get('name') or r['art']}",
                body=(f"<code>{esc(r['art'])}</code> uid {r.get('uid')}\n"
                      f"cascade position <b>{r['pos']}</b> of {new.get('open_n', '?')}\n"
                      f"{esc(r.get('at') or '')}"),
                dedup_key=r["art"],
            ))

        # Stage transitions only. `status` flapping while a batch waits is not
        # news; entering the stage that actually runs the tasks is.
        if changed(old, new, "batch_stage"):
            stage = new.get("batch_stage") or ""
            runs = stage == "running_and_scoring_tasks"
            out.append(CompEvent(
                kind="batch_stage", severity="warn" if runs else "info",
                icon="▶️" if runs else "•",
                title=f"Batch → {_stage(stage)}",
                body=(f"{new.get('batch_field') or '?'} {_unit(stage)} · "
                      f"{new.get('batch_validators') or '?'} validators\n"
                      + (f"eta {new['batch_eta_min']}m" if new.get("batch_eta_min") is not None else "")),
                dedup_key=f"{new.get('batch_id')}:{stage}",
            ))

        # Only a CROSSING of VAL_FLOOR is reported -- never the size change
        # itself. Both directions are stateless comparisons against the same
        # constant, so nothing has to be remembered between polls, and the
        # ordinary 4<->5 churn that made up 72% of this topic's traffic emits
        # nothing at all. See VAL_FLOOR at the top of this file.
        if changed(old, new, "val_quorum"):
            try:
                o, n = int(old["val_quorum"]), int(new["val_quorum"])
            except (TypeError, ValueError):
                o = n = None
            if o is not None and n <= VAL_FLOOR < o:
                out.append(CompEvent(
                    kind="validators", severity="critical", icon="🩺",
                    title=f"Validator quorum collapsed — {n} healthy",
                    body=(f"{o} → <b>{n}</b> healthy · "
                          f"{new.get('val_unhealthy')} unhealthy · "
                          f"{new.get('val_unknown')} unknown\n"
                          "<i>each healthy validator is one judge draw and a "
                          "task score is their median — a batch judged now is "
                          "on too few draws to trust</i>"),
                    dedup_key="collapse", cooldown_h=2.0,
                ))
            elif o is not None and o <= VAL_FLOOR < n:
                out.append(CompEvent(
                    kind="validators", severity="good", icon="🩺",
                    title=f"Validator quorum recovered — {n} healthy",
                    body=f"{o} → <b>{n}</b> healthy · scoring is back to a "
                         f"normal number of draws",
                    dedup_key="recovered", cooldown_h=2.0,
                ))
        return out

    # ---- 3. RENDER ----------------------------------------------------------
    def render_state(self, s: dict) -> str:
        L = [f"<b>SN{self.netuid} · {esc(self.label)}</b>", ""]

        L.append("<b>⏳ THIS BATCH</b>")
        rows = [("stage", esc(_stage(s.get("batch_stage")))),
                (_unit(s.get("batch_stage")), str(s.get("batch_field") or "?"))]
        if s.get("batch_pct") is not None:
            rows.append(("done", f"{s['batch_pct']}%"))
        if s.get("batch_eta_min") is not None:
            rows.append(("eta", f"{s['batch_eta_min']}m"))
        L.append(two_col(rows, 20))
        mins = self.deadline_minutes(s)
        if mins is not None:
            L.append(f"next cutoff in <b>{mins // 60}h {mins % 60:02d}m</b> "
                     f"· {s.get('open_n', '?')} already in")

        L += ["", "<b>👑 CROWN</b> <i>(last completed)</i>"]
        if s.get("champ_uid") is not None:
            L.append(f"uid <b>{s['champ_uid']}</b> <code>{esc(s.get('champ_artifact'))}</code>")
            L.append(two_col([("score", num(s.get("champ_score"), 4)),
                              ("cost", _usd(s.get("champ_cost"))),
                              ("runtime", _ms(s.get("champ_ms"))),
                              ("field", str(s.get("last_field") or "?"))], 20))
        else:
            L.append("<i>unavailable</i>")

        L += ["", "<b>🛠 DEPLOY PIN</b>",
              f"<code>{esc(s.get('val_version') or '?')}</code> "
              f"<code>{esc((s.get('val_revision') or '')[:10])}</code> · "
              f"{s.get('val_quorum', '?')} healthy / {s.get('val_unhealthy', '?')} down"]

        L += ["", "<b>🔬 OURS</b>"]
        if s.get("open_ours"):
            for r in s["open_ours"]:
                L.append(f"📤 <code>{esc(r['art'])}</code> {esc(r.get('name') or '')} "
                         f"uid {r.get('uid')} · pos {r['pos']}/{s.get('open_n', '?')}")
        for ref, o in (s.get("ours_last") or {}).items():
            L.append(f"🏁 <code>{esc(ref)}</code> {esc(o.get('name') or '')} "
                     f"{esc(o.get('stage'))} <b>{num(o.get('score'))}</b>")
        if not s.get("open_ours") and not s.get("ours_last"):
            L.append("<i>nothing in flight</i>")
        L.append(f"<i>{len(s.get('our_hk') or [])} live hotkeys</i>")
        return "\n".join(L)

    def render_board(self, s: dict, limit: int = 10) -> str:
        board = s.get("board") or []
        if not board:
            return "No completed main stage yet."
        rows = [f"{'uid':>4} {'score':>7} {'cost':>8} {'runtime':>8}"]
        for r in board[:limit]:
            mark = "👑" if r["champ"] else ("•" if r["inc"] else " ")
            rows.append(f"{str(r['uid']):>4} {num(r.get('score'), 4):>7} "
                        f"{_usd(r.get('cost')):>8} {_ms(r.get('ms')):>8} {mark}")
        return (f"<b>SN{self.netuid} main stage</b> · {s.get('n_main', len(board))} "
                f"of {s.get('last_field') or '?'} survived qualifying\n\n"
                "<pre>" + "\n".join(rows) + "</pre>\n"
                "<i>👑 champion · • previous incumbent</i>")

    def render_me(self, s: dict) -> str:
        L = [f"<b>Our SN{self.netuid} position</b>", ""]
        hk = s.get("our_uids") or {}
        L.append(f"<b>Slots</b> — {len(hk)} live")
        for h, u in hk.items():
            L.append(f"  <code>{esc(short(h))}</code> uid <b>{u}</b>")
        L += ["", "<b>Open window</b>"]
        if s.get("open_ours"):
            for r in s["open_ours"]:
                L.append(f"  📤 <code>{esc(r['art'])}</code> {esc(r.get('name') or '')}\n"
                         f"     uid {r.get('uid')} · position <b>{r['pos']}</b>"
                         f" of {s.get('open_n', '?')}")
        else:
            L.append("  <i>nothing submitted into the open window</i>")
        L += ["", "<b>Last completed race</b>"]
        if s.get("ours_last"):
            for ref, o in s["ours_last"].items():
                L.append(f"  🏁 <code>{esc(ref)}</code> {esc(o.get('name') or '')}\n"
                         f"     uid {o.get('uid')} · {esc(o.get('stage'))} "
                         f"<b>{num(o.get('score'))}</b>")
        else:
            L.append("  <i>nothing of ours completed</i>")
        L.append("\n<i>uid on a receipt is frozen at submission time — the slot "
                 "list above is the live one.</i>")
        return "\n".join(L)

    def render_info(self, s: dict) -> str:
        L = [f"<b>SN{self.netuid} · {esc(self.label)} — the rules</b>", "",
             "One batch per day, cutoff <b>15:00Z</b>. Two stages:",
             "<pre>qualifying  ~10 tasks, whole field, a selection tournament\n"
             "main        20 tasks, 1-4 artifacts, the crown</pre>",
             "Dethroning is a cascade, checked in order:",
             "<pre>score_margin       +0.10 ABSOLUTE\n"
             "cost_reduction     &lt;=0.90x  (score must not regress)\n"
             "runtime_reduction  &lt;=0.90x  (score must not regress)</pre>",
             "<i>An agent can lose on score and still take the crown by being "
             "cheaper or faster.</i>", ""]
        L.append("<b>Cascade order is submission order</b> — position is decided "
                 "by the second you upload.")
        L += ["", "<b>🛠 Validators</b>"]
        for r in (s.get("val_table") or [])[:9]:
            glyph = {"healthy": "✅", "unhealthy": "💥"}.get(r["health"], "❔")
            L.append(f"{glyph} <code>{esc(r['ver'] or '?')}</code> "
                     f"<code>{esc(r['rev'])}</code> {esc(r['name'])}")
        for r in (s.get("_repos") or []):
            L.append(f"\n📦 <code>{esc(r['repo'])}</code> "
                     f"<code>{esc((r.get('sha') or '')[:10])}</code> "
                     f"{esc((r.get('subject') or '')[:60])}")
        return "\n".join(L)

    def render_digest(self, s: dict) -> str:
        bits = [f"<b>SN{self.netuid}</b> {esc(self.label)}"]
        if s.get("champ_uid") is not None:
            bits.append(f"👑 uid {s['champ_uid']} <code>{num(s.get('champ_score'), 3)}</code>")
        bits.append(f"batch {esc(s.get('batch_stage') or '—')}")
        if s.get("open_ours"):
            bits.append(f"ours {len(s['open_ours'])} in")
        return " · ".join(bits)

    def ours_summary(self, s: dict) -> str:
        n_hk = len(s.get("our_hk") or [])
        if not n_hk:
            return ""
        n_in = len(s.get("open_ours") or [])
        return f"{n_in} in window · {n_hk} slots" if n_in else f"{n_hk} slots idle"

    def deadline_minutes(self, s: dict) -> int | None:
        return int((_next_cutoff() - _now()).total_seconds() // 60)

    async def seed_watchlist(self) -> list[dict]:
        """Our artifacts come from the miner's own receipts file.

        Returned for /watch bookkeeping and so the ids are visible in the UI;
        this adapter reads its own results through get_miner rather than
        per-artifact, which costs one call per hotkey instead of one per
        artifact.
        """
        try:
            data = json.loads(RECEIPTS.read_text())
        except Exception:  # noqa: BLE001
            return []
        subs = data.get("submissions") if isinstance(data, dict) else data
        rows = []
        for r in (subs or [])[-8:]:
            if r.get("artifact_id"):
                rows.append({"ref": r["artifact_id"],
                             "label": r.get("version") or "",
                             "uid": r.get("uid")})
        return rows


def _now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def _next_cutoff() -> dt.datetime:
    n = _now()
    c = n.replace(hour=CUTOFF_HOUR, minute=0, second=0, microsecond=0)
    return c if c > n else c + dt.timedelta(days=1)


def _last_cutoff() -> dt.datetime:
    return _next_cutoff() - dt.timedelta(days=1)
