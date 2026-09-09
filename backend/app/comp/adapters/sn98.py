"""SN98 — Never Play Alone. Minecraft Mineflayer agents, winner-take-all daily rounds.

WHAT MAKES THIS SUBNET AWKWARD, and why the adapter is shaped the way it is:

  * Rounds OVERLAP. A round is 48h but a new one opens every 24h, so three are
    always in flight: one taking submissions, one being evaluated, one just
    published. Round N's `round_end_block` IS round N+1's submission close, so
    **every submission decision is blind to the previous round's result.** The
    single most useful thing this bot can say is "the window shuts in Xh".

  * In-flight rounds are PRIVATE. `/public/rounds/{id}/roster|scoreboards`
    return 403 until a round completes, and `n_miners`/`standings` come back
    null. So the board can only ever describe the newest COMPLETED round -- it
    is a result feed, not a live ladder. Saying otherwise would be a lie.

  * Consensus is stake-weighted across validators (val0 ~0.636, val153 ~0.364,
    and the split drifts -- val0 has gained ~14% stake over 11 rounds). Never
    average the per-validator scores by hand: `standings[].consensus_score` is
    authoritative and `validators[].weight` carries the live split.

  * `required_score` is NOT the bar for the next round. It is
    `champion_score_before + margin` -- the bar that applied INSIDE the round
    reporting it, already settled by the time we read it. Checked against ten
    published rounds it never once equalled the following round's bar, and on
    2026-08-25 it read 31.00 while the incoming champion sat at 54.20. Nor is
    the next bar derivable: the champion's score is re-measured as a
    `champion_defense` entry each round on freshly drawn tasks, so round
    2026-08-23's winner at 86.65 carried into 2026-08-24 as 85.65.

Our own entries are matched by HOTKEY, not uid: uids get reassigned when a
registration displaces a neuron, and a stale uid would silently track a
stranger. `/work/sn98-npa/artifacts/our_hotkeys.json` is written by the miner
tooling.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

from ..base import (CompEvent, SubnetAdapter, changed, diff_block, esc,
                    fetch_json, num, short, two_col)

API = "https://api.neverplayalone.ai"
BLOCK_SECONDS = 12


def _hours(blocks) -> str:
    try:
        h = (float(blocks) * BLOCK_SECONDS) / 3600.0
    except (TypeError, ValueError):
        return "?"
    return f"{h:.1f}h" if h >= 1 else f"{int(h * 60)}m"


class SN98(SubnetAdapter):
    # ---- identity -----------------------------------------------------------
    netuid = 98
    slug = "npa"
    label = "Never Play Alone"

    # A round only changes state a few times a day; the one time-critical fact
    # is the submission window closing, and 5 min is fine resolution for that.
    poll_seconds = 300

    # The bench repo matters more than the subnet repo: `crafting_v2/configs/
    # iron.yaml` there defines task duration, the target catalog and the scoring
    # bands, so a commit changes the GAME. The v3 branch is watched by name
    # because it moves to a diamond tier at 900s with iron tools given -- if it
    # merges, every iron optimisation we have is worthless, and that is the
    # single most valuable thing this bot can tell us early.
    repos: list[str] = [
        "neverplayalone/neverplayalone_bench",
        "neverplayalone/neverplayalone_bench@feat/crafting-mission-v3",
        "neverplayalone/neverplayalone_subnet",
    ]

    links = {
        "dashboard": "https://neverplayalone.ai",
        "api": API,
    }

    watchfile = Path(os.getenv("SN98_WATCHFILE",
                               "/work/sn98-npa/artifacts/our_hotkeys.json"))

    alerts = {
        "round_published": "a round completed — winner, field size, our rank, "
                           "and the bar for next round",
        "king_change": "the crown moved",
        "round_open": "a new submission window opened",
        "window_closing": "the window is closing (<6h / <3h / <1h)",
        "new_entrant": "uids on the board that were not there last round",
    }

    def render_guide(self, s: dict) -> str:
        base = super().render_guide(s)
        return base + (
            "\n\n<b>How this subnet works</b>\n"
            "Winner-take-all. A round lasts <b>48h</b> but a new one opens every "
            "<b>24h</b>, so three are always in flight. Round N's end IS round "
            "N+1's submission close — <b>every submission is blind to the "
            "previous round's result</b>. Do not wait for results to decide.\n\n"
            "Score is the mean over 5 tasks per validator, then stake-weighted "
            "across validators (val0 ~64%, val153 ~36%, and drifting). One "
            "catastrophic task costs 20% of that validator's mean, and entry "
            "count cannot rescue it — <b>consistency beats peak</b>.\n\n"
            "<b>What this bot cannot tell you</b>\n"
            "In-flight rosters are private (<code>403</code>) and "
            "<code>n_miners</code> is null until a round completes, so "
            "<b>who submitted to the open round is unknowable</b>. The closest "
            "proxy is the operator count under ⛓ CHAIN: a competitor must "
            "burn-register before they can submit.\n\n"
            "<b>A hotkey is a 3-submission lifetime ticket</b>; use_count counts "
            "distinct rounds, and re-submitting inside a round is free — so "
            "submit early and upgrade until the deadline."
        )

    # ---- 1. COLLECT ---------------------------------------------------------
    async def snapshot(self) -> dict:
        s: dict = {}

        rounds = await fetch_json(f"{API}/api/v1/rounds")
        if not rounds or not rounds.get("data"):
            return s
        data = rounds["data"]
        rs = data["rounds"] if isinstance(data, dict) and "rounds" in data else data
        if not rs:
            return s
        rs = sorted(rs, key=lambda r: r.get("round_id") or "", reverse=True)

        evalr = next((r for r in rs if r.get("status") == "evaluating"), None)
        done = next((r for r in rs if r.get("status") == "completed"), None)

        # The OPEN round is not in /api/v1/rounds at all -- that list lags by a
        # round and /api/v1/rounds/{next} 404s. `/miner/rounds/current` is the
        # only source for the submission window, but it carries no block height,
        # so `current_block` comes from /api/v1/rounds/current.
        openr = None
        mc = await fetch_json(f"{API}/miner/rounds/current")
        if mc and mc.get("submission_round"):
            openr = mc["submission_round"]
        cur = None
        rc = await fetch_json(f"{API}/api/v1/rounds/current")
        if rc and rc.get("data"):
            cur = rc["data"].get("current_block")

        if openr:
            close = openr.get("evaluation_start_block")
            s["open_round"] = openr.get("round_id")
            if cur is not None and close is not None:
                s["open_blocks_left"] = close - cur
                # Bucketed so a normal countdown does not fire an event every
                # poll; only crossing a threshold is worth a message.
                left = (close - cur) * BLOCK_SECONDS / 3600.0
                s["open_bucket"] = ("<1h" if left < 1 else "<3h" if left < 3
                                    else "<6h" if left < 6 else "plenty")
        if evalr:
            s["eval_round"] = evalr.get("round_id")

        if done:
            s["last_round"] = done.get("round_id")
            s["n_miners"] = done.get("n_miners")
            s["king_uid"] = done.get("winner_uid")
            s["king_score"] = done.get("winner_score")
            s["required"] = done.get("required_score")
            s["margin"] = done.get("margin")
            s["dethroned"] = done.get("dethroned")
            s["champ_before"] = done.get("champion_uid_before")
            s["champ_after"] = done.get("champion_uid_after")

            vals = done.get("validators") or []
            if vals:
                s["validators"] = [{"uid": v.get("uid"),
                                    "weight": v.get("weight"),
                                    "stake": v.get("stake")} for v in vals]

            st = done.get("standings") or []
            if st:
                s["board"] = [{
                    "rank": r.get("rank"),
                    "uid": r.get("miner_uid"),
                    "hk": r.get("miner_hotkey") or "",
                    "score": r.get("consensus_score"),
                    "kind": r.get("entry_kind"),
                    # `is_king` comes back False for every row, including the
                    # champion's own defence entry -- entry_kind is the real signal.
                    "king": r.get("entry_kind") == "champion_defense",
                    # per_validator is a LIST of {validator_uid, score, weight, ...}
                    "per_val": {str(v.get("validator_uid")): v.get("score")
                                for v in (r.get("per_validator") or [])},
                } for r in st]
                s["board_uids"] = sorted(str(r["uid"]) for r in s["board"]
                                         if r.get("uid") is not None)
                s["ours"] = self._ours(s["board"])

        return s

    def _ours(self, board: list[dict]) -> dict:
        """Match our entries by HOTKEY. uids are reassigned; hotkeys are not."""
        mine = {}
        try:
            for row in json.loads(self.watchfile.read_text()):
                if row.get("ss58"):
                    mine[row["ss58"]] = row.get("hotkey") or ""
        except Exception:  # noqa: BLE001 -- absent or unreadable file is normal
            return {}
        out: dict = {}
        for r in board:
            if r.get("hk") in mine:
                out[mine[r["hk"]]] = {
                    "uid": r.get("uid"),
                    "rank": r.get("rank"),
                    "score": r.get("score"),
                    "kind": r.get("kind"),
                    # The ss58 goes back out again so render_board can colour a
                    # row on the SAME key this matched on. Matching the board
                    # on uid instead would reintroduce exactly the bug this
                    # function exists to avoid: uids are reassigned when a
                    # registration displaces a neuron, so a stale uid paints a
                    # stranger's row green.
                    "hk": r.get("hk"),
                }
        return out

    async def seed_watchlist(self) -> list[dict]:
        # Our entries are resolved from the board by hotkey in _ours(), so
        # nothing needs to be typed into /watch for this subnet.
        return []

    # ---- 2. DIFF ------------------------------------------------------------
    def diff(self, old: dict, new: dict) -> list[CompEvent]:
        out: list[CompEvent] = []
        if not old:
            return out

        # A round publishing is the only moment real information arrives.
        if changed(old, new, "last_round"):
            board = new.get("board") or []
            ours = new.get("ours") or {}
            best = min((o.get("rank") or 999) for o in ours.values()) if ours else None
            lines = [f"👑 uid {new.get('king_uid')} · "
                     f"<b>{num(new.get('king_score'))}</b>",
                     f"field {new.get('n_miners', '?')} miners"]
            if best:
                lines.append(f"our best: <b>#{best}</b> of {new.get('n_miners', '?')}")
            for hk, o in sorted(ours.items(), key=lambda kv: kv[1].get("rank") or 999):
                lines.append(f"  {esc(hk)} · #{o.get('rank')} · {num(o.get('score'))}")
            if new.get("required") is not None:
                # The bar that applied INSIDE this round, not the next one --
                # it is champion-before + margin and is already settled. The
                # incoming champion's score is re-measured as a defence entry
                # next round, so next round's bar is not derivable from here.
                lines.append(f"bar in this round: "
                             f"<b>{num(new.get('required'))}</b> "
                             f"(champion uid {new.get('champ_before')} "
                             f"+ margin {num(new.get('margin'))})")
            out.append(CompEvent(
                kind="round_published", severity="warn",
                title=f"SN98 {esc(new.get('last_round'))} published",
                body="\n".join(lines),
                dedup_key=str(new.get("last_round")),
            ))

        # Crown change is the headline; it is separate from the round event
        # because the champion can hold across several rounds.
        if changed(old, new, "king_uid") and old.get("king_uid") is not None:
            out.append(CompEvent(
                kind="king_change", severity="critical",
                title="👑 New SN98 champion",
                body=f"uid <b>{new.get('king_uid')}</b> at "
                     f"{num(new.get('king_score'))}\n"
                     f"was uid {old.get('king_uid')} at {num(old.get('king_score'))}",
                dedup_key=f"{new.get('last_round')}:{new.get('king_uid')}",
            ))

        # The submission window. Rounds are blind to the previous result, so
        # this countdown IS the actionable fact.
        if changed(old, new, "open_round"):
            out.append(CompEvent(
                kind="round_open", severity="warn",
                title=f"SN98 {esc(new.get('open_round'))} open for submissions",
                body=f"closes in {_hours(new.get('open_blocks_left'))} · "
                     f"bar to beat: <b>{num(new.get('required'))}</b>",
                dedup_key=str(new.get("open_round")),
            ))
        elif changed(old, new, "open_bucket") and new.get("open_bucket") != "plenty":
            out.append(CompEvent(
                kind="window_closing",
                severity="critical" if new.get("open_bucket") == "<1h" else "warn",
                title=f"⏳ SN98 window {new.get('open_bucket')}",
                body=f"round {esc(new.get('open_round'))} closes in "
                     f"{_hours(new.get('open_blocks_left'))}",
                dedup_key=f"{new.get('open_round')}:{new.get('open_bucket')}",
            ))

        if changed(old, new, "board_uids"):
            fresh = [u for u in new["board_uids"]
                     if u not in set(old.get("board_uids") or [])]
            if fresh:
                out.append(CompEvent(
                    kind="new_entrant", severity="info",
                    title=f"{len(fresh)} new uid(s) on the SN98 board",
                    body=", ".join(esc(u) for u in fresh[:15]),
                    dedup_key=",".join(sorted(fresh)),
                ))
        return out

    # ---- 3. RENDER ----------------------------------------------------------
    def render_state(self, s: dict) -> str:
        L = [f"<b>SN{self.netuid} · {esc(self.label)}</b>", ""]
        if s.get("open_round"):
            L.append(f"<b>📥 OPEN</b>  {esc(s['open_round'])} · closes in "
                     f"<b>{_hours(s.get('open_blocks_left'))}</b>")
        if s.get("eval_round"):
            L.append(f"<b>⏳ EVALUATING</b>  {esc(s['eval_round'])} "
                     f"<i>(field withheld until it publishes)</i>")
        if s.get("last_round"):
            L.append(f"<b>👑 LAST</b>  {esc(s['last_round'])} · uid "
                     f"{s.get('king_uid')} · <code>{num(s.get('king_score'))}</code>"
                     f" · {s.get('n_miners', '?')} miners")
            if s.get("required") is not None:
                L.append(f"<b>🎯 BAR</b>  {num(s.get('required'))} to dethrone")
        ours = s.get("ours") or {}
        if ours:
            L += ["", "<b>🔬 OURS</b> <i>(last published round)</i>"]
            for hk, o in sorted(ours.items(), key=lambda kv: kv[1].get("rank") or 999):
                L.append(f"<code>{esc(hk)}</code> uid {o.get('uid')} · "
                         f"#<b>{o.get('rank')}</b> · {num(o.get('score'))}")
        elif s.get("board"):
            L += ["", "<i>none of our hotkeys on the last board</i>"]
        return "\n".join(L)

    def render_info(self, s: dict) -> str:
        L = [f"<b>SN{self.netuid} · {esc(self.label)} — the rules</b>", "",
             "Winner-take-all. Rounds are <b>48h</b> but a new one opens every "
             "<b>24h</b>, so round N's end IS round N+1's submission close — "
             "<b>every submission is blind to the previous result</b>.", ""]
        L.append(two_col([
            ("open", esc(s.get("open_round") or "—")),
            ("closes in", _hours(s.get("open_blocks_left"))),
            ("evaluating", esc(s.get("eval_round") or "—")),
            ("last", esc(s.get("last_round") or "—")),
            ("bar", num(s.get("required"))),
            ("margin", num(s.get("margin"))),
        ], 22))
        vals = s.get("validators") or []
        if vals:
            L += ["", "<b>Consensus weights</b> <i>(stake-weighted, drifts)</i>"]
            L.append(two_col([(f"val{v['uid']}", f"{100 * (v.get('weight') or 0):.2f}%")
                              for v in vals], 22))
        ch = s.get("_chain") or {}
        if ch:
            L += ["", "<b>Chain</b>",
                  two_col([("reg", "OPEN" if ch.get("registration_allowed") else "closed"),
                           ("cost", f"τ{num(ch.get('burn_tao'), 4)}"),
                           ("uids", f"{ch.get('num_uids')}/{ch.get('max_uids')}")], 22)]
        for r in (s.get("_repos") or []):
            L.append(f"📦 <code>{esc(r['repo'])}</code> "
                     f"<code>{esc((r.get('sha') or '')[:10])}</code>")
        return "\n".join(L)

    def render_board(self, s: dict, limit: int = 10) -> str:
        """The published ladder, with our own rows coloured.

        Rendered through `diff_block`, so the FIRST character of each row is
        what a highlighting client paints: `+` (green) is ours, `-` (red) is
        the champion whose score sets the bar, everything else is plain. See
        base.diff_block for why the marker is a real character in a real column
        rather than styling -- it degrades to ordinary aligned monospace where a
        client does not highlight, so nothing is lost when the colour is.

        The previous version marked ours with a trailing `*`. Two things were
        wrong with that beyond the colour. It was appended straight onto a
        right-justified score, so it read as part of the number -- the same
        collision `devtools/preview_views.py` exists to catch. And it was
        mutually exclusive with the champion's `K`, so on the one board that
        matters most -- the one where we hold the crown -- the crown vanished.
        Here the colour and the tag are separate columns and both survive.
        """
        board = s.get("board") or []
        if not board:
            return ("No published board yet. In-flight rounds are private "
                    "(<code>403</code>) until they complete.")
        vals = [str(v["uid"]) for v in (s.get("validators") or [])]
        vals = sorted(vals, key=lambda u: -(next(
            (v.get("weight") or 0) for v in s["validators"] if str(v["uid"]) == u)))
        # ss58 -> the label we know it by, so a green row can also say WHICH of
        # our hotkeys it is. Keyed on the hotkey for the reason _ours() is:
        # matching the board on uid would paint a stranger's row after a
        # registration reassigned the uid.
        ours = {o["hk"]: name for name, o in (s.get("ours") or {}).items()
                if o.get("hk")}

        # Two leading spaces: the marker occupies a column of its own, and the
        # header has to sit above the numbers, not above the marker.
        head = f"  {'#':<3}{'consensus':>10}{'uid':>5}"
        for u in vals:
            head += f"{'v' + u:>7}"
        rows = [head]

        def line(r: dict) -> str:
            mine = r.get("hk") in ours
            out = (f"{'+' if mine else '-' if r.get('king') else ' '} "
                   f"{str(r.get('rank') or '?'):<3}{num(r.get('score')):>10}"
                   f"{str(r.get('uid') or '—'):>5}")
            for u in vals:
                out += f"{num(r['per_val'].get(u), 1):>7}"
            # esc() BEFORE the block: a <pre> is still HTML-parsed, and one
            # stray `<` makes Telegram reject the whole message.
            tag = " ".join(x for x in ("K" if r.get("king") else "",
                                       esc(ours.get(r.get("hk"), ""))) if x)
            return out + ("  " + tag if tag else "")

        rows += [line(r) for r in board[:limit]]

        # Our rows are shown even when they fall below the cut. A board that
        # answers "where am I" by truncating us out of it answers nothing, and
        # the field only grows -- 9 miners today, but /board 5 already hides us.
        below = [r for r in board[limit:] if r.get("hk") in ours]
        if below:
            rows.append("  ...")
            rows += [line(r) for r in below]

        L = [f"<b>SN98 · {esc(s.get('last_round'))}</b> — "
             f"{s.get('n_miners', len(board))} miners",
             diff_block(rows)]
        if s.get("required") is not None:
            # NOT "the bar for next round". `required_score` is champion-BEFORE
            # plus margin, i.e. the bar that applied inside this round and has
            # already been settled. Across ten published rounds it never once
            # equalled the next round's bar, and on 2026-08-25 it read 31.00
            # while the incoming champion sat at 54.20 -- a 23-point error in
            # the direction that would green-light too weak a submission.
            L.append(f"🎯 the bar <i>in this round</i> was "
                     f"<b>{num(s.get('required'))}</b> "
                     f"<i>(champion uid {s.get('champ_before')} + margin "
                     f"{num(s.get('margin'))})</i>")
        legend = ["<i>green + = ours"]
        if any(r.get("king") for r in board):
            legend.append("red - = champion")
        legend.append("K = the champion row</i>")
        L.append(" · ".join(legend))
        if not ours:
            L.append("<i>None of our hotkeys placed in this round. Identity "
                     "comes from <code>artifacts/our_hotkeys.json</code>.</i>")
        if s.get("eval_round"):
            L.append(f"<i>{esc(s['eval_round'])} still evaluating.</i>")
        return "\n".join(L)

    def render_me(self, s: dict) -> str:
        ours = s.get("ours") or {}
        if not ours:
            if not s.get("board"):
                return "No published board yet."
            return ("None of our hotkeys placed in "
                    f"{esc(s.get('last_round'))}.\n"
                    "<i>Identity comes from "
                    "<code>sn98-npa/artifacts/our_hotkeys.json</code>.</i>")
        n = s.get("n_miners") or "?"
        L = [f"<b>Our SN98 entries</b> · {esc(s.get('last_round'))}", ""]
        for hk, o in sorted(ours.items(), key=lambda kv: kv[1].get("rank") or 999):
            gap = ""
            if o.get("score") is not None and s.get("king_score") is not None:
                gap = f" · {o['score'] - s['king_score']:+.2f} vs 👑"
            L.append(f"<code>{esc(hk)}</code> uid <b>{o.get('uid')}</b> · "
                     f"#<b>{o.get('rank')}</b>/{n} · {num(o.get('score'))}{gap}")
        return "\n".join(L)

    def ours_summary(self, s: dict) -> str:
        ours = s.get("ours") or {}
        if not ours:
            return ""
        ranked = [o.get("rank") for o in ours.values() if o.get("rank")]
        if ranked:
            # snapshot() records the field size as `n_miners`; `n_board`
            # was never set, so this read None and the " of N" -- the half that
            # makes a rank mean anything -- silently never rendered.
            n = s.get("n_miners") or len(s.get("board") or []) or None
            of = f" of {n}" if n else ""
            return f"best #{min(ranked)}{of} · {len(ours)} entered"
        return f"{len(ours)} entered, unranked"

    def render_digest(self, s: dict) -> str:
        ours = s.get("ours") or {}
        best = min((o.get("rank") or 999) for o in ours.values()) if ours else None
        bit = f" · ours #{best}" if best else ""
        win = ""
        if s.get("open_blocks_left") is not None:
            win = f" · window {_hours(s.get('open_blocks_left'))}"
        return (f"<b>SN98</b> {esc(self.label)} — 👑 uid {s.get('king_uid')} "
                f"<code>{num(s.get('king_score'))}</code>{bit}{win}")
