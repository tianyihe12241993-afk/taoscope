"""Database access for competition tracking."""
from __future__ import annotations

import json
import logging

from ..db import pool
from .base import CompEvent

log = logging.getLogger("taoscope.comp.store")

# kind -> default cooldown hours. An event may override it per instance.
COOLDOWN = {
    "king_change":   1.0,
    "king_score":    0.5,
    "board":         1.0,
    "new_entrant":   0.25,
    "rules":         0.0,    # never suppress: the rules changing invalidates work
    "repo":          0.0,
    "emission":      0.0,
    "sealed":        2.0,
    "our_run":       0.0,    # every stage transition of ours is wanted
    "queue":         1.0,
    "registration":  1.0,
    "error":         6.0,
}


async def register_subnet(netuid: int, slug: str, label: str) -> None:
    await pool().execute(
        "INSERT INTO comp_subnet (netuid, slug, label) VALUES ($1,$2,$3)"
        " ON CONFLICT (netuid) DO UPDATE SET slug=EXCLUDED.slug, label=EXCLUDED.label",
        netuid, slug, label,
    )
    await pool().execute(
        "INSERT INTO comp_state (netuid) VALUES ($1) ON CONFLICT DO NOTHING", netuid)


async def last_state(netuid: int) -> dict:
    row = await pool().fetchrow(
        "SELECT data, updated_at FROM comp_state WHERE netuid=$1", netuid)
    if not row:
        return {}
    d = dict(row["data"] or {})
    if row["updated_at"]:
        d["_fetched_at"] = row["updated_at"].isoformat()
    return d


async def save_state(netuid: int, data: dict, *, changed: bool,
                     error: str | None = None) -> None:
    # Only `_fetched_at` is transient -- it is injected on read. `_chain` and
    # `_repos` are view data the renderers need, and stripping every underscore
    # key silently emptied the chain and repo blocks of /info.
    # MERGE over the stored snapshot, never replace it.
    #
    # A snapshot omits any key whose source failed this round (that is the
    # absence-is-not-a-change contract). Overwriting therefore DELETED the key
    # from storage, and the next successful fetch looked like `None -> value`
    # and re-fired a critical alert. That is exactly what produced two bogus
    # "PROD pin promoted: None -> 22d60561e7" messages when prod had not moved
    # at all. Last-known-good must survive a failed fetch.
    prior = await pool().fetchval("SELECT data FROM comp_state WHERE netuid=$1", netuid)
    payload = dict(prior or {})
    payload.update({k: v for k, v in data.items() if k != "_fetched_at"})
    await pool().execute(
        "UPDATE comp_state SET data=$2::jsonb, updated_at=now(), polls=polls+1,"
        "  fails = CASE WHEN $3::text IS NULL THEN 0 ELSE fails+1 END,"
        "  last_error=$3 WHERE netuid=$1",
        netuid, json.dumps(payload, default=str), error,
    )
    if changed:
        await pool().execute(
            "INSERT INTO comp_snapshot (netuid, data) VALUES ($1,$2::jsonb)"
            " ON CONFLICT DO NOTHING",
            netuid, json.dumps(payload, default=str),
        )


def _dk(key: str) -> str:
    """Bound the dedup key. It sits in a btree index, and an adapter that joins
    a whole list into it (SN114's new_entrant: every fresh hotkey, comma-joined)
    blew past the 8191-byte index-row limit after a burst — 59 consecutive
    poll failures, no SN114 alerts, nothing said. Long keys become a prefix
    plus a hash, which keeps equality (and therefore the cooldown) intact."""
    key = key or ""
    if len(key.encode("utf-8")) <= 256:
        return key
    import hashlib
    return key[:96] + "#" + hashlib.sha256(key.encode("utf-8")).hexdigest()[:24]


async def _suppressed(netuid: int, ev: CompEvent) -> bool:
    hours = ev.cooldown_h if ev.cooldown_h else COOLDOWN.get(ev.kind, 1.0)
    if hours <= 0:
        return False
    return bool(await pool().fetchval(
        "SELECT 1 FROM comp_event WHERE netuid=$1 AND kind=$2 AND dedup_key=$3"
        "  AND ts > now() - ($4 || ' hours')::interval LIMIT 1",
        netuid, ev.kind, _dk(ev.dedup_key), str(hours),
    ))


