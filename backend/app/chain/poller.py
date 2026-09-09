import asyncio
import datetime as dt
import logging

import httpx

from .. import events
from ..config import settings
from ..db import pool
from ..hub import hub
from ..telegram import bot as telegram
from .client import chain
from .transform import add_emission_share, neurons_from_metagraph, subnet_from_dynamic

log = logging.getLogger("taoscope.poller")

BLOCKS_PER_HOUR = 300  # 12s blocks

# Seeded from the database on first tick so a restart never re-announces
# every existing subnet as "new".
_known_netuids: set[int] = set()

# netuid -> alpha distributed to UIDs per day, refreshed by the neuron sweep and
# multiplied by the live price on every fast tick.
_alpha_per_day: dict[int, float] = {}

# netuid -> whether it was emitting on the previous tick, so the moment a subnet
# switches on can be reported.
_active_state: dict[int, bool] = {}

SUBNET_LIVE_COLS = [
    "netuid", "name", "symbol", "price", "moving_price", "market_cap_tao",
    "tao_in", "alpha_in", "alpha_out", "subnet_volume", "tao_in_emission",
    "alpha_out_emission", "is_active", "emission_share", "realized_tao_per_hour", "tempo",
    "last_step", "blocks_since_last_step", "owner_coldkey", "owner_hotkey",
    "network_registered_at", "chain_github", "chain_url", "chain_discord",
    "chain_logo", "chain_description", "chain_contact", "block",
]

AGG_COLS = [
    "netuid", "participant_alpha_per_day", "miner_alpha_per_day",
    "validator_alpha_per_day", "emitted_alpha_per_day", "num_uids", "max_uids", "active_uids", "validator_count",
    "unique_coldkeys", "earning_coldkeys", "top_coldkey", "top_coldkey_pct",
    "top_coldkey_hotkeys",
    "hhi", "burn_tao", "difficulty", "registration_allowed",
    "pow_registration_allowed", "immunity_period", "max_validators",
    "activity_cutoff", "min_allowed_weights", "max_weights_limit",
    "weights_rate_limit", "weights_version", "commit_reveal_enabled",
    "commit_reveal_period",
]

NEURON_COLS = [
    "netuid", "uid", "hotkey", "coldkey", "emission", "emission_pct", "incentive",
    "dividends", "consensus", "stake", "alpha_stake", "tao_stake",
    "validator_permit", "active", "last_update", "block_at_registration",
    "rank_in_subnet",
]


def _upsert_sql(table: str, cols: list[str], key: str) -> str:
    ph = ", ".join(f"${i + 1}" for i in range(len(cols)))
    sets = ", ".join(f"{c}=EXCLUDED.{c}" for c in cols if c not in key.split(","))
    return (
        f"INSERT INTO {table} ({', '.join(cols)}) VALUES ({ph}) "
        f"ON CONFLICT ({key}) DO UPDATE SET {sets}, updated_at=now()"
    )


