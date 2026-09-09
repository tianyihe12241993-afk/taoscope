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
