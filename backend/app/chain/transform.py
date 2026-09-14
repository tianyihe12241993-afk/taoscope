"""Convert bittensor SDK objects into plain rows.

Balance values render as strings with currency symbols, so everything is pushed
through to_f(). moving_price is a float on DynamicInfo but a Balance on
MetagraphInfo, which is exactly the kind of mismatch to_f() absorbs.
"""
from collections import defaultdict


def to_f(x, default: float = 0.0) -> float:
    if x is None:
        return default
    try:
        return float(x)
    except (TypeError, ValueError):
        return default


def to_i(x, default: int = 0) -> int:
    try:
        return int(x)
    except (TypeError, ValueError):
        return default


INT64_MAX = 2**63 - 1


def to_i64(x) -> int | None:
    """Hyperparameter ints, where the chain uses u64 max as 'unbounded/disabled'.

    That sentinel exceeds even bigint, so it is stored as NULL instead of being
    clamped to a number that would read as a real limit.
    """
    try:
        v = int(x)
    except (TypeError, ValueError):
        return None
    return None if v > INT64_MAX or v < -(2**63) else v


def _ident(obj, field: str) -> str | None:
    if obj is None:
        return None
    val = getattr(obj, field, None)
    if isinstance(val, str) and val.strip():
        return val.strip()
    return None


def subnet_from_dynamic(s, block: int) -> dict:
    """Row from all_subnets() -> pool/price state, cheap enough to poll per block."""
    price = to_f(s.price)
    alpha_out = to_f(s.alpha_out)
    ident = getattr(s, "subnet_identity", None)
    return {
        "netuid": to_i(s.netuid),
        "name": getattr(s, "subnet_name", None) or f"subnet-{s.netuid}",
        "symbol": getattr(s, "symbol", None),
        "price": price,
        "moving_price": to_f(s.moving_price),
        "market_cap_tao": price * alpha_out,
        "tao_in": to_f(s.tao_in),
        "alpha_in": to_f(s.alpha_in),
        "alpha_out": alpha_out,
        "subnet_volume": to_f(s.subnet_volume),
        "tao_in_emission": to_f(s.tao_in_emission),
        "alpha_out_emission": to_f(s.alpha_out_emission),
        # registered subnets only emit once the owner makes the start call
        "is_active": to_f(s.alpha_out_emission) > 0,
        "tempo": to_i64(s.tempo),
        "last_step": to_i(s.last_step),
        "blocks_since_last_step": to_i64(s.blocks_since_last_step),
        "owner_coldkey": getattr(s, "owner_coldkey", None),
        "owner_hotkey": getattr(s, "owner_hotkey", None),
        "network_registered_at": to_i(s.network_registered_at),
        "chain_github": _ident(ident, "github_repo"),
        "chain_url": _ident(ident, "subnet_url"),
        "chain_discord": _ident(ident, "discord"),
        "chain_logo": _ident(ident, "logo_url"),
        "chain_description": _ident(ident, "description"),
        "chain_contact": _ident(ident, "subnet_contact"),
        "block": block,
    }


def add_emission_share(rows: list[dict]) -> None:
    """Share of network emission, by moving price (the stable measure).

    Instantaneous tao_in_emission swings with where each subnet sits in its tempo,
    so it is stored but not used for ranking.
    """
    total = sum(r["moving_price"] for r in rows if r["netuid"] != 0) or 1.0
    for r in rows:
        r["emission_share"] = 0.0 if r["netuid"] == 0 else r["moving_price"] / total