async def _fast_tick(last_snapshot: list[float]) -> None:
    """Prices + pools for all subnets. ~0.7s on chain."""
    block = await chain.block()
    subs = await chain.all_subnets()
    rows = [subnet_from_dynamic(s, block) for s in subs]
    add_emission_share(rows)

    global _alpha_per_day
    if not _alpha_per_day:
        seeded = await pool().fetch(
            "SELECT netuid, participant_alpha_per_day FROM subnet_live"
            " WHERE participant_alpha_per_day IS NOT NULL")
        _alpha_per_day = {r["netuid"]: r["participant_alpha_per_day"] for r in seeded}

    for r in rows:
        # What UIDs on this subnet collectively earn per day, in TAO. Uses real
        # emissions when the sweep has run; until then falls back to the alpha the
        # subnet emits per day, less the ~18% owner take.
        alpha_day = _alpha_per_day.get(r["netuid"])
        if alpha_day is None:
            alpha_day = r["alpha_out_emission"] * 7200.0 * 0.82
        r["realized_tao_per_hour"] = alpha_day * r["price"] / 24.0

    p = pool()

    global _known_netuids
    if not _known_netuids:
        seeded = await p.fetch("SELECT netuid FROM subnet_live")
        _known_netuids = {r["netuid"] for r in seeded} or {r["netuid"] for r in rows}

    fresh = [r for r in rows if r["netuid"] not in _known_netuids]

    global _active_state
    if not _active_state:
        seeded_active = await pool().fetch(
            "SELECT netuid, is_active FROM subnet_live WHERE is_active IS NOT NULL")
        _active_state = {r["netuid"]: r["is_active"] for r in seeded_active}
    # a subnet flipping from registered to emitting is the moment mining pays
    started = [r for r in rows
               if r["netuid"] != 0 and r["is_active"]
               and _active_state.get(r["netuid"]) is False]

    sql = _upsert_sql("subnet_live", SUBNET_LIVE_COLS, "netuid")
    await p.executemany(sql, [[r[c] for c in SUBNET_LIVE_COLS] for r in rows])

    if started:
        await p.executemany(
            "UPDATE subnet_live SET started_at=now() WHERE netuid=$1 AND started_at IS NULL",
            [(r["netuid"],) for r in started],
        )
    for r in started:
        ev = await events.emit(
            "subnet_started", f"SN{r['netuid']} {r['name']} started emitting",
            "The owner made the start call — registrations here now earn.",
            r["netuid"], {"name": r["name"]},
        )
        if ev:
            await telegram.notify_event(ev)

    _active_state.update({r["netuid"]: r["is_active"] for r in rows})

    for r in fresh:
        ev = await events.emit(
            "new_subnet", f"New subnet SN{r['netuid']} — {r['name']}",
            (r.get("chain_description") or "Just appeared on the network.")[:200],
            r["netuid"], {"name": r["name"]},
        )
        if ev:
            await telegram.notify_event(ev)
    _known_netuids |= {r["netuid"] for r in rows}

    hub.subnets = rows
    hub.status["block"] = block
    hub.status["last_fast_poll"] = dt.datetime.now(dt.timezone.utc).isoformat()

    now = asyncio.get_event_loop().time()
    if now - last_snapshot[0] >= settings.snapshot_every:
        last_snapshot[0] = now
        ts = dt.datetime.now(dt.timezone.utc)
        await p.copy_records_to_table(
            "subnet_snapshot",
            columns=[
                "ts", "netuid", "block", "price", "moving_price", "tao_in", "alpha_in",
                "alpha_out", "subnet_volume", "market_cap_tao", "tao_in_emission",
                "emission_share", "burn_tao", "tao_usd",
            ],
            records=[
                (
                    ts, r["netuid"], r["block"], r["price"], r["moving_price"], r["tao_in"],
                    r["alpha_in"], r["alpha_out"], r["subnet_volume"], r["market_cap_tao"],
                    r["tao_in_emission"], r["emission_share"], None, hub.tao_usd or None,
                )
                for r in rows
            ],
        )
        await p.execute(
            "INSERT INTO network_snapshot (ts, block, tao_usd, total_subnets, total_neurons,"
            " total_coldkeys, total_stake_tao) VALUES ($1,$2,$3,$4,$5,$6,$7)"
            " ON CONFLICT (ts) DO NOTHING",
            ts, block, hub.tao_usd or None, len(rows),
            hub.network.get("total_neurons"), hub.network.get("total_coldkeys"),
            hub.network.get("total_stake_tao"),
        )

    await hub.broadcast(
        "subnets",
        {"block": block, "tao_usd": hub.tao_usd, "subnets": rows},
    )


