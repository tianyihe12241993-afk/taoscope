from fastapi import APIRouter, HTTPException, Query

from ..db import pool
from ..hub import hub
from ..security import RequireUser

router = APIRouter(prefix="/api", tags=["subnets"])

# meta overrides chain identity where the user filled something in
MERGED = """
SELECT s.*,
       COALESCE(s.miner_alpha_per_day, 0) * s.price     AS miner_tao_per_day,
       COALESCE(s.validator_alpha_per_day, 0) * s.price AS validator_tao_per_day,
       GREATEST(COALESCE(s.emitted_alpha_per_day,0)
                - COALESCE(s.participant_alpha_per_day,0), 0) * s.price AS owner_tao_per_day,
       m.dashboard_url, m.notes, m.tags, m.watch, m.our_uids, m.extra,
       COALESCE(NULLIF(m.github_repo,''), s.chain_github)  AS github_repo,
       COALESCE(NULLIF(m.website,''),     s.chain_url)     AS website,
       COALESCE(NULLIF(m.discord,''),     s.chain_discord) AS discord,
       m.docs_url, m.twitter
FROM subnet_live s
LEFT JOIN subnet_meta m USING (netuid)
"""


@router.get("/subnets")
async def list_subnets(user=RequireUser):
    rows = await pool().fetch(MERGED + " ORDER BY s.emission_share DESC NULLS LAST")
    return {
        "block": hub.status.get("block"),
        "tao_usd": hub.tao_usd,
        "tao_usd_change": hub.tao_usd_change,
        "count": len(rows),
        "subnets": [dict(r) for r in rows],
    }


@router.get("/subnets/{netuid}")
async def subnet_detail(netuid: int, user=RequireUser):
    row = await pool().fetchrow(MERGED + " WHERE s.netuid=$1", netuid)
    if row is None:
        raise HTTPException(404, "unknown subnet")
    return dict(row)


@router.get("/subnets/{netuid}/history")
async def subnet_history(
    netuid: int,
    hours: int = Query(24, ge=1, le=24 * 90),
    user=RequireUser,
):
    """Raw 60s series for one subnet."""
    rows = await pool().fetch(
        """
        SELECT ts, price, moving_price, market_cap_tao, tao_in, alpha_in, alpha_out,
               emission_share, active_uids, unique_coldkeys, top_coldkey_pct, tao_usd
        FROM subnet_snapshot
        WHERE netuid=$1 AND ts > now() - ($2 || ' hours')::interval
        ORDER BY ts
        """,
        netuid, str(hours),
    )
    return {"netuid": netuid, "points": [dict(r) for r in rows]}


@router.get("/network")
async def network(user=RequireUser):
    tot = await pool().fetchrow(
        """
        SELECT count(*)                       AS subnets,
               sum(market_cap_tao)            AS mcap_tao,
               sum(tao_in)                    AS tao_locked,
               sum(num_uids)                  AS neurons,
               sum(active_uids)               AS active_neurons
        FROM subnet_live WHERE netuid <> 0
        """
    )
    ck = await pool().fetchval("SELECT count(DISTINCT coldkey) FROM neuron_live")
    return {
        **{k: v for k, v in dict(tot).items()},
        "unique_coldkeys": ck,
        "tao_usd": hub.tao_usd,
        "tao_usd_change": hub.tao_usd_change,
        "status": hub.status,
    }


@router.get("/prelaunch")
async def prelaunch(user=RequireUser):
    """Subnets registered on chain but not yet emitting.

    The owner has to make a start call before rewards flow. Until then a UID can be
    taken cheaply with little competition, and it begins earning the moment the
    subnet switches on.
    """
    rows = await pool().fetch(
        """
        SELECT netuid, name, symbol, chain_description, num_uids, max_uids,
               GREATEST(max_uids - num_uids, 0) AS uids_free,
               burn_tao, registration_allowed, immunity_period, unique_coldkeys,
               network_registered_at, chain_github, chain_url,
               ($1::bigint - network_registered_at) * 12.0 / 86400.0 AS age_days
        FROM subnet_live
        WHERE netuid <> 0 AND COALESCE(is_active, true) = false
        ORDER BY network_registered_at DESC
        """,
        hub.status.get("block") or 0,
    )
    return {"count": len(rows), "subnets": [dict(r) for r in rows]}


@router.get("/events")
async def events(limit: int = Query(40, ge=1, le=200), user=RequireUser):
    """Network events plus anything personal to this user."""
    rows = await pool().fetch(
        "SELECT e.id, e.ts, e.kind, e.netuid, e.severity, e.title, e.body, e.detail,"
        "       s.name AS subnet_name"
        " FROM chain_event e LEFT JOIN subnet_live s USING (netuid)"
        " WHERE e.user_id IS NULL OR e.user_id = $1"
        " ORDER BY e.ts DESC LIMIT $2",
        user["id"], limit,
    )
    return {"events": [dict(r) for r in rows]}


@router.get("/status")
async def status():
    """Unauthenticated liveness probe for the reverse proxy / uptime checks."""
    return {"ok": True, **hub.status, "subnets_cached": len(hub.subnets)}
