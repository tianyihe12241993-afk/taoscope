"""The competition poll loop -- one independent task per subnet.

Per subnet, per tick:

    seed watchlist -> snapshot -> merge chain facts -> check repos
                   -> diff vs the LAST STORED state -> record -> deliver

Diffing against the stored state rather than an in-memory one is what makes a
restart safe: the process can die mid-competition and come back without
re-announcing a king change from three hours ago.

Each subnet gets its own task so a slow or broken dashboard on one subnet
cannot stall the others -- the failure mode we actually care about, because
these dashboards go down more often than the chain does.
"""
from __future__ import annotations

import asyncio
import logging
import time

from ..config import settings
from ..db import pool
from ..hub import hub
from . import adapters as registry
from . import github, router, store, topics
from .adapters import all_adapters
from .base import CompEvent, SubnetAdapter, num

log = logging.getLogger("taoscope.comp.poller")

REPO_POLL_S = 600
BACKOFF_MAX_S = 900
# How often the supervisor looks for subnets we newly hold a UID on. The neuron
# sweep that would reveal one runs every 15 minutes.
SUPERVISE_S = 300


async def _chain_facts(netuid: int) -> dict:
    """Registration cost, operator count and emission -- straight from our own
    chain tables, so every adapter gets them without writing any code."""
    row = await pool().fetchrow(
        "SELECT name, price, emission_share, realized_tao_per_hour, num_uids,"
        "       max_uids, unique_coldkeys, earning_coldkeys, burn_tao,"
        "       registration_allowed, tempo"
        " FROM subnet_live WHERE netuid=$1", netuid)
    return dict(row) if row else {}


def _diff_chain(old: dict, new: dict) -> list[CompEvent]:
    """Generic chain-side events every subnet gets for free."""
    o, n = old.get("_chain") or {}, new.get("_chain") or {}
    if not o or not n:
        return []
    out = []
    if o.get("registration_allowed") != n.get("registration_allowed"):
        opened = bool(n.get("registration_allowed"))
        out.append(CompEvent(
            kind="registration", severity="warn" if opened else "info",
            icon="🚪" if opened else "🔒",
            title="Registration opened" if opened else "Registration closed",
            body=(f"entry τ{float(n.get('burn_tao') or 0):.4f}\n"
                  f"{n.get('num_uids')}/{n.get('max_uids')} uids · "
                  f"{n.get('unique_coldkeys')} operators")
                 + ("\n<i>A slot can be registered now.</i>" if opened else ""),
            dedup_key=str(opened),
        ))

    # Operator growth is deliberately NOT alerted (removed 2026-09-24 at the
    # user's request). A registration count ticking 164 -> 166 asks nothing of
    # anyone: it fired on every subnet, several times a day, and nothing was
    # done differently because of it. The count is still on the /sn panel for
    # whoever wants to look. Do not re-add it as a push alert.
    return out


def _diff_ours_chain(old: dict, new: dict) -> list[CompEvent]:
    """Our UIDs on this subnet stopped earning altogether, or started.

    Subnet-level on purpose: per-UID flips on a 27-UID subnet would be a wall of
    messages that each say nothing. "Everything we hold here earns zero" is the
    line that calls for a look. Deregistration is not reported here -- the chain
    sweep already emits it, and it is routed to this topic."""
    o, n = old.get("_ours_chain"), new.get("_ours_chain")
    if o is None or n is None or not n.get("n"):
        return []
    was, now = int(o.get("earning") or 0), int(n.get("earning") or 0)
    if was > 0 and now == 0:
        return [CompEvent(
            kind="our_earning", severity="warn", icon="📉",
            title="Our UIDs here stopped earning",
            body=(f"{was} → <b>0</b> earning of {n['n']} uids · "
                  f"was τ{num(o.get('tpd'), 2)}/day\n"
                  f"<i>/mine for every hotkey</i>"),
            dedup_key="stopped", cooldown_h=6.0)]
    if was == 0 and now > 0:
        return [CompEvent(
            kind="our_earning", severity="good", icon="💰",
            title=f"Earning here — {now} of {n['n']} uids",
            body=f"τ{num(n.get('tpd'), 2)}/day\n<i>/mine for every hotkey</i>",
            dedup_key="started", cooldown_h=6.0)]
    return []


async def _sync_watchlist(ad: SubnetAdapter) -> None:
    """Refresh the watchlist from whatever the miner tooling wrote.

    ISOLATED, because this is the one step in a tick that depends on a file
    another program owns. On 2026-08-21 SN100's watchfile changed shape and the
    resulting AttributeError -- raised here, before snapshot() -- took the whole
    subnet's monitoring down for 3.4 days: no board, no crown, no repo watch,
    for a broken auxiliary feature.

    A watchlist we cannot read costs us the "our runs" view. It must not cost us
    the subnet.
    """
    try:
        rows = await ad.seed_watchlist()
    except Exception:  # noqa: BLE001
        log.exception("SN%s seed_watchlist failed — watchlist not refreshed "
                      "this tick; everything else continues", ad.netuid)
        return
    for row in rows:
        if not isinstance(row, dict) or not row.get("ref"):
            log.warning("SN%s seed_watchlist returned a row with no ref: %.80r",
                        ad.netuid, row)
            continue
        await store.add_watch(ad.netuid, row["ref"], row.get("label", ""),
                              row.get("uid"))


