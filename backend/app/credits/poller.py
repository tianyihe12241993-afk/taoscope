"""Watch API credit balances and speak only when a decision changes.

Why this exists: on 2026-08-20 the OpenRouter key had $10.58 left against a
$2827 lifetime spend, and an SN114 eval had already run that same key dry once
before. Nothing in the stack noticed either time. A provider running out does
not fail loudly -- it fails in the middle of a run, hours in.

The alerting rules are the same three the subnet adapters live by:

  * A THRESHOLD CROSSING speaks, not a balance. Diffing the number itself would
    fire every poll; the balance is bucketed against `credits_thresholds` and
    only a tighter bucket is news.
  * ABSENCE IS NOT A CHANGE. A 5xx or a timeout leaves the stored reading alone
    and says nothing. Only an explicit 401/403 -- the provider rejecting our
    key -- is treated as a real event, because that one silently breaks runs.
  * FIRST SIGHT IS A BASELINE. A fresh row never alerts, or every restart
    replays "you are low on credit" into the chat.
"""
from __future__ import annotations

import asyncio
import datetime as dt
import json
import logging
import os

from ..comp import router, store as comp_store
from ..comp.base import SEVERITY
from ..config import settings
from ..db import pool
from ..hub import hub
from . import providers

log = logging.getLogger("taoscope.credits.poller")

# comp_event.netuid is a plain column with no foreign key, so credit events can
# share the table -- and with it the cooldown/dedup machinery. The sentinel is
# NEGATIVE on purpose: 0 is the root subnet, and a credit alert must never be
# mistaken for, or surface in, a real subnet's /events.
CREDIT_NETUID = -1


def thresholds(provider: str | None = None) -> list[float]:
    """Alert thresholds, per provider where set.

    Providers sit on wildly different scales -- an OpenRouter account that has
    spent $2,827 dropping under $100 is worth knowing about, while a Chutes
    account that never held more than $70 is simply healthy. A single global
    list therefore mislabels one of them, so any provider can override it with
    TAOSCOPE_CREDITS_THRESHOLDS_<PROVIDER>.
    """
    raw = ""
    if provider:
        raw = os.environ.get(
            f"TAOSCOPE_CREDITS_THRESHOLDS_{provider.upper()}", "")
    raw = raw or settings.credits_thresholds or ""
    out = []
    for part in raw.split(","):
        part = part.strip()
        if part:
            try:
                out.append(float(part))
            except ValueError:
                continue
    return sorted(out, reverse=True)


def bucket(remaining, provider: str | None = None) -> float | None:
    """The tightest threshold this balance has fallen to, or None if healthy."""
    if remaining is None:
        return None
    crossed = [t for t in thresholds(provider) if remaining <= t]
    return min(crossed) if crossed else None


def severity_for(b: float | None) -> str:
    """Only a genuinely small number is allowed to buzz.

    Crossing $100 or $50 is context, not an emergency, and a monitor that
    buzzes for context gets muted -- so those arrive silently and the phone
    only rings under $20.
    """
    if b is None:
        return "info"
    if b <= 5:
        return "critical"
    return "warn" if b <= 20 else "info"


def money(v, unit: str = "$") -> str:
    if v is None:
        return "—"
    try:
        return f"{unit}{float(v):,.2f}"
    except (TypeError, ValueError):
        return "—"


def runway(remaining, burn_per_h) -> str:
    """How long the money lasts at the observed burn. The number that matters."""
    try:
        if not remaining or not burn_per_h or burn_per_h <= 0:
            return ""
        h = float(remaining) / float(burn_per_h)
    except (TypeError, ValueError, ZeroDivisionError):
        return ""
    if h < 1:
        return f"{int(h * 60)}m left"
    if h < 48:
        return f"{h:.1f}h left"
    return f"{h / 24:.1f}d left"


async def last(provider: str) -> dict:
    row = await pool().fetchrow(
        "SELECT data, updated_at FROM api_credit WHERE provider=$1", provider)
    if not row:
        return {}
    d = dict(row["data"] or {})
    if row["updated_at"]:
        d["_at"] = row["updated_at"].isoformat()
    return d


async def save(provider: str, data: dict, error: str | None = None) -> None:
    payload = {k: v for k, v in data.items() if not k.startswith("_")}
    await pool().execute(
        "INSERT INTO api_credit (provider, data, last_error, polls)"
        " VALUES ($1,$2::jsonb,$3,1)"
        " ON CONFLICT (provider) DO UPDATE SET data=EXCLUDED.data,"
        "   updated_at=now(), last_error=EXCLUDED.last_error,"
        "   polls=api_credit.polls+1",
        provider, json.dumps(payload, default=str), error)


async def all_rows() -> list[dict]:
    """Stored readings for the ENABLED providers only.

    Filtered on read as well as on write: a provider switched off after it had
    already been polled still has a row, and /keys must not resurrect it.
    """
    live = set(providers.enabled())
    rows = await pool().fetch(
        "SELECT provider, data, updated_at, last_error FROM api_credit"
        " WHERE provider = ANY($1::text[]) ORDER BY provider", list(live))
    out = []
    for r in rows:
        d = dict(r["data"] or {})
        d["provider"] = r["provider"]
        d["_at"] = r["updated_at"]
        d["_error"] = r["last_error"]
        out.append(d)
    return out


