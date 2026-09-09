import asyncio
import json
import logging
import pathlib
import re

import asyncpg

from .config import settings

log = logging.getLogger("taoscope.db")

_pool: asyncpg.Pool | None = None
MIGRATIONS = pathlib.Path(__file__).resolve().parent.parent / "migrations"


def _jsonb_encode(value):
    """Accept either a dict/list or an already-serialised string."""
    return value if isinstance(value, str) else json.dumps(value)


async def _init_connection(con: asyncpg.Connection) -> None:
    """Decode jsonb into real Python objects.

    Without this asyncpg hands back raw strings, so a jsonb column reaches the
    browser as a string — spreading a saved filter's criteria then spreads its
    characters instead of its keys, and the filter silently does nothing.
    """
    await con.set_type_codec(
        "jsonb", encoder=_jsonb_encode, decoder=json.loads, schema="pg_catalog",
    )
    await con.set_type_codec(
        "json", encoder=_jsonb_encode, decoder=json.loads, schema="pg_catalog",
    )


async def connect(retries: int = 30) -> asyncpg.Pool:
    """Open the pool, waiting for Postgres to accept connections on cold boot."""
    global _pool
    if _pool is not None:
        return _pool
    last: Exception | None = None
    for attempt in range(retries):
        try:
            _pool = await asyncpg.create_pool(
                settings.database_url, min_size=2, max_size=12, command_timeout=120,
                init=_init_connection,
            )
            log.info("database pool ready")
            return _pool
        except Exception as exc:  # noqa: BLE001
            last = exc
            log.warning("db not ready (%s/%s): %s", attempt + 1, retries, exc)
            await asyncio.sleep(2)
    raise RuntimeError(f"could not reach database: {last}")


def pool() -> asyncpg.Pool:
    if _pool is None:
        raise RuntimeError("database pool not initialised")
    return _pool


async def close() -> None:
    global _pool
    if _pool is not None:
        await _pool.close()
        _pool = None


def _split_statements(sql: str) -> list[str]:
    """Split a migration into standalone statements.

    Continuous aggregates cannot be created inside a transaction block, so each
    statement is executed on its own rather than sending the whole file at once.
    """
    sql = re.sub(r"^\s*--.*$", "", sql, flags=re.MULTILINE)
    return [s.strip() for s in sql.split(";") if s.strip()]


async def migrate() -> None:
    p = pool()
    for path in sorted(MIGRATIONS.glob("*.sql")):
        sql = path.read_text()
        for stmt in _split_statements(sql):
            async with p.acquire() as con:
                try:
                    await con.execute(stmt)
                except asyncpg.DuplicateObjectError:
                    pass
                except asyncpg.exceptions.PostgresError as exc:
                    msg = str(exc).lower()
                    # Re-running policies / already-compressed tables is expected.
                    if "already exists" in msg or "already has" in msg:
                        continue
                    log.error("migration %s failed on: %.120s\n  %s", path.name, stmt, exc)
                    raise
        log.info("migration applied: %s", path.name)