async def record(netuid: int, ev: CompEvent) -> dict | None:
    """Persist an event unless an identical one is inside its cooldown."""
    if await _suppressed(netuid, ev):
        log.debug("suppressed %s/%s (cooldown)", ev.kind, ev.dedup_key)
        return None
    row = await pool().fetchrow(
        "INSERT INTO comp_event (netuid, kind, severity, dedup_key, title, body,"
        "                        detail, icon)"
        " VALUES ($1,$2,$3,$4,$5,$6,$7::jsonb,$8) RETURNING *",
        netuid, ev.kind, ev.severity, _dk(ev.dedup_key), ev.title, ev.body,
        json.dumps(ev.detail, default=str), ev.icon,
    )
    log.info("SN%s event %s: %s", netuid, ev.kind, ev.title)
    return dict(row)


async def mark_delivered(event_id: int) -> None:
    await pool().execute("UPDATE comp_event SET delivered=true WHERE id=$1", event_id)


async def recent_events(netuid: int | None, limit: int = 15) -> list[dict]:
    if netuid is None:
        rows = await pool().fetch(
            "SELECT * FROM comp_event ORDER BY ts DESC LIMIT $1", limit)
    else:
        rows = await pool().fetch(
            "SELECT * FROM comp_event WHERE netuid=$1 ORDER BY ts DESC LIMIT $2",
            netuid, limit)
    return [dict(r) for r in rows]


# ---------------- topic routing ----------------
async def bind_topic(chat_id: int, thread_id: int, netuid: int | None,
                     title: str = "") -> None:
    await pool().execute(
        "INSERT INTO comp_topic (chat_id, thread_id, netuid, title) VALUES ($1,$2,$3,$4)"
        " ON CONFLICT (chat_id, thread_id) DO UPDATE"
        "   SET netuid=EXCLUDED.netuid, title=EXCLUDED.title, bound_at=now()",
        chat_id, thread_id, netuid, title,
    )


async def unbind_topic(chat_id: int, thread_id: int) -> None:
    await pool().execute(
        "DELETE FROM comp_topic WHERE chat_id=$1 AND thread_id=$2", chat_id, thread_id)


async def netuid_for_topic(chat_id: int, thread_id: int) -> tuple[int | None, bool]:
    """(netuid, bound). bound=False means this topic has no binding at all."""
    row = await pool().fetchrow(
        "SELECT netuid FROM comp_topic WHERE chat_id=$1 AND thread_id=$2",
        chat_id, thread_id)
    if row is None:
        return None, False
    return row["netuid"], True


async def targets(netuid: int) -> list[tuple[int, int, dict]]:
    """(chat_id, thread_id, prefs) for every topic bound to this subnet."""
    rows = await pool().fetch(
        "SELECT chat_id, thread_id, prefs FROM comp_topic WHERE netuid=$1", netuid)
    return [(r["chat_id"], r["thread_id"], dict(r["prefs"] or {})) for r in rows]


async def digest_targets() -> list[tuple[int, int]]:
    rows = await pool().fetch(
        "SELECT chat_id, thread_id FROM comp_topic WHERE netuid IS NULL")
    return [(r["chat_id"], r["thread_id"]) for r in rows]


async def all_topics() -> list[dict]:
    rows = await pool().fetch(
        "SELECT * FROM comp_topic ORDER BY chat_id, thread_id")
    return [dict(r) for r in rows]


async def topic_for(chat_id: int, netuid: int) -> tuple[int, dict] | None:
    """(thread_id, prefs) of this chat's topic for a subnet, or None."""
    row = await pool().fetchrow(
        "SELECT thread_id, prefs FROM comp_topic WHERE chat_id=$1 AND netuid=$2"
        " ORDER BY bound_at DESC LIMIT 1", chat_id, netuid)
    return (row["thread_id"], dict(row["prefs"] or {})) if row else None


async def topic_gone(chat_id: int, thread_id: int) -> int | None:
    """Telegram says this thread no longer exists: drop the binding.

    Returns the netuid it was bound to (None for a digest). A subnet topic that
    was deleted is also recorded as skipped, so the automatic sync does not
    recreate a topic the operator removed on purpose."""
    row = await pool().fetchrow(
        "DELETE FROM comp_topic WHERE chat_id=$1 AND thread_id=$2 RETURNING netuid",
        chat_id, thread_id)
    netuid = row["netuid"] if row else None
    if netuid is not None:
        await skip_topic(chat_id, netuid, "deleted")
    return netuid


