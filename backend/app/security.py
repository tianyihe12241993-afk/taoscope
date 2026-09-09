import datetime as dt
import secrets

import bcrypt
import jwt
from fastapi import Depends, HTTPException, Request, Response, status

from .config import settings
from .db import pool

ALGO = "HS256"
COOKIE = "taoscope_token"


# ---------- passwords ----------
def hash_password(raw: str) -> str:
    return bcrypt.hashpw(raw.encode(), bcrypt.gensalt()).decode()


def verify_password(raw: str, hashed: str | None) -> bool:
    if not hashed:
        return False
    try:
        return bcrypt.checkpw(raw.encode(), hashed.encode())
    except ValueError:
        return False


# ---------- sessions ----------
async def issue_session(user: dict, request: Request, response: Response) -> str:
    """Mint a JWT whose jti is recorded server-side, so it can be revoked."""
    jti = secrets.token_urlsafe(24)
    now = dt.datetime.now(dt.timezone.utc)
    exp = now + dt.timedelta(hours=settings.jwt_ttl_hours)

    await pool().execute(
        "INSERT INTO user_session (jti, user_id, expires_at, ip, user_agent)"
        " VALUES ($1,$2,$3,$4,$5)",
        jti, user["id"], exp, client_ip(request),
        (request.headers.get("user-agent") or "")[:400],
    )
    await pool().execute("UPDATE app_user SET last_login=now() WHERE id=$1", user["id"])

    token = jwt.encode(
        {"sub": user["email"], "uid": user["id"], "role": user["role"],
         "jti": jti, "iat": now, "exp": exp},
        settings.jwt_secret, algorithm=ALGO,
    )
    response.set_cookie(
        COOKIE, token,
        httponly=True,
        samesite="lax",
        secure=settings.cookie_secure,
        max_age=settings.jwt_ttl_hours * 3600,
        path="/",
    )
    return token


async def revoke_session(jti: str) -> None:
    await pool().execute(
        "UPDATE user_session SET revoked_at=now() WHERE jti=$1 AND revoked_at IS NULL", jti
    )


def decode_token(token: str) -> dict:
    return jwt.decode(token, settings.jwt_secret, algorithms=[ALGO])


def _token_from(request: Request) -> str | None:
    auth = request.headers.get("authorization", "")
    if auth.lower().startswith("bearer "):
        return auth[7:].strip()
    return request.cookies.get(COOKIE)


def client_ip(request: Request) -> str:
    """The peer address, as reported by the proxy.

    Safe against a client spoofing its own X-Forwarded-For *because Caddy
    replaces that header* with the real peer rather than appending to it:
    ops/Caddyfile sets no `trusted_proxies`, and Caddy honours an inbound XFF
    only from a trusted one (verified against caddy:2-alpine v2.11.4 -- a
    request sent with "X-Forwarded-For: 1.2.3.4, 5.6.7.8" reaches the backend
    as the peer address alone). This value is the throttle key in check_rate()
    and the recorded ip in login_attempt and audit_log, so if anyone ever adds
    `trusted_proxies`, publishes port 8000, or fronts this with another proxy,
    revisit this function -- XFF becomes caller-controlled at that moment.
    """
    fwd = request.headers.get("x-forwarded-for")
    if fwd:
        return fwd.split(",")[0].strip()
    return request.client.host if request.client else "?"


async def current_user(request: Request) -> dict:
    token = _token_from(request)
    if not token:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "not authenticated")
    try:
        claims = decode_token(token)
    except jwt.PyJWTError:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid or expired token")

    jti = claims.get("jti")
    if not jti:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "stale token, sign in again")

    row = await pool().fetchrow(
        """
        SELECT s.jti, s.revoked_at, s.expires_at, u.id, u.email, u.role, u.disabled
        FROM user_session s JOIN app_user u ON u.id = s.user_id
        WHERE s.jti = $1
        """,
        jti,
    )
    if row is None or row["revoked_at"] is not None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "session revoked")
    if row["disabled"]:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "account disabled")
    if row["expires_at"] < dt.datetime.now(dt.timezone.utc):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "session expired")

    return {"id": row["id"], "email": row["email"], "role": row["role"], "jti": jti}


async def require_admin(user: dict = Depends(current_user)) -> dict:
    if user.get("role") != "admin":
        raise HTTPException(status.HTTP_403_FORBIDDEN, "admin only")
    return user


# ---------- CSRF ----------
SAFE = {"GET", "HEAD", "OPTIONS"}


async def csrf_guard(request: Request) -> None:
    """Cookie auth + a required custom header.

    Browsers cannot set X-Requested-With cross-origin without a preflight that
    our CORS policy refuses, so a mutation carrying it cannot have been forged
    by another site. Bearer-token callers are exempt (not cookie-driven).
    """
    if request.method in SAFE:
        return
    if request.headers.get("authorization", "").lower().startswith("bearer "):
        return
    if request.headers.get("x-requested-with") != "taoscope":
        raise HTTPException(status.HTTP_403_FORBIDDEN, "missing CSRF header")


# ---------- brute-force throttle ----------
async def check_rate(request: Request, email: str | None = None) -> None:
    """Throttle per (ip, account) plus a looser per-IP ceiling.

    Scoping to the account matters: with a bare per-IP counter, anyone hammering
    a bogus address locks the real user out from the same NAT. The per-IP ceiling
    still stops someone spraying many accounts from one host.
    """
    ip = client_ip(request)
    per_account = await pool().fetchval(
        "SELECT count(*) FROM login_attempt"
        " WHERE ip=$1 AND lower(email)=lower($2) AND success=false"
        "   AND ts > now() - interval '15 minutes'",
        ip, email or "",
    )
    per_ip = await pool().fetchval(
        "SELECT count(*) FROM login_attempt"
        " WHERE ip=$1 AND success=false AND ts > now() - interval '15 minutes'",
        ip,
    )
    limit = settings.max_login_attempts
    if (per_account and per_account >= limit) or (per_ip and per_ip >= limit * 5):
        raise HTTPException(
            status.HTTP_429_TOO_MANY_REQUESTS,
            "too many failed attempts; try again in 15 minutes",
        )


async def record_attempt(request: Request, email: str | None, success: bool) -> None:
    await pool().execute(
        "INSERT INTO login_attempt (ip, email, success) VALUES ($1,$2,$3)",
        client_ip(request), email, success,
    )


async def audit(request: Request, actor: str | None, action: str, detail: dict | None = None) -> None:
    import json
    await pool().execute(
        "INSERT INTO audit_log (actor, action, detail, ip) VALUES ($1,$2,$3::jsonb,$4)",
        actor, action, json.dumps(detail or {}), client_ip(request),
    )


RequireUser = Depends(current_user)
RequireAdmin = Depends(require_admin)
CsrfGuard = Depends(csrf_guard)
