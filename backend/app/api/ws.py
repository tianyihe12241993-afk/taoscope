import asyncio
import json
import logging

import jwt
from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from ..db import pool
from ..hub import hub
from ..security import decode_token

log = logging.getLogger("taoscope.ws")
router = APIRouter()


@router.websocket("/ws")
async def live(ws: WebSocket):
    """Live push of every poll. Token comes via ?token= or the session cookie."""
    token = ws.query_params.get("token") or ws.cookies.get("taoscope_token")
    if not token:
        await ws.close(code=4401)
        return
    try:
        claims = decode_token(token)
    except jwt.PyJWTError:
        await ws.close(code=4401)
        return

    # a revoked session must not keep a live socket open
    ok = await pool().fetchval(
        "SELECT 1 FROM user_session WHERE jti=$1 AND revoked_at IS NULL AND expires_at > now()",
        claims.get("jti"),
    )
    if not ok:
        await ws.close(code=4401)
        return

    await ws.accept()
    hub.register(ws)
    try:
        # prime the client with current state so it renders immediately
        await ws.send_text(json.dumps(
            {"event": "subnets",
             "data": {"block": hub.status.get("block"), "tao_usd": hub.tao_usd,
                      "subnets": hub.subnets}},
            default=str,
        ))
        while True:
            # client is push-only; this just detects disconnects
            await asyncio.wait_for(ws.receive_text(), timeout=300)
    except (WebSocketDisconnect, asyncio.TimeoutError, RuntimeError):
        pass
    except Exception:  # noqa: BLE001
        log.debug("websocket closed unexpectedly", exc_info=True)
    finally:
        hub.unregister(ws)
