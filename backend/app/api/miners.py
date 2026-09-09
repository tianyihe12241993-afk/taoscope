from fastapi import APIRouter, HTTPException, Query

from ..db import pool
from ..security import RequireUser
from .roles import ALSO_MINES, IS_VALIDATOR, ROLE

router = APIRouter(prefix="/api", tags=["miners"])

# Neuron emission is alpha per tempo. Blocks/day = 7200, so tempos/day = 7200/tempo.
# Multiplying by the subnet's alpha price gives a TAO/day earn rate.
TAO_PER_DAY = "(n.emission * (7200.0 / NULLIF(s.tempo,0)) * s.price)"
# Income by how a UID actually earns. validator_permit only grants the right to set
# weights — a permitted UID can earn purely as a miner.
MINER_TAO = "(n.incentive * COALESCE(s.miner_alpha_per_day,0) * s.price)"
VALI_TAO = "(n.dividends * COALESCE(s.validator_alpha_per_day,0) * s.price)"

SORTS = {
    "emission": "n.emission DESC",
    "stake": "n.stake DESC",
    "incentive": "n.incentive DESC",
    "dividends": "n.dividends DESC",
    "uid": "n.uid ASC",
    "age": "n.block_at_registration ASC",
}


@router.get("/subnets/{netuid}/miners")
async def subnet_miners(
    netuid: int,
    limit: int = Query(256, ge=1, le=1024),
    sort: str = Query("emission"),
    user=RequireUser,
):
    order = SORTS.get(sort, SORTS["emission"])
    rows = await pool().fetch(
        f"""
        SELECT n.uid, n.hotkey, n.coldkey, n.emission, n.emission_pct, n.incentive,
               n.dividends, n.consensus, n.stake, n.alpha_stake, n.tao_stake,
               n.validator_permit, n.active, n.rank_in_subnet, n.last_update,
               n.block_at_registration,
               {TAO_PER_DAY} AS tao_per_day,
               {ROLE} AS role, {ALSO_MINES} AS also_mines,
               c.label AS coldkey_label,
               COALESCE(c.is_ours, false)
                 OR EXISTS (SELECT 1 FROM my_coldkey mc
                            WHERE mc.coldkey = n.coldkey AND mc.user_id = $3) AS is_ours
        FROM neuron_live n
        JOIN subnet_live s USING (netuid)
        LEFT JOIN coldkey_label c ON c.coldkey = n.coldkey
        WHERE n.netuid=$1
        ORDER BY {order}
        LIMIT $2
        """,
        netuid, limit, user["id"],
    )
    return {"netuid": netuid, "count": len(rows), "miners": [dict(r) for r in rows]}


@router.get("/subnets/{netuid}/coldkeys")
async def subnet_coldkeys(netuid: int, user=RequireUser):
    """Who actually controls this subnet: emission grouped by coldkey, with the
    hotkeys split by role so an operator reads as miner, validator or both."""
    rows = await pool().fetch(
        f"""
        SELECT n.coldkey,
               count(*)                       AS hotkeys,
               count(*) FILTER (WHERE {IS_VALIDATOR})     AS validator_hotkeys,
               count(*) FILTER (WHERE NOT {IS_VALIDATOR}) AS miner_hotkeys,
               CASE WHEN bool_and({IS_VALIDATOR}) THEN 'validator'
                    WHEN bool_or({IS_VALIDATOR})  THEN 'both'
                    ELSE 'miner' END           AS role,
               sum(n.emission)                AS emission,
               sum(n.emission_pct)            AS emission_pct,
               sum(n.stake)                   AS stake,
               sum({TAO_PER_DAY})             AS tao_per_day,
               sum({TAO_PER_DAY}) FILTER (WHERE {IS_VALIDATOR})     AS validator_tao_per_day,
               sum({TAO_PER_DAY}) FILTER (WHERE NOT {IS_VALIDATOR}) AS miner_tao_per_day,
               bool_or({IS_VALIDATOR})        AS has_validator,
               min(n.block_at_registration)   AS first_reg_block,
               array_agg(n.uid ORDER BY n.emission DESC) AS uids,
               array_agg(n.uid ORDER BY n.emission DESC) FILTER (WHERE {IS_VALIDATOR}) AS validator_uids,
               c.label,
               COALESCE(c.is_ours, false)
                 OR EXISTS (SELECT 1 FROM my_coldkey mc
                            WHERE mc.coldkey = n.coldkey AND mc.user_id = $2) AS is_ours
        FROM neuron_live n
        JOIN subnet_live s USING (netuid)
        LEFT JOIN coldkey_label c ON c.coldkey = n.coldkey
        WHERE n.netuid=$1
        GROUP BY n.coldkey, c.label, c.is_ours
        ORDER BY emission DESC
        """,
        netuid, user["id"],
    )
    return {"netuid": netuid, "count": len(rows), "coldkeys": [dict(r) for r in rows]}


