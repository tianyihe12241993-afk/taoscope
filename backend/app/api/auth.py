import secrets
import urllib.parse

import httpx
import jwt
from fastapi import APIRouter, HTTPException, Request, Response, status
from fastapi.responses import RedirectResponse
from pydantic import BaseModel, EmailStr

from ..config import settings
from ..db import pool
from ..security import (
    CsrfGuard, RequireAdmin, RequireUser, audit, check_rate, hash_password,
    issue_session, record_attempt, revoke_session, verify_password,
)

router = APIRouter(prefix="/api/auth", tags=["auth"])

GOOGLE_AUTH = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN = "https://oauth2.googleapis.com/token"
GOOGLE_JWKS = "https://www.googleapis.com/oauth2/v3/certs"
STATE_COOKIE = "taoscope_oauth_state"


def google_ready() -> bool:
    return bool(settings.google_client_id and settings.google_client_secret and settings.public_url)


def redirect_uri() -> str:
    return settings.public_url.rstrip("/") + "/api/auth/google/callback"


@router.get("/config")
async def auth_config():
    """Lets the login page know which methods are actually usable."""
    return {
        "google_enabled": google_ready(),
        "password_enabled": True,
        "reason": None if google_ready() else
                  "Google sign-in needs TAOSCOPE_PUBLIC_URL (a real domain) plus client id/secret.",
    }


# ---------------- password login ----------------
class LoginIn(BaseModel):
    email: EmailStr
    password: str


@router.post("/login")
async def login(body: LoginIn, request: Request, response: Response):
    await check_rate(request, body.email)
    row = await pool().fetchrow(
        "SELECT id, email, password_hash, role, disabled FROM app_user"
        " WHERE lower(email)=lower($1)",
        body.email,
    )
    if row is None or row["disabled"] or not verify_password(body.password, row["password_hash"]):
        await record_attempt(request, body.email, False)
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "bad credentials")

    await record_attempt(request, body.email, True)
    token = await issue_session(dict(row), request, response)
    await audit(request, row["email"], "login.password")
    return {"token": token, "email": row["email"], "role": row["role"]}


# ---------------- google oauth ----------------
@router.get("/google/start")
async def google_start(request: Request):
    if not google_ready():
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "Google sign-in is not configured. Set TAOSCOPE_PUBLIC_URL, "
            "TAOSCOPE_GOOGLE_CLIENT_ID and TAOSCOPE_GOOGLE_CLIENT_SECRET.",
        )
    state = secrets.token_urlsafe(24)
    nonce = secrets.token_urlsafe(16)
    params = {
        "client_id": settings.google_client_id,
        "redirect_uri": redirect_uri(),
        "response_type": "code",
        "scope": "openid email profile",
        "state": state,
        "nonce": nonce,
        "prompt": "select_account",
    }
    resp = RedirectResponse(f"{GOOGLE_AUTH}?{urllib.parse.urlencode(params)}")
    resp.set_cookie(STATE_COOKIE, f"{state}:{nonce}", httponly=True, samesite="lax",
                    secure=settings.cookie_secure, max_age=600, path="/")
    return resp


@router.get("/google/callback")
async def google_callback(request: Request, code: str = "", state: str = "", error: str = ""):
    if error:
        return RedirectResponse(f"/login?error={urllib.parse.quote(error)}")
    if not google_ready():
        raise HTTPException(503, "Google sign-in is not configured")

    saved = request.cookies.get(STATE_COOKIE) or ""
    want_state, _, want_nonce = saved.partition(":")
    if not want_state or state != want_state:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "bad OAuth state")

    async with httpx.AsyncClient(timeout=20) as cx:
        tok = await cx.post(GOOGLE_TOKEN, data={
            "code": code,
            "client_id": settings.google_client_id,
            "client_secret": settings.google_client_secret,
            "redirect_uri": redirect_uri(),
            "grant_type": "authorization_code",
        })
    if tok.status_code != 200:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"token exchange failed: {tok.text[:200]}")

    id_token = tok.json().get("id_token")
    if not id_token:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "no id_token returned")

    # Verify the signature against Google's JWKS — never trust an unverified token.
    try:
        signing_key = jwt.PyJWKClient(GOOGLE_JWKS).get_signing_key_from_jwt(id_token)
        claims = jwt.decode(
            id_token, signing_key.key, algorithms=["RS256"],
            audience=settings.google_client_id, issuer="https://accounts.google.com",
        )
    except jwt.PyJWTError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"invalid id_token: {exc}")

    if want_nonce and claims.get("nonce") not in (None, want_nonce):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "nonce mismatch")
    if not claims.get("email_verified"):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Google email is not verified")

    email = (claims.get("email") or "").lower()
    allowed = await pool().fetchrow("SELECT role FROM email_allowlist WHERE lower(email)=$1", email)
    if allowed is None:
        await audit(request, email, "login.google.denied", {"reason": "not allowlisted"})
        return RedirectResponse("/login?error=not_allowed")

    row = await pool().fetchrow("SELECT id, email, role, disabled FROM app_user WHERE lower(email)=$1", email)
    if row is None:
        row = await pool().fetchrow(
            "INSERT INTO app_user (email, role, google_sub, display_name, avatar_url)"
            " VALUES ($1,$2,$3,$4,$5) RETURNING id, email, role, disabled",
            email, allowed["role"], claims.get("sub"), claims.get("name"), claims.get("picture"),
        )
    else:
        await pool().execute(
            "UPDATE app_user SET google_sub=COALESCE(google_sub,$2), display_name=COALESCE($3,display_name),"
            " avatar_url=COALESCE($4,avatar_url) WHERE id=$1",
            row["id"], claims.get("sub"), claims.get("name"), claims.get("picture"),
        )
    if row["disabled"]:
        return RedirectResponse("/login?error=disabled")

    resp = RedirectResponse("/")
    await issue_session(dict(row), request, resp)
    resp.delete_cookie(STATE_COOKIE, path="/")
    await audit(request, email, "login.google")
    return resp


