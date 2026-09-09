"""Read-only data tools Claude may call to answer questions.

Every tool returns compact JSON. They are deliberately narrow and read-only —
there is no free-form SQL, so a bad question cannot reach anything it shouldn't
or mutate state. Row counts are capped to keep token spend predictable.
"""
import contextvars
import json
from typing import Any

from anthropic import beta_async_tool

from ..db import pool
from ..hub import hub

TAO_PER_DAY = "(n.emission * (7200.0 / NULLIF(s.tempo,0)) * s.price)"
# How a UID actually earns. validator_permit only means "may set weights" — a
# permitted UID can earn purely as a miner, so never split on that flag.
MINER_TAO = "(n.incentive * COALESCE(s.miner_alpha_per_day,0) * s.price)"
VALI_TAO = "(n.dividends * COALESCE(s.validator_alpha_per_day,0) * s.price)"

# Set per-request by the agent. Identity never becomes a tool parameter, so the
# model cannot be talked into reading another account's positions.
current_user_id: contextvars.ContextVar[int | None] = contextvars.ContextVar(
    "current_user_id", default=None
)


def _j(value: Any) -> str:
    return json.dumps(value, default=str, separators=(",", ":"))


def _rows(rows) -> list[dict]:
    return [dict(r) for r in rows]


@beta_async_tool
async def network_summary() -> str:
    """Overall Bittensor network state: TAO price, subnet count, total market cap,
    neuron and operator counts, and the current block."""
    row = await pool().fetchrow(
        """
        SELECT count(*) AS subnets, sum(market_cap_tao) AS mcap_tao,
               sum(tao_in) AS tao_in_pools, sum(num_uids) AS neurons
        FROM subnet_live WHERE netuid <> 0
        """
    )
    operators = await pool().fetchval("SELECT count(DISTINCT coldkey) FROM neuron_live")
    return _j({**dict(row), "operators": operators,
               "tao_usd": hub.tao_usd, "block": hub.status.get("block")})


@beta_async_tool
async def find_subnet(query: str) -> str:
    """Look up subnets by name, netuid, or words in their description.

    Args:
        query: A subnet name, a netuid number, or a keyword like "inference" or "compute".
    """
    q = query.strip()
    rows = await pool().fetch(
        """
        SELECT netuid, name, symbol, chain_description, price, market_cap_tao,
               emission_share, num_uids, max_uids, unique_coldkeys, top_coldkey_pct,
               burn_tao, registration_allowed
        FROM subnet_live
        WHERE netuid::text = $1 OR name ILIKE $2 OR chain_description ILIKE $2
        ORDER BY emission_share DESC NULLS LAST LIMIT 12
        """,
        q, f"%{q}%",
    )
    return _j(_rows(rows))


@beta_async_tool
async def get_subnet(netuid: int) -> str:
    """Full detail for one subnet, including which single coldkey dominates it
    (top_coldkey / top_coldkey_pct) and the mining-decision metrics:
    reward per day, number of rival operators, what share of miners actually earn,
    what a median earning miner makes, registration cost and payback days.

    Args:
        netuid: The subnet number, e.g. 64.
    """
    row = await pool().fetchrow(
        f"""
        WITH miner AS (
            SELECT {MINER_TAO} AS tpd
            FROM neuron_live n JOIN subnet_live s USING (netuid)
            WHERE n.netuid = $1
        )
        SELECT s.netuid, s.name, s.symbol, s.chain_description, s.price,
               s.market_cap_tao, s.emission_share,
               s.realized_tao_per_hour * 24 AS total_tao_per_day_to_uids,
               COALESCE(s.miner_alpha_per_day,0) * s.price     AS miner_tao_per_day,
               COALESCE(s.validator_alpha_per_day,0) * s.price AS validator_tao_per_day,
               GREATEST(COALESCE(s.emitted_alpha_per_day,0)
                        - COALESCE(s.participant_alpha_per_day,0),0) * s.price
                                                               AS owner_cut_tao_per_day,
               s.num_uids, s.max_uids, GREATEST(s.max_uids - s.num_uids, 0) AS uids_free,
               s.validator_count, s.unique_coldkeys AS rival_operators,
               s.top_coldkey, s.top_coldkey_pct, s.hhi,
               s.burn_tao AS registration_cost, s.registration_allowed,
               s.immunity_period, s.tempo,
               ($2::bigint - s.network_registered_at) * 12.0 / 86400.0 AS age_days,
               s.chain_github, s.chain_url,
               (SELECT count(*) FROM miner)                          AS miners,
               (SELECT count(*) FROM miner WHERE tpd > 0)            AS miners_earning,
               (SELECT percentile_cont(0.5) WITHIN GROUP (ORDER BY tpd)
                  FROM miner WHERE tpd > 0)                          AS median_earner_tao_per_day,
               (SELECT max(tpd) FROM miner)                          AS best_miner_tao_per_day
        FROM subnet_live s WHERE s.netuid = $1
        """,
        netuid, hub.status.get("block") or 0,
    )
    if row is None:
        return _j({"error": f"no subnet {netuid}"})
    d = dict(row)
    if d.get("miners"):
        d["pct_miners_earning"] = round(100.0 * (d["miners_earning"] or 0) / d["miners"], 1)
    if d.get("median_earner_tao_per_day"):
        d["payback_days"] = round((d["registration_cost"] or 0) / d["median_earner_tao_per_day"], 1)
    return _j(d)


