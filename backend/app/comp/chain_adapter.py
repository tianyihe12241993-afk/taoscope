"""The adapter every subnet gets when it has no competition adapter of its own.

A topic exists for every subnet one of our coldkeys holds a UID on. Most of
those subnets have no dashboard adapter -- writing one is real work per subnet
-- but they still deserve the same topic: the same commands answer, the same
alerts arrive, the same chain facts and the same table of our hotkeys show.
This adapter supplies all of that from our own chain tables. It makes no HTTP
call, so it cannot fail the way a dashboard does.

When a real adapter is written for the subnet, it replaces this one in the
registry and the topic keeps working unchanged.
"""
from __future__ import annotations

from ..db import pool
from .base import (SubnetAdapter, diff_block, esc, num, ours_chain_block, ours_line,
                   short, two_col)


class ChainAdapter(SubnetAdapter):
    poll_seconds = 300          # the neuron sweep behind it runs every 15 min

    def __init__(self, netuid: int, label: str):
        self.netuid = netuid
        self.slug = f"sn{netuid}"
        self.label = label
        self.links = {"taostats": f"https://taostats.io/subnets/{netuid}"}
        self.repos = []

    async def snapshot(self) -> dict:
        s = await pool().fetchrow(
            "SELECT name, symbol, price, market_cap_tao, tempo, immunity_period,"
            "       owner_hotkey, chain_github, chain_url, chain_description,"
            "       validator_count, active_uids"
            " FROM subnet_live WHERE netuid=$1", self.netuid)
        if s is None:
            return {}
        if s["name"]:
            self.label = s["name"]
        out = dict(s)
        board = await pool().fetch(
            "SELECT n.uid, n.hotkey, n.coldkey, n.incentive,"
            "       n.emission * (7200.0 / NULLIF(t.tempo, 0)) * t.price AS tpd,"
            "       EXISTS (SELECT 1 FROM my_coldkey m WHERE m.coldkey = n.coldkey) AS ours"
            " FROM neuron_live n JOIN subnet_live t ON t.netuid = n.netuid"
            " WHERE n.netuid = $1 AND n.emission > 0"
            " ORDER BY n.emission DESC LIMIT 20", self.netuid)
        # `_`-prefixed: view data, saved with the state but not counted as a
        # change, so a moving board does not write a history row every sweep.
        out["_board"] = [{"uid": r["uid"], "hk": r["hotkey"] or "",
                          "ck": r["coldkey"] or "", "tpd": float(r["tpd"] or 0),
                          "ours": bool(r["ours"])} for r in board]
        return out

    def diff(self, old: dict, new: dict) -> list:
        # Every alert this subnet can raise is generic: registration, operators
        # and our earnings come from the poller, deregistration and the top
        # earner from the chain sweep.
        return []

    def render_state(self, s: dict) -> str:
        L = [f"<b>SN{self.netuid} · {esc(self.label)}</b>",
             "<i>chain view — no competition adapter for this subnet</i>"]
        rows = [("price", f"τ{num(s.get('price'), 5)}"),
                ("mcap", f"τ{num(s.get('market_cap_tao'), 0)}"),
                ("validators", str(s.get("validator_count") or "?")),
                ("active", str(s.get("active_uids") or "?"))]
        L.append(two_col(rows, 20))
        top = (s.get("_board") or [])[:1]
        if top:
            t = top[0]
            L.append(f"<b>👑 TOP</b> uid {t['uid']} · {esc(short(t['hk']))} · "
                     f"τ{num(t['tpd'], 2)}/day" + (" · <b>ours</b>" if t["ours"] else ""))
        return "\n".join(L)

    def render_info(self, s: dict) -> str:
        L = [f"<b>SN{self.netuid} · {esc(self.label)} — on chain</b>", ""]
        if s.get("chain_description"):
            L += [esc(str(s["chain_description"])[:300]), ""]
        rows = [("tempo", str(s.get("tempo") or "?")),
                ("immunity", f"{s.get('immunity_period') or '?'} blk")]
        L.append(two_col(rows, 20))
        if s.get("owner_hotkey"):
            L.append(f"owner <code>{esc(short(s['owner_hotkey']))}</code>")
        links = dict(self.links)
        for k in ("chain_github", "chain_url"):
            if s.get(k):
                links[k.removeprefix("chain_")] = str(s[k])
        L.append(" · ".join(f'<a href="{esc(v)}">{esc(k)}</a>' for k, v in links.items()))
        return "\n".join(L)

    def render_board(self, s: dict, limit: int = 10) -> str:
        board = (s.get("_board") or [])[:limit]
        if not board:
            return f"<b>SN{self.netuid} earners</b>\n<i>nobody is earning here right now</i>"
        L = [f"{'':2}{'#':>2} {'uid':>4} {'hotkey':<13} {'τ/day':>8}"]
        for i, r in enumerate(board, 1):
            L.append(f"{'+ ' if r['ours'] else '  '}{i:>2} {r['uid']:>4} "
                     f"{esc(short(r['hk'], 6, 4)):<13} {num(r['tpd'], 2):>8}")
        return (f"<b>SN{self.netuid} top earners</b> · by emission\n" + diff_block(L)
                + "\n<i>+ = ours</i>")

    def render_me(self, s: dict) -> str:
        return ours_chain_block(s)

    def render_digest(self, s: dict) -> str:
        return f"<b>SN{self.netuid}</b> {esc(self.label)} — chain"

    def ours_summary(self, s: dict) -> str:
        return ours_line(s)
