"""SN108 Prometheon.

Scaffolded from _template.py. Fill in snapshot() against the real
dashboard, then decide in diff() what is worth a message.
Validate with:  python devtools/smoke_comp.py 108
"""
from __future__ import annotations

from ..base import (CompEvent, SubnetAdapter, changed, clean_error, esc,
                    fetch_json, num, pct, sci, short, two_col)

API = "https://example-subnet-dashboard.ai"


class SN108(SubnetAdapter):
    # ---- identity -----------------------------------------------------------
    netuid = 108
    slug = "prometheon"
    label = "Prometheon"

    # How often to poll. Match the cadence at which this subnet's state can
    # actually change: a per-round subnet with a 6h round does not need 60s.
    poll_seconds = 300

    # Watched generically. A commit here is usually the earliest warning that
    # the rules changed -- earlier than the dashboard, earlier than any
    # announcement. "owner/repo" or "owner/repo@branch".
    repos: list[str] = []

    # Surfaced by /info.
    links = {"dashboard": API}

    # ---- 1. COLLECT ---------------------------------------------------------
    async def snapshot(self) -> dict:
        """Return the subnet's competition state as a flat dict of scalars.

        THE ONE RULE: if a source fails, OMIT its keys. Never substitute a zero
        or a None. `fetch_json` returns None on failure precisely so that this
        stays easy -- guard on it and skip.

        Substituting a default is how a dashboard outage becomes a fake "the
        rules changed" alert at 3am.
        """
        s: dict = {}

        rules = await fetch_json(f"{API}/v1/competition")
        if rules:
            s["comp_id"] = rules.get("id")
            s["round_ends"] = rules.get("ends_at")
            s["entry_cost"] = rules.get("entry_cost")

        board = await fetch_json(f"{API}/v1/leaderboard?limit=50")
        if board and board.get("items"):
            items = board["items"]
            s["n_board"] = board.get("total") or len(items)
            s["board"] = [{
                "rank": r.get("rank"),
                "hk": r.get("hotkey", ""),
                "uid": r.get("uid"),
                "score": r.get("score"),
            } for r in items[:20]]
            top = s["board"][0]
            s["king_hk"] = top["hk"]
            s["king_uid"] = top["uid"]
            s["king_score"] = top["score"]
            # A sorted list of names, so `changed()` can spot new entrants
            # without the ordering of the API's response creating false diffs.
            s["board_hk"] = sorted({r["hk"] for r in s["board"] if r["hk"]})

        s["ours"] = await self._ours()
        return s

    async def _ours(self) -> dict:
        """Per-entry state for everything on our watchlist.

        `store.watched(self.netuid)` is populated by /watch and by
        seed_watchlist() below.
        """
        from .. import store
        out: dict = {}
        for row in await store.watched(self.netuid):
            d = await fetch_json(f"{API}/v1/entries/{row['ref']}")
            if not d:
                continue
            out[row["ref"][:8]] = {
                "uid": row.get("uid"),
                "label": row.get("label") or "",
                "stage": d.get("stage"),
                "status": d.get("status"),
                "score": d.get("score"),
                "rank": d.get("rank"),
                "err": (d.get("error") or "")[:800],   # cleaned at render time
            }
        return out

    async def seed_watchlist(self) -> list[dict]:
        """Optional: read our own entry ids off disk so nothing is typed twice.

        /home/dev/work is mounted read-only at /work, so point this at whatever
        file your miner tooling already writes.

            rows = json.loads(Path("/work/snXX/artifacts/live.json").read_text())
            return [{"ref": r["id"], "label": r["label"], "uid": r["uid"]} for r in rows]
        """
        return []

    # ---- 2. DIFF ------------------------------------------------------------
    def diff(self, old: dict, new: dict) -> list[CompEvent]:
        """What changed, and is it worth a message?

        TWO RULES:
          * `if not old: return []` -- first sight is a baseline. Without this,
            every restart replays the whole competition into the chat.
          * Compare through `changed(old, new, *keys)`, never raw `.get()`s.
            changed() returns False when a key is MISSING from `new`, which is
            what makes an API outage silent instead of alarming.

        Severity decides whether the phone buzzes:
          info / good  -> silent
          warn         -> buzzes; something needs a decision soon
          critical     -> buzzes; the ground moved
        """
        out: list[CompEvent] = []
        if not old:
            return out

        if changed(old, new, "comp_id"):
            out.append(CompEvent(
                kind="rules", severity="critical",
                title=f"New competition on SN{self.netuid}",
                body=f"<code>{esc(old.get('comp_id'))}</code> → "
                     f"<code>{esc(new.get('comp_id'))}</code>",
                dedup_key=str(new.get("comp_id")),
            ))

        if changed(old, new, "king_hk") and old.get("king_hk"):
            out.append(CompEvent(
                kind="king_change", severity="critical",
                title=f"👑 New leader on SN{self.netuid}",
                body=f"{esc(short(new['king_hk']))} (uid {new.get('king_uid')}) "
                     f"at {num(new.get('king_score'))}\n"
                     f"was {esc(short(old.get('king_hk')))} at {num(old.get('king_score'))}",
                dedup_key=str(new.get("king_hk")),
            ))
        elif changed(old, new, "king_score"):
            out.append(CompEvent(
                kind="king_score", severity="info",
                title=f"Leader's score → {num(new.get('king_score'))}",
                body=f"same hotkey {esc(short(new.get('king_hk')))}",
                dedup_key=str(new.get("king_score")),
            ))

        if changed(old, new, "board_hk"):
            fresh = [h for h in new["board_hk"]
                     if h not in set(old.get("board_hk") or [])]
            if fresh:
                out.append(CompEvent(
                    kind="new_entrant", severity="info",
                    title=f"{len(fresh)} new on the board",
                    body="\n".join(esc(short(h)) for h in fresh[:10]),
                    dedup_key=",".join(sorted(fresh)),
                ))

        # Our own entries: every transition matters, so no cooldown.
        for ref, cur in (new.get("ours") or {}).items():
            was = (old.get("ours") or {}).get(ref)
            if was is None or was.get("stage") == cur.get("stage"):
                continue
            dead = cur.get("stage") in ("failed", "rejected")
            body = f"{esc(cur.get('label') or '')}\n{was.get('stage')} → " \
                   f"<b>{esc(cur.get('stage'))}</b>"
            if dead and cur.get("err"):
                body += f"\n↳ <code>{esc(clean_error(cur['err']))}</code>"
            out.append(CompEvent(
                kind="our_run", severity="warn" if dead else "good",
                title=f"Our entry {ref} → {cur.get('stage')}",
                body=body, dedup_key=f"{ref}:{cur.get('stage')}",
            ))
        return out

    # ---- 3. RENDER ----------------------------------------------------------
    # Telegram HTML only: <b> <i> <u> <s> <code> <pre> <a> <blockquote>.
    # Anything else makes Telegram reject the WHOLE message. esc() everything
    # that came from an API. two_col() gives aligned monospace on a phone.
    def render_state(self, s: dict) -> str:
        L = [f"<b>SN{self.netuid} · {esc(self.label)}</b>", ""]
        L.append(f"<b>👑 LEADER</b>  <code>{num(s.get('king_score'))}</code>")
        L.append(f"{esc(short(s.get('king_hk')))} · uid {s.get('king_uid')}")
        L.append(f"<b>📥 FIELD</b>  {s.get('n_board', '?')} entries")
        L.append("")
        L.append("<b>🔬 OURS</b>")
        for ref, o in (s.get("ours") or {}).items():
            L.append(f"<code>{ref}</code> uid {o.get('uid')} · {esc(o.get('stage'))}")
        return "\n".join(L)

    def render_info(self, s: dict) -> str:
        ch = s.get("_chain") or {}      # filled in for free by the poller
        L = [f"<b>SN{self.netuid} · {esc(self.label)} — the rules</b>", "",
             two_col([("comp", esc(s.get("comp_id") or "?")),
                      ("ends", esc(s.get("round_ends") or "—"))], 24)]
        if ch:
            L += ["", "<b>Chain</b>",
                  two_col([("reg", "OPEN" if ch.get("registration_allowed") else "closed"),
                           ("cost", f"τ{num(ch.get('burn_tao'), 4)}"),
                           ("uids", f"{ch.get('num_uids')}/{ch.get('max_uids')}"),
                           ("operators", str(ch.get("unique_coldkeys")))], 22)]
        for r in (s.get("_repos") or []):
            L.append(f"📦 <code>{esc(r['repo'])}</code> "
                     f"<code>{esc((r.get('sha') or '')[:10])}</code>")
        return "\n".join(L)

    def render_board(self, s: dict, limit: int = 10) -> str:
        board = s.get("board") or []
        if not board:
            return "Board unavailable."
        rows = [f"{'#':<2} {'score':>9} {'uid':>4}"]
        for r in board[:limit]:
            rows.append(f"{r['rank']:<2} {num(r.get('score')):>9} "
                        f"{str(r.get('uid') or '—'):>4}")
        return (f"<b>SN{self.netuid} leaderboard</b> · {s.get('n_board', len(board))}\n\n"
                "<pre>" + "\n".join(rows) + "</pre>")

    def render_me(self, s: dict) -> str:
        ours = s.get("ours") or {}
        if not ours:
            return "Nothing tracked. <code>/watch &lt;id&gt; [label]</code>"
        L = [f"<b>Our SN{self.netuid} entries</b>", ""]
        for ref, o in ours.items():
            L.append(f"<code>{ref}</code> · uid <b>{o.get('uid')}</b> · "
                     f"<b>{esc(o.get('stage'))}</b>")
            if o.get("err"):
                L.append(f"   ↳ {esc(clean_error(o['err']))}")
        return "\n".join(L)

    def render_digest(self, s: dict) -> str:
        """One line for the cross-subnet /state in the digest topic."""
        return (f"<b>SN{self.netuid}</b> {esc(self.label)} — 👑 "
                f"<code>{num(s.get('king_score'))}</code> · "
                f"board {s.get('n_board', '?')} · ours {len(s.get('ours') or {})}")