async def _neuron_tick() -> None:
    """Full metagraph sweep: every neuron on every subnet. ~6s on chain."""
    started = asyncio.get_event_loop().time()
    # snapshot BEFORE the sweep overwrites live state, so we can diff against it
    prev_subnets = await events.capture_previous()
    prev_my_uids = await events.capture_my_uids()

    mgs = await chain.all_metagraphs()
    ts = dt.datetime.now(dt.timezone.utc)

    aggs: list[dict] = []
    neurons: list[dict] = []
    for m in mgs:
        agg, rows = neurons_from_metagraph(m)
        aggs.append(agg)
        neurons.extend(rows)

    p = pool()
    async with p.acquire() as con:
        await con.executemany(
            "UPDATE subnet_live SET "
            + ", ".join(f"{c}=${i + 2}" for i, c in enumerate(AGG_COLS[1:]))
            + ", updated_at=now() WHERE netuid=$1",
            [[a[c] for c in AGG_COLS] for a in aggs],
        )
        await con.executemany(_upsert_sql("neuron_live", NEURON_COLS, "netuid,uid"),
                              [[r[c] for c in NEURON_COLS] for r in neurons])
        # drop UIDs that no longer exist (subnet shrank / dereg)
        await con.executemany(
            "DELETE FROM neuron_live WHERE netuid=$1 AND uid >= $2",
            [(a["netuid"], a["num_uids"]) for a in aggs],
        )
        await con.copy_records_to_table(
            "neuron_snapshot",
            columns=[
                "ts", "netuid", "uid", "hotkey", "coldkey", "emission", "incentive",
                "dividends", "consensus", "stake", "alpha_stake", "tao_stake",
                "validator_permit", "active", "last_update", "block_at_registration",
            ],
            records=[
                (
                    ts, r["netuid"], r["uid"], r["hotkey"], r["coldkey"], r["emission"],
                    r["incentive"], r["dividends"], r["consensus"], r["stake"],
                    r["alpha_stake"], r["tao_stake"], r["validator_permit"], r["active"],
                    r["last_update"], r["block_at_registration"],
                )
                for r in neurons
            ],
        )

    names = {a["netuid"]: (getattr(m, "name", None) or f"subnet-{a['netuid']}")
             for a, m in zip(aggs, mgs)}
    try:
        fired = await events.detect_subnet_events(prev_subnets, aggs, names)
        fired += await events.detect_my_miner_events(prev_my_uids, names)
        for ev in fired:
            await telegram.notify_event(ev)
    except Exception:  # noqa: BLE001 — notifications must never break collection
        log.exception("event detection failed")

    _alpha_per_day.update(
        {a["netuid"]: a["participant_alpha_per_day"] for a in aggs
         if a.get("participant_alpha_per_day")}
    )

    hub.network = {
        "total_neurons": len(neurons),
        "total_coldkeys": len({r["coldkey"] for r in neurons if r["coldkey"]}),
        "total_stake_tao": sum(r["tao_stake"] for r in neurons),
    }
    elapsed = asyncio.get_event_loop().time() - started
    hub.status["last_neuron_poll"] = ts.isoformat()
    hub.status["neuron_poll_seconds"] = round(elapsed, 2)
    log.info("neuron sweep: %d subnets, %d neurons in %.1fs", len(aggs), len(neurons), elapsed)
    await hub.broadcast("neurons", {"ts": ts.isoformat(), **hub.network})


async def _price_tick() -> None:
    async with httpx.AsyncClient(timeout=15) as cx:
        r = await cx.get(settings.tao_price_url)
        r.raise_for_status()
        data = r.json().get("bittensor", {})
        hub.tao_usd = float(data.get("usd") or 0.0)
        hub.tao_usd_change = float(data.get("usd_24h_change") or 0.0)


async def _loop(name: str, fn, interval: int, *args) -> None:
    """Run fn forever; a failure never kills the loop."""
    while True:
        start = asyncio.get_event_loop().time()
        try:
            await fn(*args)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            hub.status["errors"] += 1
            hub.status["last_error"] = f"{name}: {exc}"
            log.exception("%s loop failed", name)
        delay = max(1.0, interval - (asyncio.get_event_loop().time() - start))
        await asyncio.sleep(delay)


def start() -> list[asyncio.Task]:
    last_snapshot = [0.0]
    return [
        asyncio.create_task(_loop("price", _price_tick, settings.price_poll)),
        asyncio.create_task(_loop("fast", _fast_tick, settings.fast_poll, last_snapshot)),
        asyncio.create_task(_loop("neuron", _neuron_tick, settings.neuron_poll)),
    ]