@beta_async_tool
async def screen_subnets(
    min_tao_per_day: float = 0,
    max_rival_operators: int = 100000,
    min_pct_miners_earning: float = 0,
    max_registration_cost: float = 1e9,
    max_age_days: float = 1e9,
    registration_open_only: bool = False,
    min_free_uids: int = 0,
    limit: int = 15,
) -> str:
    """Find subnets matching mining criteria, ranked by MINER reward per rival operator.

    Every subnet emits the same alpha per day; what differs is how it is split. Roughly
    18% is the owner cut and the rest is split between miners and validators — but that
    miner/validator split varies, and on some subnets miners receive nothing at all.
    All reward figures here are the MINER share unless named otherwise.

    Use this for questions like "which subnet should I mine", "where is there least
    competition", "what is cheap to enter", or "which subnets are new".

    Args:
        min_tao_per_day: Minimum TAO paid to MINERS per day (validator income excluded).
        max_rival_operators: Maximum number of distinct competing coldkeys.
        min_pct_miners_earning: Minimum share (0-100) of non-validator UIDs that earn anything.
        max_registration_cost: Maximum registration burn in TAO.
        max_age_days: Maximum subnet age in days (use ~60 for "new subnets").
        registration_open_only: Only subnets currently accepting registrations.
        min_free_uids: Minimum number of empty UID slots.
        limit: How many subnets to return (max 25).
    """
    rows = await pool().fetch(
        f"""
        WITH miner AS (
            SELECT n.netuid, {MINER_TAO} AS tpd
            FROM neuron_live n JOIN subnet_live s USING (netuid)
        ),
        agg AS (
            SELECT netuid, count(*) AS miners,
                   count(*) FILTER (WHERE tpd > 0) AS earning,
                   100.0 * count(*) FILTER (WHERE tpd > 0) / NULLIF(count(*),0) AS pct_earning,
                   percentile_cont(0.5) WITHIN GROUP (ORDER BY tpd)
                     FILTER (WHERE tpd > 0) AS median_earner
            FROM miner GROUP BY netuid
        )
        SELECT s.netuid, s.name,
               COALESCE(s.miner_alpha_per_day,0) * s.price     AS miner_tao_per_day,
               COALESCE(s.validator_alpha_per_day,0) * s.price AS validator_tao_per_day,
               s.realized_tao_per_hour * 24                    AS total_tao_per_day_to_uids,
               s.unique_coldkeys AS rival_operators,
               CASE WHEN s.unique_coldkeys > 0
                    THEN (COALESCE(s.miner_alpha_per_day,0) * s.price) / s.unique_coldkeys
                    END AS miner_reward_per_rival,
               round(a.pct_earning::numeric, 1) AS pct_miners_earning,
               a.median_earner AS median_earner_tao_per_day,
               s.burn_tao AS registration_cost, s.registration_allowed,
               GREATEST(s.max_uids - s.num_uids, 0) AS uids_free,
               round((($9::bigint - s.network_registered_at) * 12.0 / 86400.0)::numeric, 0) AS age_days,
               CASE WHEN COALESCE(a.median_earner,0) > 0
                    THEN round((s.burn_tao / a.median_earner)::numeric, 1) END AS payback_days
        FROM subnet_live s LEFT JOIN agg a USING (netuid)
        WHERE s.netuid <> 0
          AND COALESCE(s.miner_alpha_per_day * s.price, 0) >= $1
          AND COALESCE(s.unique_coldkeys, 0) <= $2
          AND COALESCE(a.pct_earning, 0) >= $3
          AND COALESCE(s.burn_tao, 0) <= $4
          AND (($9::bigint - s.network_registered_at) * 12.0 / 86400.0) <= $5
          AND ($6 = false OR s.registration_allowed)
          AND GREATEST(s.max_uids - s.num_uids, 0) >= $7
            ORDER BY miner_reward_per_rival DESC NULLS LAST
        LIMIT $8
        """,
        min_tao_per_day, max_rival_operators, min_pct_miners_earning,
        max_registration_cost, max_age_days, registration_open_only,
        min_free_uids, min(limit, 25), hub.status.get("block") or 0,
    )
    return _j(_rows(rows))