# ---------------- per-chat topic opt-out ----------------
async def skip_topic(chat_id: int, netuid: int, reason: str) -> None:
    await pool().execute(
        "INSERT INTO comp_topic_skip (chat_id, netuid, reason) VALUES ($1,$2,$3)"
        " ON CONFLICT (chat_id, netuid) DO UPDATE SET reason=EXCLUDED.reason, at=now()",
        chat_id, netuid, reason)


async def unskip_topic(chat_id: int, netuid: int | None = None) -> None:
    """Clear one opt-out, or every opt-out for the chat when netuid is None."""
    if netuid is None:
        await pool().execute("DELETE FROM comp_topic_skip WHERE chat_id=$1", chat_id)
    else:
        await pool().execute(
            "DELETE FROM comp_topic_skip WHERE chat_id=$1 AND netuid=$2", chat_id, netuid)


async def skipped_topics(chat_id: int) -> set[int]:
    rows = await pool().fetch(
        "SELECT netuid FROM comp_topic_skip WHERE chat_id=$1", chat_id)
    return {r["netuid"] for r in rows}


async def forum_chats() -> list[int]:
    """Chats that were set up as a subnet forum and belong to a linked account.

    A digest binding is what /setup leaves behind, so it marks a forum. Requiring
    the account link as well keeps test fixtures (fake chat ids written by the
    devtools checks) out of the automatic topic sync."""
    rows = await pool().fetch(
        "SELECT DISTINCT t.chat_id FROM comp_topic t"
        " JOIN telegram_link l ON l.chat_id = t.chat_id AND l.linked_at IS NOT NULL"
        " WHERE t.netuid IS NULL")
    return [r["chat_id"] for r in rows]


# ---------------- our UIDs on chain ----------------
async def held_subnets() -> dict[int, str]:
    """netuid -> chain name for every subnet a registered coldkey holds a UID on."""
    rows = await pool().fetch(
        "SELECT n.netuid, max(s.name) AS name FROM neuron_live n"
        " JOIN my_coldkey m ON m.coldkey = n.coldkey"
        " LEFT JOIN subnet_live s ON s.netuid = n.netuid"
        " GROUP BY n.netuid")
    return {r["netuid"]: (r["name"] or f"subnet-{r['netuid']}") for r in rows}


async def bound_netuids() -> set[int]:
    rows = await pool().fetch(
        "SELECT DISTINCT netuid FROM comp_topic WHERE netuid IS NOT NULL")
    return {r["netuid"] for r in rows}


async def subnet_name(netuid: int) -> str | None:
    return await pool().fetchval("SELECT name FROM subnet_live WHERE netuid=$1", netuid)


async def ours_on_chain(netuid: int) -> dict:
    """Every UID a registered coldkey holds on this subnet, from the last sweep.

    `tpd` uses the same formula as /me, so the topic and the DM agree."""
    rows = await pool().fetch(
        "SELECT n.uid, n.hotkey, n.incentive, n.rank_in_subnet, n.validator_permit,"
        "       n.block_at_registration, s.block, s.immunity_period,"
        "       n.emission * (7200.0 / NULLIF(s.tempo, 0)) * s.price AS tpd"
        " FROM neuron_live n"
        " JOIN my_coldkey m ON m.coldkey = n.coldkey"
        " JOIN subnet_live s ON s.netuid = n.netuid"
        " WHERE n.netuid = $1 ORDER BY n.uid", netuid)
    uids = []
    for r in rows:
        immune = None
        if r["block"] and r["block_at_registration"] is not None and r["immunity_period"]:
            immune = (r["block"] - r["block_at_registration"]) < r["immunity_period"]
        uids.append({"uid": r["uid"], "hk": r["hotkey"] or "",
                     "inc": float(r["incentive"] or 0),
                     "tpd": float(r["tpd"] or 0),
                     "rank": r["rank_in_subnet"],
                     "vp": bool(r["validator_permit"]),
                     "immune": immune})
    return {"n": len(uids),
            "earning": sum(1 for u in uids if u["tpd"] > 0),
            "tpd": sum(u["tpd"] for u in uids),
            "uids": uids}


