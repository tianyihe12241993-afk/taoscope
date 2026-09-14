"""Backfill subnet_burn from an archive node: SubtensorModule::MinerBurned at past blocks.

The neuron sweep only records MinerBurned from the moment it was deployed, so the
Burn trend sparkline would start empty. The storage itself is on chain for
~45+ days (it did not exist 90 days back), so the history can be read directly --
the same number the Burn column shows, not a reconstruction.

Run against the live stack (idempotent; re-running skips rows already stored):

    docker compose run --rm -v "$PWD/backend/devtools:/srv/devtools:ro" backend \\
        python /srv/devtools/backfill_burn.py --days 31 --step-hours 6

Endpoint: archive.sub.latent.to, not archive.chain.opentensor.ai -- the opentensor
archive rejects deep map queries at ~8 per 10 s ("Historical work rate limit
exceeded"); latent took 40 in a row without refusal. Each historical
query_map over all subnets costs ~10 s there, so this runs a few connections in
parallel and backs off (doubling) whenever a call fails.
"""
import argparse
import asyncio
import datetime as dt
import logging
import os
import threading
import time

import asyncpg
import bittensor as bt

log = logging.getLogger("backfill_burn")
BLOCK_SECONDS = 12
_local = threading.local()


def _st(endpoint: str) -> bt.Subtensor:
    if getattr(_local, "st", None) is None:
        _local.st = bt.Subtensor(network=endpoint)
    return _local.st


def _drop() -> None:
    st, _local.st = getattr(_local, "st", None), None
    try:
        if st is not None:
            st.close()
    except Exception:  # noqa: BLE001
        pass


def fetch_at(endpoint: str, block: int) -> tuple[int, dt.datetime, dict[int, float]] | None:
    """(block, chain timestamp, {netuid: burned 0..1}); None if the storage did not exist yet."""
    delay = 1.0
    for attempt in range(6):
        try:
            sub = _st(endpoint).substrate
            h = sub.get_block_hash(block)
            now_ms = sub.query("Timestamp", "Now", block_hash=h)
            now_ms = int(now_ms.value if hasattr(now_ms, "value") else now_ms)
            out: dict[int, float] = {}
            for k, v in sub.query_map("SubtensorModule", "MinerBurned", block_hash=h):
                key = k.value if hasattr(k, "value") else k
                val = v.value if hasattr(v, "value") else v
                bits = val.get("bits") if isinstance(val, dict) else val
                out[int(key)] = int(bits) / 2**32
            ts = dt.datetime.fromtimestamp(now_ms / 1000, tz=dt.timezone.utc)
            return block, ts, out
        except Exception as exc:  # noqa: BLE001
            if "StorageFunctionNotFound" in type(exc).__name__ or "not found" in str(exc).lower():
                return None
            log.warning("block %s attempt %d failed: %s -- backing off %.0fs", block, attempt + 1, exc, delay)
            _drop()
            time.sleep(delay)
            delay = min(delay * 2, 60)
    raise RuntimeError(f"block {block}: gave up after retries")


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=float, default=31)
    ap.add_argument("--step-hours", type=float, default=6)
    ap.add_argument("--workers", type=int, default=3)
    ap.add_argument("--endpoint", default=os.getenv("BURN_ARCHIVE", "wss://archive.sub.latent.to:443"))
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    head = bt.Subtensor(network=args.endpoint)
    head_block = head.block
    head.close()
    step = int(args.step_hours * 3600 / BLOCK_SECONDS)
    blocks = [head_block - i * step for i in range(1, int(args.days * 24 / args.step_hours) + 1)]
    log.info("head %d; %d target blocks every %d blocks (~%.0fh) over %.0f days",
             head_block, len(blocks), step, args.step_hours, args.days)

    db = await asyncpg.connect(os.environ["TAOSCOPE_DATABASE_URL"])
    sem = asyncio.Semaphore(args.workers)
    db_lock = asyncio.Lock()  # one asyncpg connection = one statement at a time
    done = stored = missing = 0
    started = time.time()

    async def one(block: int) -> None:
        nonlocal done, stored, missing
        async with sem:
            res = await asyncio.to_thread(fetch_at, args.endpoint, block)
        done += 1
        if res is None:
            missing += 1
            log.info("[%d/%d] block %d: MinerBurned not on chain yet", done, len(blocks), block)
            return
        blk, ts, burned = res
        async with db_lock:
            await _store(db, blk, ts, burned)
        stored += len(burned)
        rate = done / max(time.time() - started, 1)
        log.info("[%d/%d] block %d @ %s: %d subnets (%.2f blocks/s, ~%.0fs left)",
                 done, len(blocks), blk, ts.isoformat(timespec="minutes"), len(burned),
                 rate, (len(blocks) - done) / max(rate, 1e-9))

    await asyncio.gather(*(one(b) for b in blocks))
    await db.close()
    log.info("done: %d blocks, %d rows offered, %d blocks before MinerBurned existed, %.0fs",
             len(blocks), stored, missing, time.time() - started)


async def _store(db, blk, ts, burned) -> None:
    """Emitting flag from our own 60s snapshots nearest this moment; older than
    the snapshots -> NULL (unknown), which the reader treats as emitting."""
    emit = {r["netuid"]: r["emitting"] for r in await db.fetch(
        """SELECT DISTINCT ON (netuid) netuid, emission_share > 0 AS emitting
           FROM subnet_snapshot
           WHERE ts BETWEEN $1::timestamptz - interval '6 hours'
                        AND $1::timestamptz + interval '6 hours'
           ORDER BY netuid, abs(extract(epoch FROM ts - $1::timestamptz))""", ts)}
    await db.executemany(
        "INSERT INTO subnet_burn (ts, netuid, block, miner_burned, emitting, source)"
        " VALUES ($1,$2,$3,$4,$5,'archive') ON CONFLICT DO NOTHING",
        [(ts, n, blk, v, emit.get(n)) for n, v in burned.items()],
    )




if __name__ == "__main__":
    asyncio.run(main())