def neurons_from_metagraph(m) -> tuple[dict, list[dict]]:
    """Row set from one MetagraphInfo: subnet-level aggregates + per-neuron rows."""
    hotkeys = list(getattr(m, "hotkeys", []) or [])
    coldkeys = list(getattr(m, "coldkeys", []) or [])
    n = len(hotkeys)

    def col(name, cast=to_f):
        vals = getattr(m, name, None) or []
        out = [cast(v) for v in vals]
        return out + [cast(None)] * (n - len(out))

    emission = col("emission")
    incentive = col("incentives")
    dividends = col("dividends")
    consensus = col("consensus")
    stake = col("total_stake")
    alpha_stake = col("alpha_stake")
    tao_stake = col("tao_stake")
    last_update = col("last_update", to_i64)
    reg_block = col("block_at_registration", to_i64)

    raw_active = list(getattr(m, "active", []) or [])
    raw_perm = list(getattr(m, "validator_permit", []) or [])
    active = [bool(raw_active[i]) if i < len(raw_active) else False for i in range(n)]
    permit = [bool(raw_perm[i]) if i < len(raw_perm) else False for i in range(n)]

    total_em = sum(emission) or 1.0
    order = sorted(range(n), key=lambda i: -emission[i])
    rank = {uid: pos + 1 for pos, uid in enumerate(order)}

    neurons = [
        {
            "netuid": to_i(m.netuid),
            "uid": i,
            "hotkey": hotkeys[i],
            "coldkey": coldkeys[i] if i < len(coldkeys) else None,
            "emission": emission[i],
            "emission_pct": emission[i] / total_em * 100.0,
            "incentive": incentive[i],
            "dividends": dividends[i],
            "consensus": consensus[i],
            "stake": stake[i],
            "alpha_stake": alpha_stake[i],
            "tao_stake": tao_stake[i],
            "validator_permit": permit[i],
            "active": active[i],
            "last_update": last_update[i],
            "block_at_registration": reg_block[i],
            "rank_in_subnet": rank[i],
        }
        for i in range(n)
    ]

    # --- coldkey concentration (requirement 2) ---
    by_ck: dict[str, float] = defaultdict(float)
    hk_count: dict[str, int] = defaultdict(int)
    for i in range(n):
        ck = coldkeys[i] if i < len(coldkeys) else None
        if ck is None:
            continue
        by_ck[ck] += emission[i]
        hk_count[ck] += 1

    # Holding a UID is not competing. Count on INCENTIVE, not emission: emission
    # includes validator dividends, so an emission-based count answers "who
    # receives alpha" when the question is "who is winning the mining
    # competition". On SN91 that is the difference between 10 and 3.
    inc_by_ck: dict[str, float] = defaultdict(float)
    for i in range(n):
        ck = coldkeys[i] if i < len(coldkeys) else None
        if ck is not None:
            inc_by_ck[ck] += incentive[i]
    earning_ck = sum(1 for v in inc_by_ck.values() if v > 0)

    # Incentive held by the owner's own keys: every UID whose coldkey is the
    # owner coldkey, plus SubnetOwnerHotkey -- the set the chain's
    # get_owner_hotkeys() builds, whose incentive is recycled or burned rather
    # than paid. This is a reconstruction; the chain records its own number as
    # MinerBurned, fetched separately. Kept side by side so a disagreement is
    # visible instead of one of them being silently believed.
    owner_ck = getattr(m, "owner_coldkey", None)
    owner_hk = getattr(m, "owner_hotkey", None)
    total_inc = sum(incentive)
    owner_inc = sum(
        incentive[i] for i in range(n)
        if (owner_ck and i < len(coldkeys) and coldkeys[i] == owner_ck)
        or (owner_hk and hotkeys[i] == owner_hk)
    )
    owner_incentive_share = (owner_inc / total_inc) if total_inc > 0 else None

    top_ck, top_em = (max(by_ck.items(), key=lambda kv: kv[1]) if by_ck else (None, 0.0))
    hhi = sum((v / total_em) ** 2 for v in by_ck.values()) if by_ck else 0.0

    # Alpha actually distributed to UIDs per day. Neuron `emission` is per tempo,
    # and there are 7200 blocks/day, so tempo count per day = 7200 / tempo.
    tempo = to_i(getattr(m, "tempo", 0)) or 0
    per_day = (7200.0 / tempo) if tempo else 0.0
    total_alpha_per_day = sum(emission) * per_day
    # The chain splits the participant pool 50/50 between miner incentive and
    # validator dividends — verified against every subnet by solving
    # emission = incentive*M + dividends*V. Do NOT split on validator_permit: that
    # flag only means "may set weights". A UID can hold a permit and still earn
    # purely as a miner (SN98 uid 236 has incentive 1.0 and dividends 0), which the
    # permit-based split misfiled as validator income.
    miner_alpha_per_day = total_alpha_per_day * 0.5
    validator_alpha_per_day = total_alpha_per_day * 0.5
    # what the subnet emits in total; the gap to total_alpha_per_day is the owner cut
    emitted_alpha_per_day = to_f(getattr(m, "alpha_out_emission", 0.0)) * 7200.0

    agg = {
        "netuid": to_i(m.netuid),
        "participant_alpha_per_day": total_alpha_per_day,
        "miner_alpha_per_day": miner_alpha_per_day,
        "validator_alpha_per_day": validator_alpha_per_day,
        "emitted_alpha_per_day": emitted_alpha_per_day,
        "num_uids": to_i(getattr(m, "num_uids", n)),
        "max_uids": to_i64(getattr(m, "max_uids", n)),
        "active_uids": sum(active),
        "validator_count": sum(permit),
        "unique_coldkeys": len(by_ck),
        "earning_coldkeys": earning_ck,
        "top_coldkey": top_ck,
        "top_coldkey_pct": (top_em / total_em * 100.0) if by_ck else 0.0,
        "top_coldkey_hotkeys": hk_count.get(top_ck, 0),
        "hhi": hhi,
        "owner_incentive_share": owner_incentive_share,
        "burn_tao": to_f(getattr(m, "burn", None)),
        "difficulty": to_f(getattr(m, "difficulty", None)),
        "registration_allowed": bool(getattr(m, "registration_allowed", False)),
        "pow_registration_allowed": bool(getattr(m, "pow_registration_allowed", False)),
        "immunity_period": to_i64(getattr(m, "immunity_period", None)),
        "max_validators": to_i64(getattr(m, "max_validators", None)),
        "activity_cutoff": to_i64(getattr(m, "activity_cutoff", None)),
        "min_allowed_weights": to_i64(getattr(m, "min_allowed_weights", None)),
        "max_weights_limit": to_f(getattr(m, "max_weights_limit", None)),
        "weights_rate_limit": to_i64(getattr(m, "weights_rate_limit", None)),
        "weights_version": to_i64(getattr(m, "weights_version", None)),
        "commit_reveal_enabled": bool(getattr(m, "commit_reveal_weights_enabled", False)),
        "commit_reveal_period": to_i64(getattr(m, "commit_reveal_period", None)),
    }
    return agg, neurons