@beta_async_tool
async def subnet_leaderboard(netuid: int, limit: int = 10) -> str:
    """Who the top miners and validators are INSIDE one subnet, ranked by earnings.

    "role" says how each UID earns (miner = has incentive, validator = has dividends).
    A UID can hold a validator permit and still earn purely as a miner, so use role,
    not the permit flag.

    Use this whenever someone asks who is top / best / winning / #1 / leading on a
    specific subnet, who earns most there, or who the biggest miner on SN<n> is.

    Args:
        netuid: The subnet number, e.g. 100.
        limit: How many UIDs to return (max 25).
    """
    rows = await pool().fetch(
        f"""
        SELECT n.uid, n.rank_in_subnet, n.hotkey, n.coldkey, c.label AS coldkey_label,
               n.emission_pct, n.incentive, n.dividends, n.stake,
               n.validator_permit, {TAO_PER_DAY} AS tao_per_day,
               {MINER_TAO} AS miner_tao_per_day, {VALI_TAO} AS validator_tao_per_day
        FROM neuron_live n
        JOIN subnet_live s USING (netuid)
        LEFT JOIN coldkey_label c ON c.coldkey = n.coldkey
        WHERE n.netuid = $1
        ORDER BY n.emission DESC
        LIMIT $2
        """,
        netuid, min(limit, 25),
    )
    if not rows:
        return _j({"error": f"no neurons found on subnet {netuid}"})
    name = await pool().fetchval("SELECT name FROM subnet_live WHERE netuid=$1", netuid)
    out = _rows(rows)
    for r in out:
        # role reflects how the UID EARNS, not whether it may set weights
        permit = r.pop("validator_permit")
        earns_mining = (r.get("incentive") or 0) > 0
        earns_validating = (r.get("dividends") or 0) > 0
        r["role"] = ("miner+validator" if earns_mining and earns_validating
                     else "miner" if earns_mining
                     else "validator" if earns_validating
                     else "idle")
        r["has_validator_permit"] = permit
    return _j({"netuid": netuid, "subnet": name,
               "note": "ranked by share of subnet emission; role separates miners from validators",
               "leaderboard": out})


@beta_async_tool
async def get_coldkey(ss58: str) -> str:
    """Everything one coldkey runs: which subnets, how many hotkeys, what it earns.

    Args:
        ss58: The coldkey address, e.g. 5GsbTgfvgCH4xd...
    """
    rows = await pool().fetch(
        f"""
        SELECT n.netuid, s.name AS subnet, count(*) AS hotkeys,
               sum({TAO_PER_DAY}) AS tao_per_day,
               bool_or(n.validator_permit) AS has_validator,
               min(n.rank_in_subnet) AS best_rank
        FROM neuron_live n JOIN subnet_live s USING (netuid)
        WHERE n.coldkey = $1
        GROUP BY n.netuid, s.name ORDER BY tao_per_day DESC NULLS LAST LIMIT 40
        """,
        ss58.strip(),
    )
    if not rows:
        return _j({"error": "that coldkey holds no registered UIDs"})
    out = _rows(rows)
    return _j({"coldkey": ss58, "total_tao_per_day": sum(r["tao_per_day"] or 0 for r in out),
               "subnets": len(out), "positions": out})


@beta_async_tool
async def top_operators(role: str = "miner", limit: int = 10) -> str:
    """Highest-earning coldkeys on the network.

    Args:
        role: "miner" for mining income, "validator" for validator income, "all" for combined.
        limit: How many to return (max 25).
    """
    role = role if role in ("miner", "validator", "all") else "miner"
    having = {"miner": "HAVING count(*) FILTER (WHERE NOT n.validator_permit) > 0",
              "validator": "HAVING count(*) FILTER (WHERE n.validator_permit) > 0",
              "all": ""}[role]
    order = {"miner": "miner_tao_per_day", "validator": "validator_tao_per_day",
             "all": "tao_per_day"}[role]
    rows = await pool().fetch(
        f"""
        SELECT n.coldkey, count(DISTINCT n.netuid) AS subnets, count(*) AS hotkeys,
               sum({TAO_PER_DAY}) AS tao_per_day,
               COALESCE(sum({TAO_PER_DAY}) FILTER (WHERE NOT n.validator_permit),0) AS miner_tao_per_day,
               COALESCE(sum({TAO_PER_DAY}) FILTER (WHERE n.validator_permit),0) AS validator_tao_per_day,
               array_agg(DISTINCT n.netuid) AS netuids
        FROM neuron_live n JOIN subnet_live s USING (netuid)
        GROUP BY n.coldkey {having}
        ORDER BY {order} DESC NULLS LAST LIMIT $1
        """,
        min(limit, 25),
    )
    return _j({"role": role, "operators": _rows(rows)})