async def seen_kinds(netuid: int) -> list[str]:
    rows = await pool().fetch(
        "SELECT DISTINCT kind FROM comp_event WHERE netuid=$1", netuid)
    return [r["kind"] for r in rows]


async def set_pref(chat_id: int, thread_id: int, kind: str, on: bool) -> None:
    await pool().execute(
        "UPDATE comp_topic SET prefs = prefs || jsonb_build_object($3::text, $4::bool)"
        " WHERE chat_id=$1 AND thread_id=$2",
        chat_id, thread_id, kind, on,
    )


# ---------------- per-topic selection ----------------
# `prefs` otherwise maps an event KIND to a bool (the /mute flags). A selection
# is a list, so it lives under a reserved key that no event kind can collide
# with, and the mute reader (`prefs.get(kind) is False`) ignores it for free.
PICK_KEY = "_pick"


async def get_pick(chat_id: int, thread_id: int) -> list[str]:
    """Which items this topic has narrowed itself to. [] means 'everything'."""
    row = await pool().fetchrow(
        "SELECT prefs FROM comp_topic WHERE chat_id=$1 AND thread_id=$2",
        chat_id, thread_id)
    val = (dict(row["prefs"] or {}) if row else {}).get(PICK_KEY)
    return [str(v) for v in val] if isinstance(val, list) else []


async def set_pick(chat_id: int, thread_id: int, values: list[str]) -> None:
    await pool().execute(
        "UPDATE comp_topic SET prefs = prefs || jsonb_build_object($3::text, $4::jsonb)"
        " WHERE chat_id=$1 AND thread_id=$2",
        chat_id, thread_id, PICK_KEY, json.dumps([str(v) for v in values]),
    )


# ---------------- watchlist ----------------
async def watched(netuid: int) -> list[dict]:
    rows = await pool().fetch(
        "SELECT ref, label, uid FROM comp_watch WHERE netuid=$1 AND active"
        " ORDER BY added_at", netuid)
    return [dict(r) for r in rows]


async def add_watch(netuid: int, ref: str, label: str = "", uid: int | None = None) -> None:
    await pool().execute(
        "INSERT INTO comp_watch (netuid, ref, label, uid) VALUES ($1,$2,$3,$4)"
        " ON CONFLICT (netuid, ref) DO UPDATE"
        "   SET label=COALESCE(NULLIF(EXCLUDED.label,''), comp_watch.label),"
        "       uid=COALESCE(EXCLUDED.uid, comp_watch.uid), active=true",
        netuid, ref, label, uid,
    )


async def drop_watch(netuid: int, ref: str) -> None:
    await pool().execute(
        "UPDATE comp_watch SET active=false WHERE netuid=$1 AND ref LIKE $2 || '%'",
        netuid, ref)


# ---------------- repos ----------------
async def repo_row(netuid: int, repo: str, branch: str) -> dict:
    row = await pool().fetchrow(
        "SELECT * FROM comp_repo WHERE netuid=$1 AND repo=$2 AND branch=$3",
        netuid, repo, branch)
    return dict(row) if row else {}


async def save_repo(netuid: int, repo: str, branch: str, **fields) -> None:
    await pool().execute(
        "INSERT INTO comp_repo (netuid, repo, branch, sha, etag, subject, author,"
        "                       committed_at, checked_at)"
        " VALUES ($1,$2,$3,$4,$5,$6,$7,$8,now())"
        " ON CONFLICT (netuid, repo, branch) DO UPDATE SET"
        "   sha=COALESCE(EXCLUDED.sha, comp_repo.sha),"
        "   etag=COALESCE(EXCLUDED.etag, comp_repo.etag),"
        "   subject=COALESCE(EXCLUDED.subject, comp_repo.subject),"
        "   author=COALESCE(EXCLUDED.author, comp_repo.author),"
        "   committed_at=COALESCE(EXCLUDED.committed_at, comp_repo.committed_at),"
        "   checked_at=now()",
        netuid, repo, branch, fields.get("sha"), fields.get("etag"),
        fields.get("subject"), fields.get("author"), fields.get("committed_at"),
    )


async def repos_for(netuid: int) -> list[dict]:
    rows = await pool().fetch(
        "SELECT * FROM comp_repo WHERE netuid=$1 ORDER BY repo", netuid)
    return [dict(r) for r in rows]
