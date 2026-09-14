"""Requirement 2: the subnet screener.

Raw chain fields don't answer "is this worth mining", so two derived metrics do:

  reward_per_operator  — subnet TAO/day divided by the number of distinct
                         coldkeys competing for it. High = a big prize with few
                         rivals, which is the thing you're actually hunting.
  pct_miners_earning   — the headline number. On most subnets only 2-25% of
                         miner UIDs earn ANYTHING; on SN3 and SN4 it is 5 of
                         ~250. A high emission share means nothing if you land
                         in the 95% that earns zero. Validators (permit holders
                         receiving dividends) are excluded from every miner
                         statistic here, including the top miner.
  payback_days         — registration burn divided by what a median EARNING
                         miner makes per day. How long a UID takes to repay
                         itself assuming you reach the earning cohort.
"""
from fastapi import APIRouter, Request
from pydantic import BaseModel

from ..db import pool
from ..hub import hub
from ..security import CsrfGuard, RequireUser
from ..comp.windows import all_windows
from .roles import IS_VALIDATOR

router = APIRouter(prefix="/api", tags=["screen"])

TAO_PER_DAY = "(n.emission * (7200.0 / NULLIF(s.tempo,0)) * s.price)"

SCREEN_SQL = f"""
WITH miner AS (
    -- Miner-role UIDs only (see roles.py: a validator is a UID receiving
    -- dividends). `tpd` is the UID's share of the miner pool, the stable figure
    -- the cohort statistics are built on; `actual` is the hotkey's own emission
    -- valued at today's price, which is what the top-miner column shows.
    SELECT n.netuid, n.uid, n.hotkey,
           n.incentive * COALESCE(s.miner_alpha_per_day,0) * s.price AS tpd,
           {TAO_PER_DAY} AS actual
    FROM neuron_live n JOIN subnet_live s USING (netuid)
    WHERE NOT {IS_VALIDATOR}
),
top AS (
    -- best miner hotkey by its own emission; validator emission never enters
    SELECT DISTINCT ON (netuid) netuid, uid AS top_miner_uid, hotkey AS top_miner_hotkey,
           actual AS best_miner_tao_per_day
    FROM miner ORDER BY netuid, actual DESC, uid
),
roles AS (
    SELECT n.netuid,
           count(*) FILTER (WHERE {IS_VALIDATOR})     AS validator_uids,
           count(*) FILTER (WHERE NOT {IS_VALIDATOR}) AS miner_uids
    FROM neuron_live n GROUP BY n.netuid
),
agg AS (
    SELECT netuid,
           count(*)                                   AS miners,
           count(*) FILTER (WHERE tpd > 0)            AS earning_miners,
           100.0 * count(*) FILTER (WHERE tpd > 0)
                 / NULLIF(count(*),0)                 AS pct_miners_earning,
           percentile_cont(0.5) WITHIN GROUP (ORDER BY tpd)
                 FILTER (WHERE tpd > 0)               AS median_earner_tao_per_day
    FROM miner GROUP BY netuid
)
SELECT s.netuid, s.name, s.symbol, s.price, s.market_cap_tao, s.emission_share,
       s.realized_tao_per_hour * 24                     AS tao_per_day,
       COALESCE(s.miner_alpha_per_day, 0) * s.price     AS miner_tao_per_day,
       COALESCE(s.validator_alpha_per_day, 0) * s.price AS validator_tao_per_day,
       GREATEST(COALESCE(s.emitted_alpha_per_day,0)
                - COALESCE(s.participant_alpha_per_day,0), 0) * s.price AS owner_tao_per_day,
       s.num_uids, s.max_uids, s.active_uids, s.validator_count,
       GREATEST(s.max_uids - s.num_uids, 0)    AS uids_free,
       s.unique_coldkeys, s.top_coldkey, s.top_coldkey_pct, s.hhi,
       s.burn_tao, s.registration_allowed, s.immunity_period, s.tempo,
       s.miner_burned, s.owner_incentive_share,
       s.network_registered_at, COALESCE(s.is_active, true) AS is_active, s.started_at,
       ($1::bigint - s.network_registered_at) * 12.0 / 86400.0 AS age_days,
       a.miners, a.earning_miners, a.pct_miners_earning,
       a.median_earner_tao_per_day,
       t.best_miner_tao_per_day, t.top_miner_uid, t.top_miner_hotkey,
       ro.validator_uids, ro.miner_uids,
       CASE WHEN s.unique_coldkeys > 0
            THEN (COALESCE(s.miner_alpha_per_day,0) * s.price) / s.unique_coldkeys
            END AS reward_per_operator,
       -- how long registration takes to repay IF you reach the earning cohort
       CASE WHEN COALESCE(a.median_earner_tao_per_day,0) > 0
            THEN s.burn_tao / a.median_earner_tao_per_day END AS payback_days,
       m.watch, m.tags, m.notes IS NOT NULL AND m.notes <> '' AS has_notes,
       COALESCE(NULLIF(m.github_repo,''), s.chain_github) AS github_repo,
       COALESCE(NULLIF(m.website,''), s.chain_url)        AS website,
       m.dashboard_url,
       s.chain_description, s.chain_logo,
       EXISTS (SELECT 1 FROM neuron_live n2 JOIN my_coldkey mc ON mc.coldkey = n2.coldkey
               WHERE n2.netuid = s.netuid AND mc.user_id = $2) AS i_am_in
FROM subnet_live s
LEFT JOIN agg a USING (netuid)
LEFT JOIN top t USING (netuid)
LEFT JOIN roles ro USING (netuid)
LEFT JOIN subnet_meta m USING (netuid)
WHERE s.netuid <> 0
ORDER BY s.emission_share DESC NULLS LAST
"""


@router.get("/screen")
async def screen(user=RequireUser):
    """All subnets with the derived decision metrics. Filtering happens client-side
    so the sliders respond instantly across 128 rows."""
    head = hub.status.get("block") or 0
    rows = await pool().fetch(SCREEN_SQL, head, user["id"])
    try:
        windows = await all_windows()
    except Exception:  # noqa: BLE001 — a tracker hiccup must not take the screener down
        windows = {}
    subnets = []
    for r in rows:
        d = dict(r)
        d["submission"] = windows.get(d["netuid"])
        subnets.append(d)
    return {"block": head, "count": len(rows), "subnets": subnets}


# ---------------- saved filters ----------------
class FilterIn(BaseModel):
    name: str
    criteria: dict


@router.get("/filters")
async def list_filters(user=RequireUser):
    rows = await pool().fetch(
        "SELECT id, name, criteria, created_at FROM saved_filter WHERE user_id=$1 ORDER BY name",
        user["id"],
    )
    return {"filters": [dict(r) for r in rows]}


@router.post("/filters", dependencies=[CsrfGuard])
async def save_filter(body: FilterIn, user=RequireUser):
    import json
    await pool().execute(
        "INSERT INTO saved_filter (user_id, name, criteria) VALUES ($1,$2,$3::jsonb)"
        " ON CONFLICT (user_id, name) DO UPDATE SET criteria=EXCLUDED.criteria",
        user["id"], body.name, json.dumps(body.criteria),
    )
    return {"ok": True}


@router.delete("/filters/{fid}", dependencies=[CsrfGuard])
async def delete_filter(fid: int, user=RequireUser):
    await pool().execute("DELETE FROM saved_filter WHERE id=$1 AND user_id=$2", fid, user["id"])
    return {"ok": True}
