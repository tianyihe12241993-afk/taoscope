"""Detect the events a miner actually wants to hear about.

Everything is derived by diffing consecutive chain sweeps — no extra RPC calls.
Each kind is de-duplicated over a cooldown window so a flapping value cannot
spam the chat.
"""
import json
import logging

from .db import pool

log = logging.getLogger("taoscope.events")

# kind -> (cooldown hours, severity)
KINDS = {
    "new_subnet":    (24, "good"),
    "subnet_started": (24, "good"),
    "king_change":   (6,  "info"),
    "registration":  (2,  "info"),
    "emission_move": (6,  "info"),
    "my_miners":     (1,  "warn"),
}


async def capture_previous() -> dict[int, dict]:
    """State to diff the next sweep against, read before subnet_live is updated."""
    rows = await pool().fetch(
        "SELECT netuid, name, top_coldkey, top_coldkey_pct, registration_allowed,"
        "       num_uids, emission_share FROM subnet_live"
    )
    return {r["netuid"]: dict(r) for r in rows}


async def capture_my_uids() -> dict[tuple[int, int, int], str]:
    """(user_id, netuid, uid) -> hotkey, for every UID under a registered coldkey.

    Keyed by user so a deregistration alert only ever reaches the person who
    owns that coldkey.
    """
    rows = await pool().fetch(
        "SELECT m.user_id, n.netuid, n.uid, n.hotkey FROM neuron_live n"
        " JOIN my_coldkey m ON m.coldkey = n.coldkey"
    )
    return {(r["user_id"], r["netuid"], r["uid"]): r["hotkey"] for r in rows}


async def _recently_sent(kind: str, netuid: int | None, hours: int,
                         user_id: int | None) -> bool:
    return bool(await pool().fetchval(
        "SELECT 1 FROM chain_event WHERE kind=$1 AND netuid IS NOT DISTINCT FROM $2"
        "   AND user_id IS NOT DISTINCT FROM $3"
        "   AND ts > now() - ($4 || ' hours')::interval LIMIT 1",
        kind, netuid, user_id, str(hours),
    ))


async def emit(kind: str, title: str, body: str = "", netuid: int | None = None,
               detail: dict | None = None, user_id: int | None = None) -> dict | None:
    """Record an event unless an identical one is inside its cooldown."""
    hours, severity = KINDS.get(kind, (6, "info"))
    if await _recently_sent(kind, netuid, hours, user_id):
        return None
    row = await pool().fetchrow(
        "INSERT INTO chain_event (kind, netuid, severity, title, body, detail, user_id)"
        " VALUES ($1,$2,$3,$4,$5,$6::jsonb,$7) RETURNING *",
        kind, netuid, severity, title, body, json.dumps(detail or {}), user_id,
    )
    log.info("event %s: %s", kind, title)
    return dict(row)


async def detect_subnet_events(prev: dict[int, dict], aggs: list[dict],
                               names: dict[int, str]) -> list[dict]:
    """New subnets, king changes and registration flips."""
    out: list[dict] = []
    for a in aggs:
        netuid = a["netuid"]
        name = names.get(netuid) or f"subnet-{netuid}"
        before = prev.get(netuid)

        if before is None:
            ev = await emit("new_subnet", f"New subnet SN{netuid} — {name}",
                            "Just appeared on the network.", netuid,
                            {"name": name})
            if ev:
                out.append(ev)
            continue

        # king = the coldkey taking the largest slice of this subnet's emission
        old_king, new_king = before.get("top_coldkey"), a.get("top_coldkey")
        if old_king and new_king and old_king != new_king and (a.get("top_coldkey_pct") or 0) >= 8:
            ev = await emit(
                "king_change", f"SN{netuid} {name}: new top earner",
                f"{new_king[:10]}…{new_king[-6:]} now takes "
                f"{a['top_coldkey_pct']:.1f}% (was {old_king[:10]}…{old_king[-6:]} "
                f"at {(before.get('top_coldkey_pct') or 0):.1f}%).",
                netuid, {"old": old_king, "new": new_king, "pct": a.get("top_coldkey_pct")},
            )
            if ev:
                out.append(ev)

        was_open, now_open = before.get("registration_allowed"), a.get("registration_allowed")
        if was_open is not None and now_open is not None and was_open != now_open:
            ev = await emit(
                "registration",
                f"SN{netuid} {name}: registration {'opened' if now_open else 'closed'}",
                f"Entry cost τ{(a.get('burn_tao') or 0):.4f}." if now_open else "",
                netuid, {"open": now_open, "burn": a.get("burn_tao")},
            )
            if ev:
                out.append(ev)
    return out


async def detect_my_miner_events(before: dict[tuple[int, int, int], str],
                                 names: dict[int, str]) -> list[dict]:
    """A UID we held is now someone else's — i.e. we were deregistered."""
    if not before:
        return []
    after = await capture_my_uids()
    lost: dict[tuple[int, int], int] = {}
    for (user_id, netuid, uid), hotkey in before.items():
        if after.get((user_id, netuid, uid)) != hotkey:
            k = (user_id, netuid)
            lost[k] = lost.get(k, 0) + 1

    out = []
    for (user_id, netuid), count in lost.items():
        name = names.get(netuid) or f"subnet-{netuid}"
        ev = await emit(
            "my_miners", f"Deregistered on SN{netuid} {name}",
            f"{count} of your UID(s) were taken over by another hotkey.",
            netuid, {"lost": count}, user_id=user_id,
        )
        if ev:
            out.append(ev)
    return out


async def subscribers(kind: str) -> list[tuple[int, int]]:
    """(chat_id, user_id) for every linked chat that wants this kind."""
    rows = await pool().fetch(
        "SELECT chat_id, user_id FROM telegram_link"
        " WHERE linked_at IS NOT NULL AND chat_id IS NOT NULL"
        "   AND COALESCE((prefs ->> $1)::bool, true)",
        kind,
    )
    return [(r["chat_id"], r["user_id"]) for r in rows]