# ---------------- session ----------------
@router.post("/logout", dependencies=[CsrfGuard])
async def logout(request: Request, response: Response, user=RequireUser):
    await revoke_session(user["jti"])
    response.delete_cookie("taoscope_token", path="/")
    await audit(request, user["email"], "logout")
    return {"ok": True}


@router.get("/me")
async def me(user=RequireUser):
    row = await pool().fetchrow(
        "SELECT email, role, display_name, avatar_url, last_login FROM app_user WHERE id=$1",
        user["id"],
    )
    return dict(row) if row else {}


@router.get("/sessions")
async def sessions(user=RequireUser):
    rows = await pool().fetch(
        "SELECT jti, issued_at, expires_at, revoked_at, ip, user_agent FROM user_session"
        " WHERE user_id=$1 ORDER BY issued_at DESC LIMIT 50",
        user["id"],
    )
    return {"sessions": [{**dict(r), "current": r["jti"] == user["jti"]} for r in rows]}


@router.delete("/sessions/{jti}", dependencies=[CsrfGuard])
async def kill_session(jti: str, request: Request, user=RequireUser):
    await pool().execute(
        "UPDATE user_session SET revoked_at=now() WHERE jti=$1 AND user_id=$2", jti, user["id"]
    )
    await audit(request, user["email"], "session.revoke", {"jti": jti})
    return {"ok": True}


# ---------------- allowlist admin ----------------
class AllowIn(BaseModel):
    email: EmailStr
    role: str = "member"
    note: str | None = None


@router.get("/allowlist")
async def get_allowlist(user=RequireAdmin):
    rows = await pool().fetch("SELECT * FROM email_allowlist ORDER BY created_at")
    users = await pool().fetch(
        "SELECT id, email, role, disabled, last_login, display_name FROM app_user ORDER BY id"
    )
    return {"allowlist": [dict(r) for r in rows], "users": [dict(u) for u in users]}


@router.post("/allowlist", dependencies=[CsrfGuard])
async def add_allow(body: AllowIn, request: Request, user=RequireAdmin):
    await pool().execute(
        "INSERT INTO email_allowlist (email, role, note, added_by) VALUES (lower($1),$2,$3,$4)"
        " ON CONFLICT (email) DO UPDATE SET role=EXCLUDED.role, note=EXCLUDED.note",
        body.email, body.role, body.note, user["email"],
    )
    await audit(request, user["email"], "allowlist.add", {"email": body.email})
    return {"ok": True}


@router.delete("/allowlist/{email}", dependencies=[CsrfGuard])
async def del_allow(email: str, request: Request, user=RequireAdmin):
    await pool().execute("DELETE FROM email_allowlist WHERE lower(email)=lower($1)", email)
    await audit(request, user["email"], "allowlist.remove", {"email": email})
    return {"ok": True}


@router.get("/audit")
async def get_audit(limit: int = 200, user=RequireAdmin):
    rows = await pool().fetch("SELECT * FROM audit_log ORDER BY ts DESC LIMIT $1", limit)
    return {"events": [dict(r) for r in rows]}


# ---------------- bootstrap ----------------
async def ensure_admin(email: str, password: str) -> None:
    """Seed the first account and the allowlist from env. Never overwrites."""
    seeds = [e.strip().lower() for e in settings.allowed_emails.split(",") if e.strip()]
    if email:
        seeds.append(email.lower())
    for e in seeds:
        await pool().execute(
            "INSERT INTO email_allowlist (email, role, note, added_by) VALUES ($1,'admin','seeded from env','system')"
            " ON CONFLICT (email) DO NOTHING",
            e,
        )
    if not email or not password:
        return
    exists = await pool().fetchval("SELECT 1 FROM app_user WHERE lower(email)=lower($1)", email)
    if not exists:
        await pool().execute(
            "INSERT INTO app_user (email, password_hash, role) VALUES ($1,$2,'admin')",
            email, hash_password(password),
        )