async def tick(ad: SubnetAdapter, *, check_repos: bool) -> int:
    """One poll. Returns the number of events delivered."""
    await _sync_watchlist(ad)

    new = await ad.snapshot()
    if not new:
        raise RuntimeError("empty snapshot")

    chain = await _chain_facts(ad.netuid)
    if chain:
        # Decimal is not JSON-serialisable, but blanket float() turned counts
        # into "uids 256.0/256.0". Only non-integers become floats.
        def _plain(v):
            if isinstance(v, bool) or isinstance(v, int) or v is None:
                return v
            try:
                f = float(v)
            except (TypeError, ValueError):
                return v
            return int(f) if f.is_integer() and abs(f) < 1e15 else f
        new["_chain"] = {k: _plain(v) for k, v in chain.items()}

    # Our UIDs, merged into EVERY subnet's snapshot the same way, so /state,
    # /mine and the digest read them identically whatever the adapter. A failed
    # read omits the key: absence is not a change.
    try:
        new["_ours_chain"] = await store.ours_on_chain(ad.netuid)
    except Exception:  # noqa: BLE001
        log.exception("SN%s: reading our UIDs failed", ad.netuid)

    old = await store.last_state(ad.netuid)

    events: list[CompEvent] = []
    try:
        events += ad.diff(old, new)
    except Exception:  # noqa: BLE001
        log.exception("SN%s diff failed", ad.netuid)
    events += _diff_chain(old, new)
    events += _diff_ours_chain(old, new)
    if check_repos and ad.repos:
        events += await github.check_all(ad.netuid, ad.repos)

    new["_repos"] = [
        {"repo": r["repo"], "sha": r["sha"], "subject": r["subject"]}
        for r in await store.repos_for(ad.netuid)
    ]

    recorded = []
    for ev in events:
        row = await store.record(ad.netuid, ev)
        if row:
            recorded.append(row)

    # `_`-prefixed keys are transient view data; save_state drops them, so the
    # comparison that matters is over the adapter's own fields.
    changed_any = any(old.get(k) != v for k, v in new.items() if not k.startswith("_"))
    await store.save_state(ad.netuid, new, changed=changed_any)

    await router.deliver(ad.netuid, recorded)
    return len(recorded)


async def loop_for(ad: SubnetAdapter) -> None:
    await store.register_subnet(ad.netuid, ad.slug, ad.label)
    last_repo = 0.0
    fails = 0
    # Stagger so several adapters do not all hit their dashboards on the same
    # second after a restart.
    await asyncio.sleep(3 + (ad.netuid % 7))
    while True:
        try:
            due = time.time() - last_repo > REPO_POLL_S
            n = await tick(ad, check_repos=due)
            if due:
                last_repo = time.time()
            fails = 0
            hub.status[f"comp_sn{ad.netuid}"] = f"ok ({n} events)"
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            fails += 1
            # WITH the traceback. "SN100 poll failed (378): 'str' object has no
            # attribute 'get'" names neither the file, the function nor the
            # line, and a one-liner that says only that is how a 3.4-day outage
            # went unnoticed. This module already argues the same point about
            # anonymous Telegram errors -- it applies here first.
            log.exception("SN%s poll failed (%d): %s", ad.netuid, fails, exc)
            hub.status[f"comp_sn{ad.netuid}"] = f"failing: {exc}"
            try:
                await store.save_state(ad.netuid, await store.last_state(ad.netuid),
                                       changed=False, error=str(exc)[:300])
            except Exception:  # noqa: BLE001
                pass
        # Back off on repeated failure so a dead dashboard is not hammered, but
        # never so far that a recovery goes unnoticed for long.
        delay = ad.poll_seconds if fails == 0 else min(
            ad.poll_seconds * (2 ** min(fails, 4)), BACKOFF_MAX_S)
        await asyncio.sleep(delay)


async def supervise(running: dict[int, asyncio.Task]) -> None:
    """Keep one poll loop per tracked subnet, and one topic per held subnet.

    Tracked grows at runtime: registering a hotkey on a new subnet adds a chain
    adapter on the next cycle, a loop for it, and (if enabled) its topic."""
    await asyncio.sleep(20)
    try:
        while True:
            try:
                new = await registry.refresh()
                if new:
                    log.info("now tracking on chain: %s",
                             ", ".join(f"SN{a.netuid}" for a in new))
                for ad in all_adapters():
                    if ad.netuid not in running or running[ad.netuid].done():
                        running[ad.netuid] = asyncio.create_task(loop_for(ad))
                if settings.telegram_bot_token and settings.telegram_auto_topics:
                    await topics.sync_all()
                hub.status["comp_supervisor"] = f"ok ({len(running)} subnets)"
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                log.exception("competition supervisor failed")
                hub.status["comp_supervisor"] = f"failing: {exc}"
            await asyncio.sleep(SUPERVISE_S)
    finally:
        for task in running.values():
            task.cancel()


def start() -> list[asyncio.Task]:
    ads = all_adapters()
    log.info("competition tracking: %s", ", ".join(f"SN{a.netuid}" for a in ads) or "none")
    running = {a.netuid: asyncio.create_task(loop_for(a)) for a in ads}
    return [*running.values(), asyncio.create_task(supervise(running))]
