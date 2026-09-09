"""SN100 BASE / Prism.

The reference adapter. Everything here is SN100-specific -- endpoints, field
names, what counts as an event -- which is exactly the point: the framework
never learns any of it.

Prism is winner-take-all. One hotkey takes the whole miner emission, so the
only questions that matter are "who is king", "did the rules change under us"
and "what is our own run doing right now". The renderers answer those three in
that order.
"""
from __future__ import annotations

import collections
import logging
import os
import re
import json
import time
from pathlib import Path

from .. import store
from ..base import (CompEvent, SubnetAdapter, arrow, changed, clean_error, esc,
                    fetch_json, num, pct, sci, short, two_col)

log = logging.getLogger("taoscope.comp.sn100")

BASE = "https://chain.joinbase.ai"
ARENA = "prism"

# An infra-class failure re-opens the submission slot for 30 minutes; a cheat
# rejection never does. Telling those two apart is the difference between
# recovering a hotkey and losing it, so the alert says which one it is.
INFRA_CLASSES = {"install", "ast_infra", "llm_infra"}
INFRA_WINDOW_MIN = 30

# phase -> (icon, the words a human uses for it)
PHASE = {
    "waiting": ("⏳", "waiting for a GPU"),
    "running": ("▶️", "training on a GPU"),
    "done":    ("🏁", "finished"),
    "dead":    ("💥", "died"),
}


def _label(text: str | None) -> str:
    """Trim the (uidNNN,seedNNN) tail -- the uid is already on the line above."""
    return re.sub(r"\s*\(uid\d+,\s*", " (", (text or "").strip())[:52]


