"""Requirement 5: register coldkeys, see every hotkey behind them.

The payload shape here is identical to /api/coldkeys/{ss58}, so inspecting a
rival's coldkey renders through exactly the same UI as your own.
"""
import secrets

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from ..db import pool
from ..security import CsrfGuard, RequireUser, audit

router = APIRouter(prefix="/api/me", tags=["portfolio"])

TAO_PER_DAY = "(n.emission * (7200.0 / NULLIF(s.tempo,0)) * s.price)"


class ColdkeyIn(BaseModel):
    coldkey: str
    label: str | None = None


@router.get("/coldkeys")
async def my_coldkeys(user=RequireUser):
    rows = await pool().fetch(
        f"""
        SELECT m.coldkey, m.label, m.created_at,
               count(n.uid)                   AS hotkeys,
               count(DISTINCT n.netuid)       AS subnets,
               COALESCE(sum({TAO_PER_DAY}),0) AS tao_per_day,
               COALESCE(sum(n.stake),0)       AS stake
        FROM my_coldkey m
        LEFT JOIN neuron_live n ON n.coldkey = m.coldkey
        LEFT JOIN subnet_live s ON s.netuid = n.netuid
        WHERE m.user_id = $1
        GROUP BY m.coldkey, m.label, m.created_at
        ORDER BY tao_per_day DESC
        """,
        user["id"],
    )
    return {"coldkeys": [dict(r) for r in rows]}


@router.post("/coldkeys", dependencies=[CsrfGuard])
async def add_coldkey(body: ColdkeyIn, request: Request, user=RequireUser):
    ck = body.coldkey.strip()
    if not (47 <= len(ck) <= 49) or not ck.startswith("5"):
        raise HTTPException(400, "that does not look like an SS58 coldkey address")
    await pool().execute(
        "INSERT INTO my_coldkey (user_id, coldkey, label) VALUES ($1,$2,$3)"
        " ON CONFLICT (user_id, coldkey) DO UPDATE SET label=EXCLUDED.label",
        user["id"], ck, body.label,
    )
    # keep the shared label table in sync so this coldkey is highlighted everywhere
    await pool().execute(
        "INSERT INTO coldkey_label (coldkey, label, is_ours) VALUES ($1,$2,true)"
        " ON CONFLICT (coldkey) DO UPDATE SET is_ours=true,"
        " label=COALESCE(EXCLUDED.label, coldkey_label.label), updated_at=now()",
        ck, body.label,
    )
    present = await pool().fetchval("SELECT count(*) FROM neuron_live WHERE coldkey=$1", ck)
    await audit(request, user["email"], "coldkey.add", {"coldkey": ck})
    return {"ok": True, "registered_uids": present}


@router.delete("/coldkeys/{coldkey}", dependencies=[CsrfGuard])
async def del_coldkey(coldkey: str, request: Request, user=RequireUser):
    await pool().execute("DELETE FROM my_coldkey WHERE user_id=$1 AND coldkey=$2", user["id"], coldkey)
    await pool().execute("UPDATE coldkey_label SET is_ours=false WHERE coldkey=$1", coldkey)
    await audit(request, user["email"], "coldkey.remove", {"coldkey": coldkey})
    return {"ok": True}


@router.get("/portfolio")
async def portfolio(user=RequireUser):
    """Every hotkey under every coldkey you registered, plus subnet rollups."""
    positions = await pool().fetch(
        f"""
        SELECT n.coldkey, m.label AS coldkey_label, n.netuid, s.name AS subnet_name,
               s.symbol, n.uid, n.hotkey, n.emission, n.emission_pct, n.incentive,
               n.dividends, n.stake, n.alpha_stake, n.tao_stake, n.validator_permit,
               n.active, n.rank_in_subnet, n.block_at_registration,
               {TAO_PER_DAY} AS tao_per_day,
               s.num_uids, s.immunity_period, s.price
        FROM my_coldkey m
        JOIN neuron_live n ON n.coldkey = m.coldkey
        JOIN subnet_live s ON s.netuid = n.netuid
        WHERE m.user_id = $1
        ORDER BY tao_per_day DESC NULLS LAST
        """,
        user["id"],
    )
    pos = [dict(p) for p in positions]
    by_subnet: dict[int, dict] = {}
    for p in pos:
        b = by_subnet.setdefault(p["netuid"], {
            "netuid": p["netuid"], "name": p["subnet_name"], "symbol": p["symbol"],
            "uids": 0, "tao_per_day": 0.0, "stake": 0.0, "emission_pct": 0.0,
        })
        b["uids"] += 1
        b["tao_per_day"] += p["tao_per_day"] or 0
        b["stake"] += p["stake"] or 0
        b["emission_pct"] += p["emission_pct"] or 0

    return {
        "totals": {
            "coldkeys": len({p["coldkey"] for p in pos}),
            "hotkeys": len(pos),
            "subnets": len(by_subnet),
            "tao_per_day": sum(p["tao_per_day"] or 0 for p in pos),
            "stake": sum(p["stake"] or 0 for p in pos),
        },
        "by_subnet": sorted(by_subnet.values(), key=lambda x: -x["tao_per_day"]),
        "positions": pos,
    }


