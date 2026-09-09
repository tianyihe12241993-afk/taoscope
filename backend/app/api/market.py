"""Price, pool depth and AMM math.

The data and pricing needed for alpha trading live here now; only the signing /
execution layer is deferred. Quotes use the same constant-product curve the
chain uses, so a quote here matches what a real order would cost.
"""
from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel

from ..db import pool
from ..hub import hub
from ..security import CsrfGuard, RequireUser

router = APIRouter(prefix="/api/market", tags=["market"])


@router.get("/candles/{netuid}")
async def candles(
    netuid: int,
    interval: str = Query("1m", pattern="^(1m|1h)$"),
    hours: int = Query(24, ge=1, le=24 * 90),
    user=RequireUser,
):
    view = "alpha_candle_1m" if interval == "1m" else "alpha_candle_1h"
    rows = await pool().fetch(
        f"""
        SELECT bucket AS ts, open, high, low, close, tao_in, alpha_in,
               market_cap_tao, tao_usd
        FROM {view}
        WHERE netuid=$1 AND bucket > now() - ($2 || ' hours')::interval
        ORDER BY bucket
        """,
        netuid, str(hours),
    )
    return {"netuid": netuid, "interval": interval, "candles": [dict(r) for r in rows]}


@router.get("/movers")
async def movers(window: str = Query("1h"), limit: int = Query(10, ge=1, le=50), user=RequireUser):
    """Biggest alpha price moves over a window, from stored history."""
    rows = await pool().fetch(
        """
        WITH bounds AS (
          SELECT netuid,
                 first(price, ts) AS then_price,
                 last(price, ts)  AS now_price
          FROM subnet_snapshot
          WHERE ts > now() - ($1 || ' hours')::interval
          GROUP BY netuid
        )
        SELECT b.netuid, s.name, s.symbol, b.then_price, b.now_price,
               CASE WHEN b.then_price > 0
                    THEN (b.now_price / b.then_price - 1) * 100 ELSE NULL END AS pct_change,
               s.market_cap_tao, s.emission_share
        FROM bounds b JOIN subnet_live s USING (netuid)
        WHERE b.then_price > 0 AND b.netuid <> 0
        ORDER BY abs(COALESCE((b.now_price / NULLIF(b.then_price,0) - 1), 0)) DESC
        LIMIT $2
        """,
        {"1h": "1", "24h": "24", "7d": "168"}.get(window, "1"), limit,
    )
    return {"window": window, "movers": [dict(r) for r in rows]}


class Quote(BaseModel):
    netuid: int
    side: str
    tao_in: float
    alpha_in: float
    spot_price: float
    exec_price: float
    alpha_delta: float
    tao_delta: float
    slippage_pct: float


@router.get("/quote/{netuid}", response_model=Quote)
async def quote(
    netuid: int,
    amount: float = Query(..., gt=0, description="TAO to spend (buy) or alpha to sell"),
    side: str = Query("buy", pattern="^(buy|sell)$"),
    user=RequireUser,
):
    """Constant-product quote against the live pool reserves."""
    row = await pool().fetchrow(
        "SELECT tao_in, alpha_in, price FROM subnet_live WHERE netuid=$1", netuid
    )
    if row is None:
        raise HTTPException(404, "unknown subnet")
    tao_r, alpha_r = float(row["tao_in"] or 0), float(row["alpha_in"] or 0)
    if tao_r <= 0 or alpha_r <= 0:
        raise HTTPException(400, "subnet pool has no liquidity")
    k = tao_r * alpha_r
    spot = tao_r / alpha_r

    if side == "buy":
        new_tao = tao_r + amount
        alpha_delta = alpha_r - k / new_tao          # alpha received
        tao_delta = -amount
        exec_price = amount / alpha_delta if alpha_delta > 0 else 0.0
    else:
        new_alpha = alpha_r + amount
        tao_delta = tao_r - k / new_alpha            # TAO received
        alpha_delta = -amount
        exec_price = tao_delta / amount if amount > 0 else 0.0

    slip = (exec_price / spot - 1) * 100 if spot > 0 else 0.0
    return Quote(
        netuid=netuid, side=side, tao_in=tao_r, alpha_in=alpha_r, spot_price=spot,
        exec_price=exec_price, alpha_delta=alpha_delta, tao_delta=tao_delta,
        slippage_pct=slip,
    )


@router.get("/depth/{netuid}")
async def depth(netuid: int, user=RequireUser):
    """Slippage curve — what various order sizes would actually cost."""
    row = await pool().fetchrow("SELECT tao_in, alpha_in FROM subnet_live WHERE netuid=$1", netuid)
    if row is None:
        raise HTTPException(404, "unknown subnet")
    tao_r, alpha_r = float(row["tao_in"] or 0), float(row["alpha_in"] or 0)
    if tao_r <= 0 or alpha_r <= 0:
        raise HTTPException(400, "subnet pool has no liquidity")
    k, spot = tao_r * alpha_r, tao_r / alpha_r
    out = []
    for amt in (1, 5, 10, 25, 50, 100, 250, 500, 1000):
        got = alpha_r - k / (tao_r + amt)
        ep = amt / got if got > 0 else 0
        out.append({
            "tao": amt, "alpha_out": got, "exec_price": ep,
            "slippage_pct": (ep / spot - 1) * 100 if spot else 0,
        })
    return {"netuid": netuid, "spot_price": spot, "tao_in": tao_r, "alpha_in": alpha_r, "ladder": out}


# ---- watchlist / alerts (foundation for trading automation) ----
class WatchIn(BaseModel):
    netuid: int
    note: str | None = None


@router.get("/watchlist")
async def get_watchlist(user=RequireUser):
    rows = await pool().fetch(
        "SELECT w.*, s.name, s.symbol, s.price, s.emission_share"
        " FROM watchlist w LEFT JOIN subnet_live s USING (netuid) ORDER BY w.created_at"
    )
    return {"watchlist": [dict(r) for r in rows]}


@router.post("/watchlist", dependencies=[CsrfGuard])
async def add_watch(body: WatchIn, user=RequireUser):
    await pool().execute("INSERT INTO watchlist (netuid, note) VALUES ($1,$2)", body.netuid, body.note)
    return {"ok": True}


@router.delete("/watchlist/{item_id}", dependencies=[CsrfGuard])
async def del_watch(item_id: int, user=RequireUser):
    await pool().execute("DELETE FROM watchlist WHERE id=$1", item_id)
    return {"ok": True}