class SN100(SubnetAdapter):
    netuid = 100
    slug = "prism"
    label = "BASE / Prism"
    poll_seconds = 150
    repos = ["BaseIntelligence/base"]
    links = {
        "dashboard": "https://joinbase.ai",
        "leaderboard": f"{BASE}/v1/site/arenas/{ARENA}/leaderboard",
        "recipe": f"{BASE}/challenge/{ARENA}/v1/recipe",
    }
    # Mounted read-only from the mining workspace; this is where our own
    # submission ids are written as we fire them. Absent = fall back to /watch.
    DEPLOY_TTL_S = 600
    _deploy_cache: dict = {}
    _deploy_at: float = 0.0
    watchfile = Path(os.getenv("SN100_WATCHFILE",
                               "/work/base-intel/artifacts/live_submissions.json"))

    # ---------------- collect ----------------
    async def seed_watchlist(self) -> list[dict]:
        """Our live submission ids, off the file the miner tooling writes.

        SHAPE-TOLERANT ON PURPOSE. This file is written by a different program
        on a different schedule, and on 2026-08-21 12:03 it started holding a
        single submission OBJECT where this expected a LIST of them. `for r in
        rows` then iterated the dict's KEYS, `r.get(...)` was called on a str,
        and the AttributeError escaped the `json.loads` try/except and killed
        the whole SN100 poll -- not just the watchlist -- for **3.4 days**.

        So: accept an object or a list, skip anything in the list that is not a
        mapping, and take the id under either name the tooling has used. Every
        branch below is a shape this file has actually been observed in; the
        cost of tolerating one more is nil and the cost of not is the subnet.
        """
        if not self.watchfile.exists():
            return []
        try:
            rows = json.loads(self.watchfile.read_text())
        except Exception:  # noqa: BLE001
            return []
        if isinstance(rows, dict):
            # One submission per file, the current format.
            rows = [rows]
        if not isinstance(rows, list):
            log.warning("sn100: watchfile is %s, expected a list or an object",
                        type(rows).__name__)
            return []
        out = []
        for r in rows:
            if not isinstance(r, dict):
                continue
            ref = r.get("submission_id") or r.get("id")
            if not ref:
                continue
            out.append({"ref": ref,
                        "label": r.get("label") or r.get("name") or "",
                        "uid": r.get("uid")})
        if rows and not out:
            log.warning("sn100: watchfile has %d row(s) but no usable id "
                        "(keys seen: %s)", len(rows),
                        sorted({k for r in rows if isinstance(r, dict)
                                for k in r})[:8])
        return out

    async def snapshot(self) -> dict:
        s: dict = {}

        rc = await fetch_json(f"{BASE}/challenge/{ARENA}/v1/recipe")
        if rc:
            s["comp_id"] = rc.get("competition_id")
            s["gen"] = rc.get("scoring_generation")
            s["recipe_version"] = rc.get("version")
            s["min_params"] = rc.get("min_params")
            s["max_params"] = rc.get("max_params")
            s["train_cap_h"] = rc.get("train_hours_cap")
            s["pod_life_h"] = rc.get("pod_lifetime_hours_cap")
            s["flops_cap"] = rc.get("train_flops_cap")
            s["min_spend"] = rc.get("min_spend_fraction")
            s["max_steps"] = rc.get("max_train_steps")
            s["pin"] = rc.get("automodel_pin_id")
            s["pin_hex"] = (rc.get("pin_hex") or "")[:12]
            s["dataset"] = rc.get("dataset_ref")

        w = await fetch_json(f"{BASE}/v1/site/weights")
        if w:
            s["shares"] = {x["arena"]: x["share"] for x in w.get("emissionShares", [])}
            s["burn"] = w.get("burnShare")
            s["sealed_epoch"] = w.get("epoch")
            s["paid"] = {x["hotkey"][:12]: round(x["weight"], 4)
                         for x in w.get("hotkeyWeights", [])}

        lb = await fetch_json(
            f"{BASE}/v1/site/arenas/{ARENA}/leaderboard?page=1&pageSize=60")
        if lb and lb.get("items"):
            items = lb["items"]
            s["n_board"] = lb.get("total") or len(items)
            s["board"] = [{
                "rank": r.get("rank"),
                "hk": (r.get("agent") or {}).get("hotkey", ""),
                "uid": (r.get("agent") or {}).get("uid"),
                "elo": round(r.get("elo") or 0),
                "sub": (r.get("submissionId") or "")[:8],
                "params": r.get("paramsM"),
                "bpb": r.get("bitsPerByte"),
                "era": r.get("recipeEra"),
                "eligible": r.get("weightEligible"),
                "groups": {g["group"]: round(g["g"], 4)
                           for g in (r.get("evalGroups") or [])},
            } for r in items[:20]]
            top = s["board"][0]
            s["king_hk"] = top["hk"]
            s["king_uid"] = top["uid"]
            s["king_elo"] = top["elo"]
            s["king_params"] = top["params"]
            s["king_bpb"] = top["bpb"]
            s["king_groups"] = top["groups"]
            s["board_hk"] = sorted({r["hk"] for r in s["board"] if r["hk"]})

        # Page the WHOLE list. A single pageSize=50 read counted 50 of 97 and
        # reported "0 waiting / 3 in admission / 2 on a GPU" -- numbers that
        # were both wrong and unreadable. What an operator actually wants is
        # the funnel: how many ever scored, how many died at a gate, how many
        # are still competing for hardware.
        items, total = [], None
        for page in range(1, 6):
            sub = await fetch_json(
                f"{BASE}/v1/site/arenas/{ARENA}/submissions?page={page}&pageSize=50")
            if not sub or not sub.get("items"):
                break
            items += sub["items"]
            total = sub.get("total") or total
            if total and len(items) >= total:
                break
        if items:
            stage = collections.Counter(x.get("stage") for x in items)
            s["n_subs"] = total or len(items)
            s["n_scanned"] = len(items)
            s["n_miners"] = len({(x.get("agent") or {}).get("uid") for x in items})
            # terminal outcomes
            s["n_scored"] = stage["terminated"]
            s["n_rejected"] = stage["rejected"]        # gates: cheat / copy / static
            s["n_failed"] = stage["failed"]            # NOT terminal: auto-retried
            # still competing
            s["n_gpu"] = stage["running"]
            s["n_provisioning"] = stage["provisioning"]
            s["n_admission"] = sum(v for k, v in stage.items()
                                   if k in ("queued", "similarity", "llm_review",
                                            "scoring", "installing", "measuring",
                                            "training", "pending"))
            s["n_live"] = s["n_gpu"] + s["n_provisioning"] + s["n_admission"]
            s["latest_sub"] = (items[0].get("id") or "")[:8]
            s["latest_at"] = items[0].get("submittedAt")

        s.update(await self._deploys())
        s["ours"] = await self._ours(s.get("board") or [])
        return s

    async def _deploys(self) -> dict:
        """Which commits are actually LIVE, not just merged.

        "The repo moved" is too blunt to act on. What changes our exposure is a
        deploy pin moving: the operator lands a fix on main, then pins it to
        staging, then promotes it to prod, and only the last step changes the
        platform we are submitting into. On 2026-08-20 the fix that stops
        Lium no_capacity requeues from re-running the LLM screens (ae7c872) was
        merged and sitting in NO pin -- so the terminal-rejection risk it fixes
        was still live in prod, which is exactly the thing worth a message.

        Cached AND conditional. The TTL bounds this to 6 requests an hour, and
        `github.conditional_json` makes each of them carry an `If-None-Match`
        plus the configured token -- so once settled they are 304s, which cost
        nothing against the rate limit.

        This used to go through `base.fetch_json`, which sends neither header.
        That made every one of those 6 an unconditional, unauthenticated request
        against a 60/hour budget shared with every other adapter, and it could
        never 304 no matter how long the repo sat still. Worse, setting
        TAOSCOPE_GITHUB_TOKEN would not have helped: the token was never
        attached, so the call stayed on the anonymous limit.
        """
        from .. import github

        now = time.time()
        if self._deploy_cache and now - self._deploy_at < self.DEPLOY_TTL_S:
            return self._deploy_cache
        data, code = await github.conditional_json(
            f"https://api.github.com/repos/{self.repos[0]}/commits?per_page=60",
            cache_key="sn100:deploys")
        if code == 304:
            # Positively unchanged, and free. Reset the TTL so a quiet repo is
            # not re-asked every 600s about an answer GitHub just confirmed.
            self._deploy_at = now
            return self._deploy_cache or {}
        if not data:
            return self._deploy_cache or {}
        prod = staging = None
        pending = []
        for c in data:                       # newest first
            msg = ((c.get("commit") or {}).get("message") or "").splitlines()[0]
            if msg.startswith("deploy: promote prod pins for") and prod is None:
                prod = {"sha": c["sha"][:10], "target": msg.rsplit(" ", 1)[-1][:10]}
            elif msg.startswith("deploy: staging pins for") and staging is None:
                staging = {"sha": c["sha"][:10], "target": msg.rsplit(" ", 1)[-1][:10]}
            elif not msg.startswith("deploy:") and prod is None:
                # merged after the newest prod promote = not live yet
                pending.append(msg[:70])
        out = {}
        if prod:
            out["prod_pin"] = prod["target"]
        if staging:
            out["staging_pin"] = staging["target"]
        out["undeployed"] = pending[:8]
        out["n_undeployed"] = len(pending)
        self._deploy_cache, self._deploy_at = out, now
        return out

    async def _ours(self, board: list[dict]) -> dict:
        """State of our own runs.

        The platform's `stage` field is NOT progress. A submission loops through
        similarity -> llm_review -> scoring -> provisioning -> failed(install)
        -> queued(rate_limit_requeue) roughly every 30 seconds while it waits for
        a GPU, so `stage` reads "scoring" on a run that has never touched a B200.
        Measured on 58ceb802: 4,168 events, 596 complete cycles, 4.7 hours,
        ZERO pod ids and ZERO heartbeats.

        So phase is derived from what actually happened:

            running  a pod id or a heartbeat exists -- really on a GPU
            done     terminated
            dead     the last event is failed/rejected (and did not re-queue)
            waiting  everything else: in the admission loop, burning nothing

        Diffing on phase instead of stage collapses 596 cycles into one event.
        """
        by_sub = {r["sub"]: r for r in board if r.get("sub")}
        out: dict = {}
        for row in await store.watched(self.netuid):
            ref = row["ref"]
            d = await fetch_json(f"{BASE}/challenge/{ARENA}/v1/submissions/{ref}")
            if not d or not d.get("submission"):
                continue
            sm = d["submission"]
            events = d.get("events") or []
            last = events[-1] if events else {}
            detail = last.get("detail") or {}
            stage = last.get("stage") or sm.get("status")

            pod = sm.get("pod_id")
            heartbeats = 0
            cycles = 0
            blocked = None
            for e in events:
                det = e.get("detail") or {}
                if e.get("stage") == "queued":
                    cycles += 1
                    if det.get("note"):
                        blocked = det["note"]
                if det.get("heartbeat"):
                    heartbeats += 1
                pod = pod or det.get("pod_id")

            if stage == "terminated":
                phase = "done"
            elif stage in ("failed", "rejected"):
                phase = "dead"
            elif pod or heartbeats or stage == "running":
                phase = "running"
            else:
                phase = "waiting"

            lb = by_sub.get(ref[:8], {})
            entry = {
                "uid": row.get("uid"),
                "label": row.get("label") or "",
                "phase": phase,
                "stage": stage,
                "status": sm.get("status"),
                "cls": detail.get("class"),
                "cycles": cycles,
                "pod": pod,
                "heartbeats": heartbeats,
                "blocked": blocked,
                "err": (sm.get("error_detail") or "")[:800],
                "epoch": sm.get("epoch"),
                "n_events": len(events),
            }
            score = sm.get("score") or {}
            if score.get("kind") and score.get("kind") != "no_score":
                entry["score_kind"] = score.get("kind")
                entry["score"] = score.get("value") or score.get("score")
            if lb:
                entry["rank"] = lb.get("rank")
                entry["elo"] = lb.get("elo")
                entry["groups"] = lb.get("groups")
            out[ref[:8]] = entry
        return out

    # ---------------- diff ----------------
    #
    # Every event follows the same shape, because a wall of alerts is only
    # readable when they all read the same way:
    #
    #     <icon> HEADLINE          what happened, in three words
    #     old -> new               the change itself, never implied
    #     context                  the numbers you would look up next
    #     action                   only when there is one, in italics
    #
    # Titles carry no icon: the router adds exactly one.

    def diff(self, old: dict, new: dict) -> list[CompEvent]:
        out: list[CompEvent] = []
        if not old:
            # First sight is a baseline, never a wall of alerts.
            return out

        # -- the rules changing invalidates every patch we are holding --
        for keys, what, note in (
            (("comp_id", "gen"), "Competition changed",
             "The board resets. Everything scored under the old id is history."),
            (("pin", "pin_hex"), "Recipe pin changed",
             "Any patch built against the old pin is invalid — rebuild before submitting."),
            (("min_params", "max_params"), "Param band changed",
             "Check the model still fits before the next submission."),
            (("train_cap_h", "flops_cap", "pod_life_h"), "Budget caps changed",
             "The token budget moves with this."),
            (("dataset",), "Dataset changed", ""),
        ):
            if changed(old, new, *keys):
                before = " / ".join(str(old.get(k)) for k in keys)
                after = " / ".join(str(new.get(k)) for k in keys)
                out.append(CompEvent(
                    kind="rules", severity="critical", icon="🚨", title=what,
                    body=f"<code>{esc(before)}</code>\n"
                         f"→ <code>{esc(after)}</code>"
                         + (f"\n<i>{esc(note)}</i>" if note else ""),
                    dedup_key=f"{what}:{after}",
                    detail={"keys": list(keys), "old": before, "new": after},
                ))

        # -- who is being paid --
        if changed(old, new, "shares", "burn"):
            out.append(CompEvent(
                kind="emission", severity="critical", icon="💰",
                title="Emission split changed",
                body=f"{self._shares_str(old)}\n→ {self._shares_str(new)}",
                dedup_key=f"{new.get('shares')}|{new.get('burn')}",
                detail={"old": old.get("shares"), "new": new.get("shares")},
            ))
        if changed(old, new, "paid"):
            was, now = old.get("paid") or {}, new.get("paid") or {}
            out.append(CompEvent(
                kind="sealed", severity="warn", icon="🔏",
                title="Sealed weights changed",
                body=f"{esc(self._who(was, old))}\n→ {esc(self._who(now, new))}\n"
                     f"epoch {new.get('sealed_epoch')}",
                dedup_key=str(sorted(now.items())),
                detail={"old": was, "new": now},
            ))

        # -- the crown --
        if changed(old, new, "king_hk") and old.get("king_hk"):
            out.append(CompEvent(
                kind="king_change", severity="critical", icon="👑",
                title="New king",
                body=f"{esc(short(new['king_hk']))} · uid {new.get('king_uid')} · "
                     f"<b>{num(new.get('king_elo'), 0)}</b>\n"
                     f"was {esc(short(old.get('king_hk')))} · "
                     f"{num(old.get('king_elo'), 0)}\n"
                     f"{num(new.get('king_params'), 0)}M · bpb {num(new.get('king_bpb'), 3)}"
                     + (f"\nweakest {esc(self._weakest(new.get('king_groups') or {}))}"
                        if new.get("king_groups") else ""),
                dedup_key=str(new.get("king_hk")),
                detail={"old": old.get("king_hk"), "new": new.get("king_hk"),
                        "elo": new.get("king_elo")},
            ))
        elif changed(old, new, "king_elo"):
            delta = (new["king_elo"] or 0) - (old.get("king_elo") or 0)
            out.append(CompEvent(
                kind="king_score", severity="info",
                icon="📈" if delta > 0 else "📉",
                title="King improved" if delta > 0 else "King's score fell",
                body=f"{num(old.get('king_elo'), 0)} → <b>{num(new['king_elo'], 0)}</b> "
                     f"({delta:+,.0f})\n"
                     f"same hotkey {esc(short(new.get('king_hk')))}",
                dedup_key=str(new.get("king_elo")),
            ))

        # -- new names on the board --
        if changed(old, new, "board_hk"):
            fresh = [h for h in new["board_hk"] if h not in set(old.get("board_hk") or [])]
            if fresh:
                rows = {r["hk"]: r for r in new.get("board", [])}
                lines = [f"rank {rows[h]['rank']} · {short(h)} · "
                         f"<b>{num(rows[h]['elo'], 0)}</b> · "
                         f"{num(rows[h].get('params'), 0)}M"
                         for h in fresh if h in rows]
                out.append(CompEvent(
                    kind="new_entrant", severity="info", icon="➕",
                    title=f"{len(fresh)} new on the board",
                    body="\n".join(lines) +
                         f"\n{new.get('n_board')} entries now",
                    dedup_key=",".join(sorted(fresh)),
                    detail={"hotkeys": fresh},
                ))

        # -- what is actually LIVE --
        if changed(old, new, "prod_pin"):
            out.append(CompEvent(
                kind="rules", severity="critical", icon="🚀",
                title="PROD pin promoted",
                body=f"<code>{esc(old.get('prod_pin'))}</code> → "
                     f"<code>{esc(new['prod_pin'])}</code>\n"
                     f"<i>The platform we submit into just changed. Re-read the "
                     f"diff before the next submission.</i>",
                dedup_key=str(new.get("prod_pin")),
                detail={"old": old.get("prod_pin"), "new": new.get("prod_pin")}))
        if changed(old, new, "staging_pin"):
            out.append(CompEvent(
                kind="rules", severity="warn", icon="🧪",
                title="Staging pin moved",
                body=f"<code>{esc(old.get('staging_pin'))}</code> → "
                     f"<code>{esc(new['staging_pin'])}</code>\n"
                     f"<i>Prod usually follows.</i>",
                dedup_key=str(new.get("staging_pin"))))

        # -- our own runs: every transition, no cooldown --
        out.extend(self._diff_ours(old.get("ours") or {}, new.get("ours") or {}))

        # -- the queue, but only when it means something --
        out.extend(self._diff_queue(old, new))
        return out

    def _shares_str(self, s: dict) -> str:
        live = " · ".join(f"{k} {pct(v, 0)}" for k, v in sorted((s.get("shares") or {}).items())
                          if (v or 0) > 0) or "—"
        return f"{live} · burn {pct(s.get('burn'), 0)}"

    def _who(self, paid: dict, s: dict) -> str:
        if not paid:
            return "nobody — burning"
        king = (s.get("king_hk") or "")[:12]
        if set(paid) == {king}:
            return "👑 the king"
        return ", ".join(f"{k[:8]}…" for k in paid)

    def _diff_queue(self, old: dict, new: dict) -> list[CompEvent]:
        """Only two things about the field are news.

        A NEW ENTRY SCORING is the whole competition -- 8 of 97 submissions ever
        reached a number, so each one changes the bar. And hardware freeing up
        matters because the queue is a race, not a line: every provisioning job
        re-attempts its own rent call every ~30s, so a freed GPU is won by
        whoever's call lands, not by whoever waited longest.

        Raw queue depth is NOT news. The first version fired on every +/-1 and
        said "was 1 / 8", naming neither number.
        """
        out = []
        if changed(old, new, "n_scored"):
            os_, ns = old.get("n_scored") or 0, new.get("n_scored") or 0
            if ns > os_:
                out.append(CompEvent(
                    kind="new_entrant", severity="warn", icon="🎯",
                    title=f"{ns - os_} new entr{'y' if ns - os_ == 1 else 'ies'} SCORED",
                    body=f"{ns} have now scored, from {new.get('n_miners', '?')} "
                         f"miners and {new.get('n_subs', '?')} submissions\n"
                         f"<i>Check /board — the bar may have moved.</i>",
                    dedup_key=str(ns)))
        if changed(old, new, "n_gpu"):
            og, ng = old.get("n_gpu") or 0, new.get("n_gpu") or 0
            waiting = new.get("n_provisioning", "?")
            if ng < og:
                out.append(CompEvent(
                    kind="queue", severity="good", icon="🟢",
                    title="A GPU just freed up",
                    body=f"{og} → {ng} on hardware · {waiting} jobs racing for it\n"
                         f"<i>Every provisioning job retries every ~30s; it is a "
                         f"race, not a line.</i>",
                    dedup_key=f"free:{ng}"))
            else:
                out.append(CompEvent(
                    kind="queue", severity="info", icon="🖥",
                    title=f"{ng} on a GPU now",
                    body=f"was {og} · {waiting} still waiting",
                    dedup_key=f"busy:{ng}"))
        return out

    def _diff_ours(self, old: dict, new: dict) -> list[CompEvent]:
        """One event per real change of situation.

        Keyed on (phase, class, blocked-reason) -- NOT stage. A run cycling
        through the admission loop changes `stage` every poll and situation
        never; alerting on stage produced a message every 150s saying nothing.
        """
        out = []
        for ref, cur in new.items():
            was = old.get(ref)
            if was is None:
                out.append(CompEvent(
                    kind="our_run", severity="info", icon="👁",
                    title=f"Now tracking {ref}",
                    body=f"uid {cur.get('uid')} · {esc(PHASE[cur['phase']][1])}\n"
                         f"{esc(_label(cur.get('label')))}",
                    dedup_key=f"track:{ref}"))
                continue

            key_now = (cur.get("phase"), cur.get("cls"), (cur.get("blocked") or "")[:40])
            key_was = (was.get("phase"), was.get("cls"), (was.get("blocked") or "")[:40])
            if key_now == key_was:
                continue

            phase = cur.get("phase") or "waiting"
            icon, human = PHASE.get(phase, ("🔄", phase))
            sev, action = "info", ""
            if phase == "dead":
                sev = "critical" if cur.get("status") == "rejected" else "warn"
                if cur.get("status") == "rejected":
                    action = "<i>Rejections do not re-open the slot.</i>"
                elif cur.get("cls") in INFRA_CLASSES:
                    action = (f"⚠️ <i>Infra class — the slot re-opens for "
                              f"{INFRA_WINDOW_MIN} min. Resubmit with a new "
                              f"SEED_OFFSET or the hotkey is lost.</i>")
                elif cur.get("cls"):
                    action = (f"<i>Class <code>{esc(cur['cls'])}</code> is not a "
                              f"known-recoverable class — check whether the slot "
                              f"re-opened before assuming it did.</i>")
            elif phase == "running":
                sev = "good"
                action = "<i>On a GPU now — this is the one that spends budget.</i>"
            elif phase == "done":
                sev = "good"
                if cur.get("elo"):
                    action = (f"<b>Scored {num(cur['elo'], 0)} · rank "
                              f"{cur.get('rank')}</b>")
            elif phase == "waiting":
                sev = "info"

            lines = [f"{esc(PHASE.get(was.get('phase'), ('', '?'))[1])} → <b>{esc(human)}</b>"
                     + (f" · <code>{esc(cur['cls'])}</code>" if cur.get("cls") else ""),
                     esc(_label(cur.get("label")))]
            if phase == "dead" and cur.get("err"):
                lines.append(f"↳ <code>{esc(clean_error(cur['err']))}</code>")
            elif phase == "waiting" and cur.get("blocked"):
                lines.append(f"↳ {esc(cur['blocked'][:120])}")
            if phase == "waiting" and not cur.get("pod") and cur.get("cycles", 0) > 5:
                lines.append(f"<i>{cur['cycles']:,} retry cycles, still no GPU.</i>")
            if action:
                lines.append(action)

            out.append(CompEvent(
                kind="our_run", severity=sev, icon=icon,
                title=f"Our run {ref} · uid {cur.get('uid')}",
                body="\n".join(x for x in lines if x),
                dedup_key=f"{ref}:{phase}:{cur.get('cls')}",
                detail={"ref": ref, "uid": cur.get("uid"), "phase": phase,
                        "cycles": cur.get("cycles")},
            ))
        return out

    # ---------------- render ----------------
    ORDER = {"running": 0, "waiting": 1, "done": 2, "dead": 3}

    def _ours_sorted(self, s: dict) -> list[tuple[str, dict]]:
        """A run on a GPU first, a run that died an hour ago last. Sorting by
        hex id put them in a meaningless order."""
        return sorted((s.get("ours") or {}).items(),
                      key=lambda kv: (self.ORDER.get(kv[1].get("phase"), 9), kv[0]))

    def _pay_line(self, s: dict) -> str:
        shares = s.get("shares") or {}
        live = " · ".join(f"{k} {pct(v, 0)}" for k, v in sorted(shares.items())
                          if (v or 0) > 0) or "—"
        paid = s.get("paid") or {}
        king = (s.get("king_hk") or "")[:12]
        if paid and set(paid) == {king}:
            who = "👑 the king"
        elif paid:
            who = ", ".join(f"{k[:8]}…" for k in paid)
        else:
            who = "nobody — burning"
        return f"{live} · burn {pct(s.get('burn'), 0)}\n→ {esc(who)}"

    def _weakest(self, groups: dict, n: int = 3) -> str:
        if not groups:
            return ""
        lo = sorted(groups.items(), key=lambda kv: kv[1])[:n]
        return " · ".join(f"{k} {v:.3f}" for k, v in lo)

    def render_state(self, s: dict) -> str:
        L = [f"<b>SN{self.netuid} · {esc(self.label)}</b>",
             f"<code>{esc(s.get('comp_id') or '?')}</code> · "
             f"updated {esc(_ago(s.get('_fetched_at')))}", ""]

        if s.get("king_hk"):
            L += [f"<b>👑 KING</b>  <code>{num(s.get('king_elo'), 0)}</code>",
                  f"{esc(short(s['king_hk']))} · uid {s.get('king_uid')} · "
                  f"{num(s.get('king_params'), 0)}M · bpb {num(s.get('king_bpb'), 3)}"]
            weak = self._weakest(s.get("king_groups") or {})
            if weak:
                L.append(f"weakest {esc(weak)}   <i>/board for all 8</i>")
        else:
            L.append("<b>👑 KING</b>  board unavailable")
        L.append("")

        ours = s.get("ours") or {}
        scored = [(o.get("elo"), o.get("rank")) for o in ours.values() if o.get("elo")]
        if scored:
            best, rank = max(scored)
            gap = (s.get("king_elo") or 0) - best
            L.append(f"<b>🏁 US</b>  <code>{num(best, 0)}</code> · rank {rank} · "
                     f"{num(gap, 0)} behind")
        else:
            L.append(f"<b>🏁 US</b>  no scored entry yet · {len(ours)} tracked")

        if s.get("prod_pin"):
            L.append(f"<b>🚀 LIVE</b>  prod <code>{esc(s['prod_pin'])}</code>"
                     + (f" · staging <code>{esc(s.get('staging_pin'))}</code>"
                        if s.get("staging_pin") else "")
                     + (f" · <b>{s['n_undeployed']} merged, not deployed</b>"
                        if s.get("n_undeployed") else ""))
        L.append(f"<b>💰 PAY</b>  {self._pay_line(s)}")
        L.append(f"<b>📥 FIELD</b>  <b>{s.get('n_scored', '?')} have ever scored</b>"
                 f" — from {s.get('n_miners', '?')} miners, "
                 f"{s.get('n_subs', '?')} submissions")
        L.append(f"  live now  <b>{s.get('n_gpu', '?')} on a GPU</b> · "
                 f"{s.get('n_provisioning', '?')} waiting for one · "
                 f"{s.get('n_admission', '?')} in admission")
        L.append(f"  dead      {s.get('n_rejected', '?')} rejected at a gate · "
                 f"{s.get('n_failed', '?')} failed <i>(retryable)</i>")
        L.append("")

        L.append("<b>🔬 OUR RUNS</b>")
        if not ours:
            L.append("<i>none — <code>/watch &lt;submission_id&gt;</code></i>")
        for ref, o in self._ours_sorted(s):
            icon, human = PHASE.get(o.get("phase"), ("🔄", "?"))
            line = f"{icon} <code>{ref}</code> uid {o.get('uid')} · {esc(human)}"
            if o.get("cls"):
                line += f" <i>{esc(o['cls'])}</i>"
            if o.get("elo"):
                line += f" · <b>{num(o['elo'], 0)}</b>"
            L.append(line)
            if o.get("phase") == "waiting" and not o.get("pod") \
                    and o.get("cycles", 0) > 5:
                L.append(f"   <i>{o['cycles']:,} retries, still no GPU</i>"
                         + (f" — {esc(o['blocked'][:48])}" if o.get("blocked") else ""))
        return "\n".join(L)

    def render_info(self, s: dict) -> str:
        ch = s.get("_chain") or {}
        L = [f"<b>SN{self.netuid} · {esc(self.label)} — the rules</b>", "",
             "<b>Competition</b>",
             two_col([
                 ("id", esc(s.get("comp_id") or "?")),
                 ("gen", str(s.get("gen"))),
                 ("recipe", esc(s.get("recipe_version") or "?")),
                 ("pin", esc(s.get("pin") or "?")),
             ], 24),
             "", "<b>Budget</b>",
             two_col([
                 ("params", f"{num((s.get('min_params') or 0)/1e6, 0)}–"
                            f"{num((s.get('max_params') or 0)/1e6, 0)}M"),
                 ("flops", sci(s.get("flops_cap"))),
                 ("train", f"{s.get('train_cap_h')}h"),
                 ("pod", f"{s.get('pod_life_h')}h"),
                 ("steps", num(s.get("max_steps"), 0)),
                 ("min spend", pct(s.get("min_spend"), 0)),
             ], 22),
             f"dataset <code>{esc(s.get('dataset'))}</code>",
             f"pin sha <code>{esc(s.get('pin_hex'))}</code>", ""]

        if ch:
            L += ["", "<b>Chain</b>",
                  two_col([
                      ("reg", "OPEN" if ch.get("registration_allowed") else "closed"),
                      ("cost", f"τ{num(ch.get('burn_tao'), 4)}"),
                      ("uids", f"{ch.get('num_uids')}/{ch.get('max_uids')}"),
                      ("operators", str(ch.get("unique_coldkeys"))),
                      ("emission", pct(ch.get("emission_share"))),
                      ("τ/day", num((ch.get("realized_tao_per_hour") or 0) * 24, 1)),
                  ], 22),
                  f"price τ{num(ch.get('price'), 6)}", ""]

        if s.get("undeployed"):
            L.append(f"<b>Merged but NOT deployed</b> ({s.get('n_undeployed')})")
            for m in s["undeployed"]:
                L.append(f"  · {esc(m)}")
            L.append("")
        for r in (s.get("_repos") or []):
            L.append(f"📦 <code>{esc(r['repo'])}</code> "
                     f"<code>{esc((r.get('sha') or '')[:10])}</code>")
            L.append(f"   <i>{esc((r.get('subject') or '')[:64])}</i>")
        if s.get("_repos"):
            L.append("")
        L += [f'<a href="{v}">{esc(k)}</a>' for k, v in self.links.items()]
        return "\n".join(L)

    def render_board(self, s: dict, limit: int = 10) -> str:
        board = s.get("board") or []
        if not board:
            return "Board unavailable."
        mine = {o_ref for o_ref in (s.get("ours") or {})}
        rows = [f"{'#':<2} {'score':>8} {'uid':>4}  {'size':<5} {'bpb':>5}"]
        for r in board[:limit]:
            mark = "◀" if (r.get("sub") or "") in mine else " "
            rows.append(f"{r['rank']:<2} {r['elo']:>8,} {str(r.get('uid') or '—'):>4}  "
                        f"{(str(round(r['params']))+'M' if r.get('params') else '—'):<5} "
                        f"{num(r.get('bpb'), 3):>5}{mark}")
        out = [f"<b>SN{self.netuid} leaderboard</b> · {s.get('n_board', len(board))} entries",
               "", "<pre>" + "\n".join(rows) + "</pre>", ""]
        g = s.get("king_groups") or {}
        if g:
            out += ["<b>👑 King's group scores</b>",
                    two_col([(k, f"{v:.3f}") for k, v in sorted(g.items())], 11),
                    f"<i>weakest: {esc(self._weakest(g))}</i>"]
        return "\n".join(out)

    def render_me(self, s: dict) -> str:
        ours = self._ours_sorted(s)
        if not ours:
            return ("Nothing tracked yet.\n"
                    "<code>/watch &lt;submission_id&gt; [label]</code> to add one.")
        L = [f"<b>Our SN{self.netuid} submissions</b> · {len(ours)} tracked", ""]
        for ref, o in ours:
            icon, human = PHASE.get(o.get("phase"), ("🔄", "?"))
            L.append(f"{icon} <code>{ref}</code> · uid <b>{o.get('uid')}</b> · "
                     f"<b>{esc(human)}</b>"
                     + (f" <i>{esc(o['cls'])}</i>" if o.get("cls") else ""))
            label = _label(o.get("label"))
            if label:
                L.append(f"   <i>{esc(label)}</i>")
            L.append(f"   epoch {o.get('epoch')} · {o.get('n_events', 0):,} events"
                     + (f" · {o['cycles']:,} retries" if o.get("cycles") else "")
                     + (f" · pod <code>{esc((o['pod'] or '')[:8])}</code>"
                        if o.get("pod") else " · no pod yet"))
            if o.get("elo"):
                L.append(f"   rank <b>{o.get('rank')}</b> · score <b>{num(o['elo'], 0)}</b>")
                g = o.get("groups") or {}
                if g:
                    L.append(two_col([(k, f"{v:.3f}") for k, v in sorted(g.items())], 11))
            if o.get("phase") == "dead":
                err = clean_error(o.get("err"))
                if err:
                    L.append(f"   ↳ {esc(err)}")
            elif o.get("phase") == "waiting" and o.get("blocked"):
                L.append(f"   ↳ {esc(o['blocked'][:120])}")
            if o.get("phase") == "dead" and o.get("cls") in INFRA_CLASSES:
                L.append(f"   ⚠️ <b>infra class — slot re-opens for "
                         f"{INFRA_WINDOW_MIN} min</b>")
            L.append("")
        return "\n".join(L).rstrip()

    def ours_summary(self, s: dict) -> str:
        ours = s.get("ours") or {}
        if not ours:
            return ""
        gpu = sum(1 for o in ours.values() if o.get("phase") == "running")
        wait = sum(1 for o in ours.values() if o.get("phase") == "waiting")
        scored = [o.get("elo") for o in ours.values() if o.get("elo")]
        bits = [f"{len(ours)} tracked"]
        if gpu:
            bits.append(f"{gpu} on GPU")
        if wait:
            bits.append(f"{wait} waiting")
        if scored:
            gap = (s.get("king_elo") or 0) - max(scored)
            bits.append(f"best {num(max(scored), 0)} ({num(gap, 0)} behind)")
        return " · ".join(bits)

    def render_digest(self, s: dict) -> str:
        ours = s.get("ours") or {}
        gpu = sum(1 for o in ours.values() if o.get("phase") == "running")
        wait = sum(1 for o in ours.values() if o.get("phase") == "waiting")
        return (f"<b>SN{self.netuid}</b> {esc(self.label)} — 👑 "
                f"<code>{num(s.get('king_elo'), 0)}</code> {esc(short(s.get('king_hk'), 6, 3))}"
                f" · board {s.get('n_board', '?')} · ours {len(ours)} "
                f"({gpu} on GPU, {wait} waiting)")


def _ago(iso: str | None) -> str:
    if not iso:
        return "never"
    try:
        import datetime as dt
        t = dt.datetime.fromisoformat(iso)
        secs = int(time.time() - t.timestamp())
    except Exception:  # noqa: BLE001
        return str(iso)
    if secs < 90:
        return f"{secs}s ago"
    if secs < 5400:
        return f"{secs // 60}m ago"
    return f"{secs // 3600}h ago"