@router.get("/history")
async def history(hours: int = 72, user=RequireUser):
    """Time series of what your hotkeys have been earning.

    Emission is valued at the subnet price recorded in the SAME 15-minute bucket,
    so the curve reflects both your work and the market, not one frozen price.
    """
    hours = max(1, min(hours, 24 * 30))
    rows = await pool().fetch(
        """
        WITH mine AS (
            SELECT time_bucket('15 minutes', ns.ts) AS b,
                   ns.ts, ns.netuid, ns.uid, ns.emission
            FROM neuron_snapshot ns
            JOIN my_coldkey m ON m.coldkey = ns.coldkey AND m.user_id = $1
            WHERE ns.ts > now() - ($2 || ' hours')::interval
        ),
        -- A restart triggers an immediate sweep, so one bucket can hold several
        -- full snapshots. Keep only the latest row per UID or the sums multiply.
        dedup AS (
            SELECT DISTINCT ON (b, netuid, uid) b, netuid, uid, emission
            FROM mine
            ORDER BY b, netuid, uid, ts DESC
        ),
        px AS (
            SELECT time_bucket('15 minutes', ts) AS b, netuid, last(price, ts) AS price
            FROM subnet_snapshot
            WHERE ts > now() - ($2 || ' hours')::interval
            GROUP BY 1, 2
        )
        SELECT d.b AS ts,
               sum(d.emission * (7200.0 / NULLIF(s.tempo,0))
                   * COALESCE(px.price, s.price))        AS tao_per_day,
               sum(d.emission)                            AS alpha_emission,
               count(*)                                   AS uids,
               count(*) FILTER (WHERE d.emission > 0)     AS earning_uids
        FROM dedup d
        JOIN subnet_live s ON s.netuid = d.netuid
        LEFT JOIN px ON px.netuid = d.netuid AND px.b = d.b
        GROUP BY d.b
        ORDER BY d.b
        """,
        user["id"], str(hours),
    )
    pts = [dict(r) for r in rows]
    first = pts[0]["tao_per_day"] if pts else None
    last = pts[-1]["tao_per_day"] if pts else None
    return {
        "hours": hours,
        "points": pts,
        "change_pct": ((last / first - 1) * 100) if first and last and first > 0 else None,
    }


# ---------------- requirement 3: wallet connect (challenge flow ready, signing later) ----------------
class WalletIn(BaseModel):
    ss58: str
    label: str | None = None


@router.get("/wallets")
async def wallets(user=RequireUser):
    rows = await pool().fetch(
        "SELECT id, ss58, label, verified_at, created_at FROM wallet_link WHERE user_id=$1 ORDER BY id",
        user["id"],
    )
    return {"wallets": [dict(r) for r in rows], "verification": "pending-implementation"}


@router.post("/wallets/challenge", dependencies=[CsrfGuard])
async def wallet_challenge(body: WalletIn, user=RequireUser):
    """Issue a nonce for the wallet to sign.

    The browser extension (polkadot-js / Talisman) signs this string; a later
    release verifies the sr25519 signature server-side and stamps verified_at.
    Storing the challenge now means the front end can be built against the real
    flow rather than a mock.
    """
    challenge = f"TaoScope wants to verify you own {body.ss58}. nonce={secrets.token_hex(16)}"
    await pool().execute(
        "INSERT INTO wallet_link (user_id, ss58, label, challenge) VALUES ($1,$2,$3,$4)"
        " ON CONFLICT (user_id, ss58) DO UPDATE SET challenge=EXCLUDED.challenge, label=EXCLUDED.label",
        user["id"], body.ss58.strip(), body.label, challenge,
    )
    return {"ss58": body.ss58, "challenge": challenge,
            "note": "Sign this with your wallet, then POST it to /api/me/wallets/verify."}


@router.post("/wallets/verify", dependencies=[CsrfGuard])
async def wallet_verify(user=RequireUser):
    raise HTTPException(
        501,
        "Signature verification is not enabled yet. The challenge flow and storage are in "
        "place; enabling it needs sr25519 verification plus a separate signing service.",
    )