@router.get("/coldkeys/top")
async def top_coldkeys(
    limit: int = Query(100, ge=1, le=500),
    role: str = Query("miner", pattern="^(miner|validator|all)$"),
    user=RequireUser,
):
    """Operators ranked by earnings, split by role.

    A coldkey can run both miner and validator hotkeys, so earnings are split
    rather than the coldkey being forced into one bucket. Validators out-earn
    miners heavily, which buries miners in a combined list — hence the default.
    """
    having = {
        "miner": "HAVING count(*) FILTER (WHERE n.incentive > 0) > 0",
        "validator": "HAVING count(*) FILTER (WHERE n.dividends > 0) > 0",
        "all": "",
    }[role]
    order = {
        "miner": "miner_tao_per_day",
        "validator": "validator_tao_per_day",
        "all": "tao_per_day",
    }[role]

    rows = await pool().fetch(
        f"""
        SELECT n.coldkey,
               count(DISTINCT n.netuid)  AS subnets,
               count(*)                  AS hotkeys,
               count(*) FILTER (WHERE n.incentive > 0) AS miner_hotkeys,
               count(*) FILTER (WHERE n.dividends > 0) AS validator_hotkeys,
               sum({TAO_PER_DAY})        AS tao_per_day,
               COALESCE(sum({MINER_TAO}), 0) AS miner_tao_per_day,
               COALESCE(sum({VALI_TAO}), 0)  AS validator_tao_per_day,
               sum(n.stake)              AS stake,
               array_agg(DISTINCT n.netuid) AS netuids,
               c.label, c.is_ours
        FROM neuron_live n
        JOIN subnet_live s USING (netuid)
        LEFT JOIN coldkey_label c ON c.coldkey = n.coldkey
        GROUP BY n.coldkey, c.label, c.is_ours
        {having}
        ORDER BY {order} DESC NULLS LAST
        LIMIT $1
        """,
        limit,
    )
    counts = await pool().fetchrow(
        """
        SELECT count(*) FILTER (WHERE miners > 0)     AS miner_operators,
               count(*) FILTER (WHERE validators > 0) AS validator_operators,
               count(*)                                AS all_operators
        FROM (
          SELECT coldkey,
                 count(*) FILTER (WHERE incentive > 0) AS miners,
                 count(*) FILTER (WHERE dividends > 0) AS validators
          FROM neuron_live GROUP BY coldkey
        ) t
        """
    )
    return {"role": role, "count": len(rows), "totals": dict(counts),
            "coldkeys": [dict(r) for r in rows]}


@router.get("/coldkeys/{coldkey}")
async def coldkey_detail(coldkey: str, user=RequireUser):
    rows = await pool().fetch(
        f"""
        SELECT n.netuid, s.name AS subnet_name, s.symbol, n.uid, n.hotkey,
               n.emission, n.emission_pct, n.stake, n.incentive, n.dividends,
               n.validator_permit, n.active, n.rank_in_subnet,
               n.block_at_registration, {TAO_PER_DAY} AS tao_per_day
        FROM neuron_live n
        JOIN subnet_live s USING (netuid)
        WHERE n.coldkey=$1
        ORDER BY tao_per_day DESC NULLS LAST
        """,
        coldkey,
    )
    if not rows:
        raise HTTPException(404, "coldkey not present in any metagraph")
    label = await pool().fetchrow("SELECT * FROM coldkey_label WHERE coldkey=$1", coldkey)
    positions = [dict(r) for r in rows]
    return {
        "coldkey": coldkey,
        "label": dict(label) if label else None,
        "subnets": len({p["netuid"] for p in positions}),
        "hotkeys": len(positions),
        "tao_per_day": sum(p["tao_per_day"] or 0 for p in positions),
        "positions": positions,
    }


@router.get("/hotkeys/{hotkey}")
async def hotkey_detail(hotkey: str, user=RequireUser):
    rows = await pool().fetch(
        f"""
        SELECT n.netuid, s.name AS subnet_name, n.uid, n.coldkey, n.emission,
               n.emission_pct, n.stake, n.rank_in_subnet, n.validator_permit,
               {TAO_PER_DAY} AS tao_per_day
        FROM neuron_live n JOIN subnet_live s USING (netuid)
        WHERE n.hotkey=$1
        """,
        hotkey,
    )
    if not rows:
        raise HTTPException(404, "hotkey not present in any metagraph")
    return {"hotkey": hotkey, "positions": [dict(r) for r in rows]}


@router.get("/competition")
async def competition(user=RequireUser):
    """How contested each subnet is: operator count, concentration, cost of entry."""
    rows = await pool().fetch(
        """
        SELECT netuid, name, symbol, num_uids, max_uids, active_uids, validator_count,
               unique_coldkeys, top_coldkey, top_coldkey_pct, top_coldkey_hotkeys, hhi,
               burn_tao, registration_allowed, immunity_period, emission_share,
               realized_tao_per_hour, price, market_cap_tao,
               CASE WHEN num_uids > 0
                    THEN unique_coldkeys::float / num_uids ELSE 0 END AS operator_ratio
        FROM subnet_live
        WHERE netuid <> 0
        ORDER BY emission_share DESC NULLS LAST
        """
    )
    return {"count": len(rows), "subnets": [dict(r) for r in rows]}


@router.get("/search")
async def search(q: str = Query(..., min_length=3), user=RequireUser):
    """Resolve a hotkey/coldkey/subnet name fragment."""
    q = q.strip()
    subnets = await pool().fetch(
        "SELECT netuid, name, symbol FROM subnet_live WHERE name ILIKE $1 OR netuid::text = $2 LIMIT 10",
        f"%{q}%", q,
    )
    keys = await pool().fetch(
        "SELECT DISTINCT coldkey AS key, 'coldkey' AS kind FROM neuron_live WHERE coldkey LIKE $1"
        " UNION ALL"
        " SELECT DISTINCT hotkey AS key, 'hotkey' AS kind FROM neuron_live WHERE hotkey LIKE $1"
        " LIMIT 20",
        f"{q}%",
    )
    return {"subnets": [dict(r) for r in subnets], "keys": [dict(r) for r in keys]}
