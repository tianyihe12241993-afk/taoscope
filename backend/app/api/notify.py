import secrets

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from ..ai import agent as ai
from ..config import settings
from ..db import pool
from ..security import CsrfGuard, RequireUser
from ..telegram import bot

router = APIRouter(prefix="/api/me", tags=["notify"])


@router.get("/telegram")
async def telegram_status(user=RequireUser):
    row = await pool().fetchrow(
        "SELECT chat_id, username, linked_at, link_code, prefs FROM telegram_link WHERE user_id=$1",
        user["id"],
    )
    username = None
    if settings.telegram_bot_token:
        me = await bot.call("getMe")
        username = (me or {}).get("username")
    return {
        "enabled": bool(settings.telegram_bot_token),
        "bot_username": username,
        "linked": bool(row and row["linked_at"]),
        "prefs": row["prefs"] if row and row["prefs"] else {},
        "chat_username": row["username"] if row else None,
        "link_code": row["link_code"] if row else None,
        "hint": None if settings.telegram_bot_token else
                "Set TAOSCOPE_TELEGRAM_BOT_TOKEN (from @BotFather) and restart the backend.",
    }


@router.post("/telegram/code", dependencies=[CsrfGuard])
async def telegram_code(user=RequireUser):
    if not settings.telegram_bot_token:
        raise HTTPException(503, "Telegram is not configured on the server.")
    code = secrets.token_hex(4).upper()
    await pool().execute(
        "INSERT INTO telegram_link (user_id, link_code) VALUES ($1,$2)"
        " ON CONFLICT (user_id) DO NOTHING",
        user["id"], code,
    )
    await pool().execute(
        "UPDATE telegram_link SET link_code=$2, chat_id=NULL, linked_at=NULL WHERE user_id=$1",
        user["id"], code,
    )
    return {"code": code, "instructions": f"Send  /link {code}  to the bot in Telegram."}


@router.delete("/telegram", dependencies=[CsrfGuard])
async def telegram_unlink(user=RequireUser):
    await pool().execute("DELETE FROM telegram_link WHERE user_id=$1", user["id"])
    return {"ok": True}


@router.get("/ai")
async def ai_status(user=RequireUser):
    """Whether Q&A is on, and what it has cost."""
    stats = await pool().fetchrow(
        """
        SELECT count(*) FILTER (WHERE error IS NULL)            AS answered,
               count(*) FILTER (WHERE error IS NOT NULL)        AS failed,
               COALESCE(sum(cost_usd), 0)                       AS spend_usd,
               COALESCE(avg(ms) FILTER (WHERE error IS NULL),0) AS avg_ms
        FROM ai_usage WHERE ts > now() - interval '30 days'
        """
    )
    today = await pool().fetchrow(
        "SELECT count(*) AS answered, COALESCE(sum(cost_usd),0) AS spend_usd"
        " FROM ai_usage WHERE ts > now() - interval '24 hours' AND error IS NULL"
    )
    return {
        "enabled": ai.available(),
        "provider": "OpenRouter" if ai.via_openrouter() else "Anthropic",
        "model": ai.model_id(),
        "effort": settings.ai_effort,
        "daily_limit": settings.ai_daily_limit,
        "last_30d": dict(stats),
        "last_24h": dict(today),
        "hint": None if ai.available() else
                "Set TAOSCOPE_ANTHROPIC_API_KEY in .env and restart the backend.",
    }


class PrefsIn(BaseModel):
    new_subnet: bool | None = None
    subnet_started: bool | None = None
    king_change: bool | None = None
    registration: bool | None = None
    my_miners: bool | None = None
    emission_move: bool | None = None
    price_alert: bool | None = None


@router.put("/telegram/prefs", dependencies=[CsrfGuard])
async def set_prefs(body: PrefsIn, user=RequireUser):
    import json
    sent = body.model_dump(exclude_unset=True)
    if not sent:
        return {"ok": True}
    await pool().execute(
        "INSERT INTO telegram_link (user_id, prefs) VALUES ($1, $2::jsonb)"
        " ON CONFLICT (user_id) DO UPDATE SET prefs = telegram_link.prefs || EXCLUDED.prefs",
        user["id"], json.dumps(sent),
    )
    row = await pool().fetchrow("SELECT prefs FROM telegram_link WHERE user_id=$1", user["id"])
    return {"ok": True, "prefs": dict(row)["prefs"] if row else {}}


class AlertIn(BaseModel):
    netuid: int
    direction: str
    threshold: float


@router.get("/alerts")
async def list_alerts(user=RequireUser):
    rows = await pool().fetch(
        "SELECT a.*, s.name, s.price FROM price_alert a LEFT JOIN subnet_live s USING (netuid)"
        " WHERE a.user_id=$1 ORDER BY a.id DESC",
        user["id"],
    )
    return {"alerts": [dict(r) for r in rows]}


@router.post("/alerts", dependencies=[CsrfGuard])
async def add_alert(body: AlertIn, user=RequireUser):
    if body.direction not in ("above", "below"):
        raise HTTPException(400, "direction must be 'above' or 'below'")
    await pool().execute(
        "INSERT INTO price_alert (netuid, direction, threshold, user_id) VALUES ($1,$2,$3,$4)",
        body.netuid, body.direction, body.threshold, user["id"],
    )
    return {"ok": True}


@router.delete("/alerts/{aid}", dependencies=[CsrfGuard])
async def del_alert(aid: int, user=RequireUser):
    await pool().execute("DELETE FROM price_alert WHERE id=$1 AND user_id=$2", aid, user["id"])
    return {"ok": True}