# A burn rate measured over a couple of minutes is mostly noise: one $0.40
# request inside a 2-minute window reads as $12/h and turns a comfortable
# runway into a false "29m left". Refuse to estimate over a short window, and
# smooth what survives, so the runway is a number worth acting on.
MIN_BURN_WINDOW_H = 5 / 60
BURN_SMOOTHING = 0.3          # weight on the newest sample


def _burn(old: dict, new: dict) -> float | None:
    """$/hour between two readings, smoothed. None if not measurable yet."""
    try:
        prev, now_ = old.get("remaining"), new.get("remaining")
        if prev is None or now_ is None or now_ > prev:
            return None
        t0 = dt.datetime.fromisoformat(old["_at"])
        hours = (dt.datetime.now(dt.timezone.utc) - t0).total_seconds() / 3600
        if hours < MIN_BURN_WINDOW_H:
            return None
        sample = (float(prev) - float(now_)) / hours
        was = old.get("burn_per_h")
        if was is None:
            return sample
        return BURN_SMOOTHING * sample + (1 - BURN_SMOOTHING) * float(was)
    except Exception:  # noqa: BLE001
        return None


# Manual (/keys) and scheduled sweeps share one lock so two sweeps never
# interleave their read-modify-write of the same provider row.
_sweep_lock = asyncio.Lock()


async def refresh(timeout: float = 30.0) -> str | None:
    """On-demand sweep for /keys: the same fetch → store → alert path as the
    scheduled poll, so a threshold crossing seen here fires exactly as it would
    have fifteen minutes later. Returns None on success, else a short reason;
    the stored readings are left as they were on failure."""
    try:
        async with _sweep_lock:
            await asyncio.wait_for(tick(), timeout=timeout)
        return None
    except asyncio.TimeoutError:
        return f"timed out after {int(timeout)}s"
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001
        log.warning("credits refresh failed: %s", exc)
        return str(exc)[:80] or exc.__class__.__name__


async def tick() -> list[dict]:
    """One sweep. Returns the events that were recorded (and delivered)."""
    readings = await providers.read_all()
    events: list[dict] = []

    for name, r in readings.items():
        try:
            await _one(name, r, events)
        except Exception:  # noqa: BLE001
            # A bug in one provider's branch used to abort the whole sweep
            # before anything was saved, silently freezing EVERY balance --
            # three of them sat 3 hours stale behind a single UnboundLocalError.
            log.exception("credits: %s failed this sweep", name)

    events = [e for e in events if e]
    await _deliver(events)
    return events


