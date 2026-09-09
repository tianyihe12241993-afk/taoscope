"""One reader per API provider, all returning the same shape.

Contract — the distinction that decides whether an alert is honest:

  * `None`                     -> no key configured. Silent. Not a problem.
  * `{"ok": False, "dead": True}` -> the provider REJECTED our key (401/403).
    That is alertable and urgent: a dead key fails every run that uses it.
  * `{"ok": False, "dead": False}` -> the call failed for some other reason
    (timeout, 5xx). NOT alertable — absence is not a change, and a provider
    having a bad five minutes must never look like "we ran out of money".
  * `{"ok": True, "remaining": ...}` -> a real reading.

`remaining` is what everything downstream keys on, and it is None whenever the
provider will not tell us a balance. A None balance is never treated as zero.
"""
from __future__ import annotations

import asyncio

import logging

import httpx

from ..config import settings

log = logging.getLogger("taoscope.credits")

TIMEOUT = 25.0


async def _get(url: str, headers: dict, **kw):
    """(status, json|None). status 0 means the call never completed."""
    try:
        async with httpx.AsyncClient(timeout=TIMEOUT, follow_redirects=True) as cx:
            r = await cx.get(url, headers=headers, **kw)
            try:
                return r.status_code, r.json()
            except ValueError:
                return r.status_code, None
    except Exception as exc:  # noqa: BLE001
        log.debug("credits GET %s failed: %s", url, exc)
        return 0, None


def _dead(status: int) -> dict:
    return {"ok": False, "dead": status in (401, 403), "status": status}


async def openrouter() -> dict | None:
    """Balance = purchased credits minus all-time usage.

    The per-key endpoint reports `limit`/`limit_remaining`, but those are the
    KEY's spend cap and are null on an uncapped key -- they say nothing about
    the account balance. `/credits` is the number that runs out.
    """
    key = settings.openrouter_api_key
    if not key:
        return None
    h = {"Authorization": f"Bearer {key}"}
    st, d = await _get("https://openrouter.ai/api/v1/credits", h)
    if st != 200 or not isinstance(d, dict):
        return _dead(st)
    data = d.get("data") or {}
    total = data.get("total_credits")
    used = data.get("total_usage")
    out: dict = {"ok": True, "unit": "$", "used": used, "limit": total}
    if total is not None and used is not None:
        out["remaining"] = float(total) - float(used)
    # Daily burn straight from the provider beats inferring it from two polls.
    st2, k = await _get("https://openrouter.ai/api/v1/key", h)
    if st2 == 200 and isinstance(k, dict):
        kd = k.get("data") or k
        if kd.get("usage_daily") is not None:
            out["used_today"] = kd["usage_daily"]
    return out


async def chutes() -> dict | None:
    key = settings.chutes_api_key
    if not key:
        return None
    st, d = await _get("https://api.chutes.ai/users/me",
                       {"Authorization": f"Bearer {key}"})
    if st != 200 or not isinstance(d, dict):
        return _dead(st)
    # Field names are not documented publicly; take the first that exists
    # rather than guessing one and silently reporting None forever.
    bal = _first(d, "balance", "credits", "credit_balance", "remaining_balance")
    used = _first(d, "used", "usage", "total_usage", "spent")
    out = {"ok": True, "unit": "$", "remaining": bal, "used": used}
    if bal is None:
        out["note"] = "no balance field in /users/me — see raw"
        out["raw_keys"] = sorted(k for k in d if not k.startswith("_"))[:12]
    return out


async def lium() -> dict | None:
    """Balance from `GET /users/me`.

    Taken from Lium's own SDK (Datura-ai/lium, lium/sdk/client.py), not guessed:
    base_url is `https://lium.io/api`, the header is `X-API-KEY`, and the CLI
    reads the balance as `.json().get("balance")`.

    Worth recording why: pointing this at the plausible-looking `api.lium.ai`
    returned 401, which the monitor would have reported as A DEAD KEY. A wrong
    endpoint and a revoked key are indistinguishable from the status code alone,
    so the endpoint has to come from the operator's source, never from a guess.
    """
    key = settings.lium_api_key
    if not key:
        return None
    base = settings.lium_api_base.rstrip("/")
    h = {"X-API-KEY": key}
    st, d = await _get(f"{base}/users/me", h)
    if st != 200 or not isinstance(d, dict):
        return _dead(st)
    out = {"ok": True, "unit": "$",
           "remaining": _first(d, "balance", "credits", "funds"),
           "used": _first(d, "spent", "used", "total_spent")}
    if out["remaining"] is None:
        out["raw_keys"] = sorted(k for k in d if not k.startswith("_"))[:12]

    # Pods are where the money actually goes, and `price` is the pod's total
    # $/h, so the burn rate can be COMPUTED rather than inferred from two
    # balance samples. Checked against the sampled figure on a live pod:
    # $1.32/h gives a 22.3h runway where sampling said 21.6h. Exact wins --
    # it is correct the moment a pod starts, instead of a poll later.
    st2, pods = await _get(f"{base}/pods", h)
    if st2 == 200 and isinstance(pods, list):
        out["pods"] = {
            (p.get("id") or "")[:8]: {
                "name": p.get("pod_name") or "",
                "status": (p.get("status") or "unknown").upper(),
                "price": p.get("price"),
                "gpu": p.get("gpu_name") or "",
                "gpus": p.get("gpu_count"),
                "created_at": p.get("created_at") or "",
                # Lium reclaims pods before the balance reaches zero; when it
                # does, this stops being null and that is the alert.
                "removal_at": p.get("removal_scheduled_at"),
            } for p in pods if p.get("id")
        }
        running = [p for p in out["pods"].values() if p["status"] == "RUNNING"]
        out["pods_running"] = len(running)
        billed = [float(p["price"]) for p in running if p.get("price") is not None]
        if billed:
            out["burn_per_h"] = sum(billed)
    return out