@beta_async_tool
async def my_positions() -> str:
    """The asking user's own registered coldkeys and every hotkey behind them.

    Use this for questions like "how am I doing", "what are my miners earning",
    or "am I at risk of deregistration".
    """
    user_id = current_user_id.get()
    if user_id is None:
        return _j({"error": "this chat is not linked to a TaoScope account"})
    rows = await pool().fetch(
        f"""
        SELECT n.netuid, s.name AS subnet, n.uid, n.rank_in_subnet, n.incentive,
               n.emission_pct, n.validator_permit, {TAO_PER_DAY} AS tao_per_day,
               n.coldkey
        FROM my_coldkey m
        JOIN neuron_live n ON n.coldkey = m.coldkey
        JOIN subnet_live s ON s.netuid = n.netuid
        WHERE m.user_id = $1
        ORDER BY tao_per_day DESC NULLS LAST LIMIT 60
        """,
        user_id,
    )
    if not rows:
        return _j({"note": "this user has not registered any coldkeys yet"})
    out = _rows(rows)
    return _j({"total_tao_per_day": sum(r["tao_per_day"] or 0 for r in out),
               "hotkeys": len(out), "positions": out})


@beta_async_tool
async def prelaunch_subnets() -> str:
    """Subnets registered on chain but NOT yet emitting — they pay nothing until the
    owner makes the start call.

    Use this for "which subnets are about to launch", "any new subnets coming",
    "where can I register early". Registering before a subnet starts is cheap and
    uncontested, and the UID earns from the moment it switches on.
    """
    rows = await pool().fetch(
        """
        SELECT netuid, name, chain_description, num_uids, max_uids,
               GREATEST(max_uids - num_uids, 0) AS uids_free,
               burn_tao AS registration_cost, registration_allowed, immunity_period,
               unique_coldkeys AS operators_already_in,
               round((($1::bigint - network_registered_at) * 12.0 / 86400.0)::numeric, 1)
                 AS days_since_registered
        FROM subnet_live
        WHERE netuid <> 0 AND COALESCE(is_active, true) = false
        ORDER BY network_registered_at DESC
        """,
        hub.status.get("block") or 0,
    )
    return _j({"note": "registered but not yet emitting; they pay nothing until started",
               "subnets": _rows(rows)})


@beta_async_tool
async def recent_events(limit: int = 10) -> str:
    """Recent network events: new subnets, top-earner changes, registration flips.

    Args:
        limit: How many events (max 25).
    """
    rows = await pool().fetch(
        "SELECT ts, kind, netuid, title, body FROM chain_event"
        " WHERE user_id IS NULL ORDER BY ts DESC LIMIT $1",
        min(limit, 25),
    )
    return _j(_rows(rows))


@beta_async_tool
async def quote_order(netuid: int, tao_amount: float) -> str:
    """What buying alpha would actually cost, including slippage, against the live pool.

    Args:
        netuid: The subnet to buy alpha in.
        tao_amount: How much TAO to spend.
    """
    row = await pool().fetchrow(
        "SELECT tao_in, alpha_in, price FROM subnet_live WHERE netuid=$1", netuid)
    if row is None or not row["tao_in"] or not row["alpha_in"]:
        return _j({"error": "no pool liquidity for that subnet"})
    tao_r, alpha_r = float(row["tao_in"]), float(row["alpha_in"])
    k, spot = tao_r * alpha_r, tao_r / alpha_r
    got = alpha_r - k / (tao_r + tao_amount)
    exec_price = tao_amount / got if got > 0 else 0
    return _j({"netuid": netuid, "tao_spent": tao_amount, "alpha_received": got,
               "spot_price": spot, "exec_price": exec_price,
               "slippage_pct": (exec_price / spot - 1) * 100 if spot else 0})


ALL_TOOLS = [
    network_summary, find_subnet, get_subnet, subnet_leaderboard, screen_subnets,
    prelaunch_subnets, get_coldkey, top_operators, my_positions, recent_events,
    quote_order,
]