async def _one(name: str, r: dict, events: list) -> None:
    old = await last(name)

    # A non-auth failure is a bad five minutes, not news. Keep the previous
    # reading so the balance does not blink to "unknown" and back.
    if not r.get("ok") and not r.get("dead"):
        await save(name, old or {}, error=f"HTTP {r.get('status')}")
        return

    if r.get("dead"):
        if old.get("dead"):          # already told them
            await save(name, {**old, "dead": True}, error="unauthorized")
            return
        ev = dict(kind="key_dead", severity="critical", icon="🔑",
                  title=f"{name} rejected our API key",
                  body=f"HTTP {r.get('status')}\n"
                       f"<i>every run using this key fails until it is "
                       f"replaced</i>")
        events.append(await _emit(name, ev))
        await save(name, {**old, "dead": True}, error="unauthorized")
        return

    # A rising balance means a TOP-UP, and burn cannot be inferred across a
    # credit event. Carrying the old figure forward is worse than having
    # none: OpenRouter's $33.59/h was measured while it drained to zero, and
    # against a freshly topped-up $25.80 that renders as "46m left" — a
    # confident, wrong number. Drop it and re-measure from clean readings.
    topped_up = (old.get("remaining") is not None
                 and r.get("remaining") is not None
                 and float(r["remaining"]) > float(old["remaining"]))
    # A provider that can tell us the exact rate (Lium: the sum of its
    # running pods' $/h) has already set burn_per_h. Never overwrite that
    # with a sampled guess -- the exact figure is right the instant a pod
    # starts, where sampling needs two polls to notice.
    if r.get("burn_per_h") is None:
        burn = None if topped_up else (_burn(old, r) if old else None)
        if burn is not None:
            r["burn_per_h"] = burn
        elif not topped_up and old.get("burn_per_h") is not None:
            r["burn_per_h"] = old["burn_per_h"]

    if old.get("dead"):
        events.append(await _emit(name, dict(
            kind="key_back", severity="good", icon="✅",
            title=f"{name} key works again",
            body=money(r.get("remaining"), r.get("unit", "$")))))

    # ---- pods ----------------------------------------------------
    # Only alert on a pod we have SEEN before, so the first sweep after a
    # restart does not announce every pod that has been running for days.
    was_pods, now_pods = old.get("pods") or {}, r.get("pods") or {}
    # Gate on the KEY existing, not on the row existing. The row had been
    # stored for hours before pods were ever fetched, so `if old:` was true
    # while `was_pods` was empty -- and a pod that had been up for 65 hours
    # announced itself as "pod started". First sight of a FIELD is a
    # baseline too, exactly like first sight of a row.
    #
    # `in` rather than truthiness: an empty dict is a real answer ("no pods
    # right now"), and gating on truthiness would swallow the genuine
    # first-pod-started alert forever.
    if "pods" in old:
        for pid, pod in now_pods.items():
            prev = was_pods.get(pid)
            who = f"{pod.get('name') or pid} ({pod.get('gpus')}× {pod.get('gpu')})"
            if prev is None:
                events.append(await _emit(name, dict(
                    kind="pod", severity="good", icon="🖥",
                    title=f"pod started — {pod.get('name') or pid}",
                    body=f"{who}\n{money(pod.get('price'))}/h"
                         + (f" · {runway(r.get('remaining'), r.get('burn_per_h'))}"
                            if runway(r.get("remaining"), r.get("burn_per_h")) else ""))))
                continue
            if prev.get("status") != pod.get("status"):
                up = pod.get("status") == "RUNNING"
                events.append(await _emit(name, dict(
                    kind="pod", severity="good" if up else "warn",
                    icon="🖥" if up else "🛑",
                    title=f"pod {pod.get('name') or pid} → {pod.get('status')}",
                    body=f"{who}\nwas {prev.get('status')}")))
            # Lium reclaims pods rather than letting a balance go negative.
            if not prev.get("removal_at") and pod.get("removal_at"):
                events.append(await _emit(name, dict(
                    kind="pod", severity="critical", icon="⏳",
                    title=f"pod {pod.get('name') or pid} scheduled for REMOVAL",
                    body=f"{who}\nat <code>{pod['removal_at']}</code>\n"
                         f"<i>top up now or lose the pod</i>")))
        for pid, prev in was_pods.items():
            if pid not in now_pods:
                events.append(await _emit(name, dict(
                    kind="pod", severity="warn", icon="🛑",
                    title=f"pod {prev.get('name') or pid} is gone",
                    body=f"was {prev.get('status')} at "
                         f"{money(prev.get('price'))}/h")))

    # The only balance event: falling into a tighter bucket.
    if old:
        was = bucket(old.get("remaining"), name)
        now_ = bucket(r.get("remaining"), name)
        if now_ is not None and (was is None or now_ < was):
            left = runway(r.get("remaining"), r.get("burn_per_h"))
            events.append(await _emit(name, dict(
                kind="credit_low", severity=severity_for(now_), icon="💳",
                title=f"{name} below {money(now_, r.get('unit', '$'))}",
                body=f"{money(r.get('remaining'), r.get('unit', '$'))} left"
                     + (f" · <b>{left}</b>" if left else "")
                     + (f"\nburn {money(r.get('burn_per_h'))}/h"
                        if r.get("burn_per_h") else "")
                     + "\n<i>top up before the next run</i>")))
    await save(name, r)


async def _emit(provider: str, ev: dict) -> dict | None:
    """Record through the competition event table so cooldown/dedup is shared."""
    from ..comp.base import CompEvent
    return await comp_store.record(CREDIT_NETUID, CompEvent(
        kind=ev["kind"], severity=ev["severity"], icon=ev.get("icon", ""),
        title=ev["title"], body=ev.get("body", ""),
        dedup_key=f"{provider}:{ev['title']}"))


async def _deliver(events: list[dict]) -> None:
    """Credit alerts are infrastructure, not a subnet: they belong in the
    general/digest topic, falling back to any linked chat."""
    if not events:
        return
    dests = await comp_store.digest_targets()
    if not dests:
        rows = await pool().fetch("SELECT DISTINCT chat_id FROM telegram_link")
        dests = [(r["chat_id"], 0) for r in rows]
    for ev in events:
        text = router.format_event(ev)
        for chat_id, thread_id in dests:
            _, loud = SEVERITY.get(ev["severity"], ("•", False))
            await router.send_to(chat_id, thread_id, text, silent=not loud)
            await asyncio.sleep(1.2)
        await comp_store.mark_delivered(ev["id"])


async def loop() -> None:
    await asyncio.sleep(8)
    while True:
        try:
            async with _sweep_lock:
                n = await tick()
            hub.status["credits"] = f"ok ({len(n)} events)"
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            log.warning("credits poll failed: %s", exc)
            hub.status["credits"] = f"failing: {exc}"
        await asyncio.sleep(settings.credits_poll)


def start() -> list[asyncio.Task]:
    # Report what is ACTUALLY polled -- the enabled list intersected with the
    # keys we hold. Logging the whole registry claimed five providers while
    # three were being read.
    live = [n for n in providers.enabled()
            if getattr(settings, f"{n}_api_key", "")
            or getattr(settings, f"{n}_api_token", "")]
    off = [n for n in providers.PROVIDERS if n not in providers.enabled()]
    log.info("credit monitoring: %s%s",
             ", ".join(live) or "no keys configured",
             f"  (off: {', '.join(off)})" if off else "")
    return [asyncio.create_task(loop())]