async def parallel() -> dict | None:
    """Liveness, and a balance only if an OAuth token is configured.

    Parallel's balance endpoint refuses API keys outright. Verified against the
    live API, not just the docs:

        GET /account/service/v1/balance  + x-api-key      -> 401 "Missing bearer token"
        GET /account/service/v1/balance  + Bearer <apikey> -> 401 VerifyServiceJwt

    It wants a JWT minted through Parallel's OAuth device flow. So unless one is
    configured we check the thing we CAN check -- that the key still
    authenticates -- by asking for a task run id that cannot exist: a live key
    gets 404 (authenticated, no such run) and a dead one gets 401. It creates
    nothing and costs nothing.
    """
    key = settings.parallel_api_key
    tok = settings.parallel_oauth_token
    if not key and not tok:
        return None

    if tok:
        st, d = await _get("https://api.parallel.ai/account/service/v1/balance",
                           {"Authorization": f"Bearer {tok}"})
        if st == 200 and isinstance(d, dict):
            cents = d.get("credit_balance_cents")
            pending = d.get("pending_debit_balance_cents") or 0
            out = {"ok": True, "unit": "$"}
            if cents is not None:
                out["remaining"] = (float(cents) - float(pending)) / 100.0
            return out
        if st in (401, 403):
            return {"ok": True, "unit": "$", "remaining": None,
                    "note": "OAuth token expired — re-mint it"}

    st, _ = await _get("https://api.parallel.ai/v1/tasks/runs/trun_liveness_probe",
                       {"x-api-key": key})
    if st in (401, 403):
        return _dead(st)
    if st == 404:
        return {"ok": True, "unit": "$", "remaining": None,
                "note": "key valid · no balance for API keys (OAuth only)"}
    return {"ok": True, "unit": "$", "remaining": None,
            "note": f"key check returned HTTP {st}"}


async def vercel() -> dict | None:
    token = settings.vercel_api_token
    if not token:
        return None
    q = f"?teamId={settings.vercel_team_id}" if settings.vercel_team_id else ""
    st, d = await _get(f"https://api.vercel.com/v2/user{q}",
                       {"Authorization": f"Bearer {token}"})
    if st != 200 or not isinstance(d, dict):
        return _dead(st)
    u = d.get("user") or d
    # Vercel bills on plan + overage rather than a prepaid balance, so there is
    # no "remaining" to report; liveness and plan are the useful facts.
    plan = u.get("version") or (u.get("billing") or {}).get("plan") or "?"
    who = u.get("username") or u.get("email") or "account ok"
    return {"ok": True, "unit": "", "remaining": None,
            "note": f"token valid · plan {plan} · {who} · "
                    f"no prepaid balance (billed per plan)"}


def _first(d: dict, *names):
    for n in names:
        v = d.get(n)
        if isinstance(v, (int, float)):
            return float(v)
        if isinstance(v, dict):
            for inner in ("amount", "value", "balance", "usd"):
                if isinstance(v.get(inner), (int, float)):
                    return float(v[inner])
    return None


# Order is display order.
PROVIDERS = {
    "lium": lium,
    "openrouter": openrouter,
    "parallel": parallel,
    "chutes": chutes,
    "vercel": vercel,
}


def enabled() -> list[str]:
    """Providers we poll and display, in display order."""
    want = {p.strip().lower() for p in (settings.credits_providers or "").split(",")
            if p.strip()}
    return [n for n in PROVIDERS if not want or n in want]


async def _read_one(name: str) -> dict | None:
    try:
        return await PROVIDERS[name]()
    except Exception as exc:  # noqa: BLE001
        log.warning("credits %s raised: %s", name, exc)
        return {"ok": False, "dead": False, "status": 0}


async def read_all() -> dict[str, dict]:
    """{provider: reading} for every enabled provider that has a key configured.

    Providers are read concurrently: /keys now waits on this, and three
    sequential 25s timeouts would have made a slow provider hold the reply
    for over a minute."""
    names = enabled()
    readings = await asyncio.gather(*(_read_one(n) for n in names))
    return {n: r for n, r in zip(names, readings) if r is not None}
