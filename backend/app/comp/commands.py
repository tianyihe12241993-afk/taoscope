"""Topic-aware Telegram commands.

The whole point of the forum layout: an incoming message carries
`message_thread_id`, and that thread is bound to a subnet, so `/state` typed in
the SN100 topic is already an SN100 command. No arguments, no `@botname`
suffixes, no second bot -- and the same command in the SN85 topic answers about
SN85 because the adapter behind that thread is different.

Every command still accepts an explicit netuid (`/state 100`) so it works in a
DM or an unbound topic.
"""
from __future__ import annotations

import asyncio
import logging

from . import poller, router, store, topics
from .adapters import all_adapters, ensure_chain, get as get_adapter, refresh
from .base import (GENERIC_ALERTS, SEVERITY, chain_block, esc, ours_chain_block,
                   ours_line)

log = logging.getLogger("taoscope.comp.commands")

HELP = (
    "<b>Competition tracking</b>\n\n"
    "<b>In a subnet topic these need no arguments.</b>\n"
    "/state — crown, field, queue, our runs\n"
    "/info — rules, caps, registration cost, repos\n"
    "/board [n] — leaderboard\n"
    "/mine — our submissions in detail\n"
    "/guide — pin this subnet's primer (what the alerts mean)\n"
    "/events — what changed recently\n"
    "/watch <code>id [label]</code> · /unwatch <code>id</code>\n"
    "/poll — force a refresh now\n"
    "/mute <code>kind</code> · /unmute <code>kind</code>\n"
    "/pick — narrow this topic to the rounds you care about\n\n"
    "<b>Setup</b> — topics for subnets our hotkeys are on are created "
    "automatically\n"
    "/setup — create + bind a topic per tracked subnet\n"
    "/bind <code>100</code> — bind THIS topic to a subnet (any netuid)\n"
    "/bind <code>digest</code> — make this the cross-subnet topic\n"
    "/unbind — stop routing here (and stop auto-creating it)\n"
    "/topics — every binding, and held subnets with no topic"
)

COMMANDS = {"state", "info", "board", "mine", "watch", "unwatch", "poll", "guide",
            "bind", "unbind", "topics", "setup", "mute", "unmute", "comphelp",
            "pick"}


async def _resolve(chat_id: int, thread_id: int, arg: str):
    """(adapter, remaining_arg, bound). Explicit netuid wins over the binding."""
    parts = arg.split()
    if parts and parts[0].isdigit():
        return get_adapter(int(parts[0])), " ".join(parts[1:]), True
    netuid, bound = await store.netuid_for_topic(chat_id, thread_id)
    if netuid is None:
        return None, arg, bound
    return get_adapter(netuid), arg, bound


async def _reply(chat_id: int, thread_id: int, text: str):
    return await router.send_to(chat_id, thread_id, text, silent=True)


async def _need_subnet(chat_id: int, thread_id: int, bound: bool):
    if bound:
        msg = ("This topic is the cross-subnet digest. Add a netuid: "
               "<code>/state 100</code>")
    else:
        known = ", ".join(f"SN{a.netuid}" for a in all_adapters())
        msg = (f"This topic isn't bound to a subnet.\n"
               f"<code>/bind 100</code> here, or <code>/setup</code> in General to "
               f"create them all.\nTracked: {known}")
    await _reply(chat_id, thread_id, msg)


async def handle(chat_id: int, thread_id: int, cmd: str, arg: str,
                 chat_type: str) -> bool:
    """Returns True if this command belongs to the competition layer."""
    if cmd not in COMMANDS:
        return False

    if cmd == "comphelp":
        await _reply(chat_id, thread_id, HELP)
        return True

    # ---- setup / routing ----
    if cmd == "bind":
        a = arg.strip().lower()
        if a in ("digest", "general", "all", "*"):
            await store.bind_topic(chat_id, thread_id, None, "digest")
            await _reply(chat_id, thread_id,
                         "✅ This topic is now the cross-subnet digest. "
                         "<code>/state</code> here summarises every subnet.")
            return True
        if not a.isdigit():
            known = "\n".join(f"<code>/bind {x.netuid}</code> — {esc(x.label)}"
                              for x in all_adapters())
            await _reply(chat_id, thread_id, f"Usage:\n{known}\n"
                                             "<code>/bind digest</code>")
            return True
        netuid = int(a)
        ad = get_adapter(netuid)
        if ad is None:
            # Any subnet on chain can have a topic: without an adapter it gets
            # the chain view, the same as every subnet we hold a UID on.
            name = await store.subnet_name(netuid)
            if name is None:
                await _reply(chat_id, thread_id, f"SN{netuid} does not exist on chain.")
                return True
            ad = ensure_chain(netuid, name)
        await store.unskip_topic(chat_id, netuid)
        # `/bind <netuid>` in GENERAL is almost never what someone means.
        #
        # General is thread 0, not a real forum topic. Binding it silently did
        # three bad things at once: the subnet's alerts went to General instead
        # of a topic of their own, whatever General was bound to before was
        # OVERWRITTEN (bind_topic upserts on (chat_id, thread_id), and General
        # is the conventional home of the digest), and `/setup` then SKIPPED
        # that subnet forever because it counted as already bound -- so no
        # command the operator would naturally reach for could undo it.
        #
        # What they meant is "give this subnet a topic". Do that.
        if not thread_id and chat_type == "supergroup":
            tid = await topics.create_topic(chat_id, ad)
            if tid:
                # General must not keep pointing at this subnet as well, or
                # every alert is delivered twice. It was the digest before a
                # mis-bind put a subnet here, so put the digest back.
                cur, bound = await store.netuid_for_topic(chat_id, 0)
                restored = ""
                if bound and cur == netuid:
                    await store.bind_topic(chat_id, 0, None, "digest")
                    restored = "\nGeneral is the cross-subnet digest again."
                await _reply(chat_id, thread_id,
                             f"✅ Created <b>SN{netuid} · {esc(ad.label)}</b> and "
                             f"bound it there.{restored}\n"
                             f"<i>/bind binds the topic you type it in, and "
                             f"General is not one — so it made the topic for "
                             f"you.</i>")
                return True
            await _reply(chat_id, thread_id,
                         f"Could not create a topic for <b>SN{netuid}</b> — the "
                         f"bot needs admin with <b>Manage Topics</b>.\n"
                         f"Make the topic by hand and run "
                         f"<code>/bind {netuid}</code> <i>inside it</i>. "
                         f"Binding General would send SN{netuid}'s alerts here "
                         f"and replace the digest, so nothing was changed.")
            return True

        await store.bind_topic(chat_id, thread_id, netuid, ad.label)
        await _reply(chat_id, thread_id,
                     f"✅ Bound to <b>SN{netuid} {esc(ad.label)}</b>.\n"
                     f"Alerts land here. Try <code>/state</code>.")
        return True

    if cmd == "unbind":
        netuid, bound = await store.netuid_for_topic(chat_id, thread_id)
        await store.unbind_topic(chat_id, thread_id)
        if netuid is not None:
            # Remember it, or the automatic sync makes a new topic for this
            # subnet five minutes later.
            await store.skip_topic(chat_id, netuid, "unbind")
            await _reply(chat_id, thread_id,
                         f"Unbound. No SN{netuid} alerts will be routed here, and "
                         f"no topic will be created for it automatically.\n"
                         f"<code>/bind {netuid}</code> to undo.")
        else:
            await _reply(chat_id, thread_id, "Unbound. No alerts will be routed here.")
        return True

    if cmd == "topics":
        # This chat only: other chats' thread ids mean nothing here.
        rows = [r for r in await store.all_topics() if r["chat_id"] == chat_id]
        held = await store.held_subnets()
        if not rows:
            await _reply(chat_id, thread_id, "Nothing bound yet. <code>/setup</code>")
            return True
        L = ["<b>Topic bindings</b>", ""]
        for r in sorted(rows, key=lambda r: (r["netuid"] is not None, r["netuid"] or 0)):
            what = f"SN{r['netuid']}" if r["netuid"] is not None else "digest"
            key = "🔑 " if r["netuid"] in held else ""
            muted = [k for k, v in (r["prefs"] or {}).items() if v is False]
            L.append(f"{key}<b>{what}</b> {esc(r['title'])} · thread "
                     f"<code>{r['thread_id']}</code>"
                     + (f"  muted: {esc(', '.join(muted))}" if muted else ""))
        bound = {r["netuid"] for r in rows}
        skipped = await store.skipped_topics(chat_id)
        gap = [n for n in sorted(held) if n not in bound]
        if gap:
            L += ["", "<b>Our hotkeys are on these, with no topic</b>"]
            L += [f"SN{n} {esc(held[n])}"
                  + (" — <i>skipped (unbound or deleted)</i>" if n in skipped else "")
                  for n in gap]
        L += ["", "<i>🔑 = our hotkeys hold a UID there</i>"]
        await _reply(chat_id, thread_id, "\n".join(L))
        return True

    if cmd == "setup":
        await _setup(chat_id, thread_id, chat_type)
        return True

    # ---- everything below needs a subnet ----
    ad, rest, bound = await _resolve(chat_id, thread_id, arg)
    if ad is None:
        # /state without a subnet is still answerable -- summarise everything --
        # but an UNBOUND topic must also say so, or the operator never learns
        # why alerts are not arriving here.
        if cmd == "state":
            await _digest(chat_id, thread_id, bound=bound)
            return True
        await _need_subnet(chat_id, thread_id, bound)
        return True

    if cmd == "watch":
        parts = rest.split(maxsplit=1)
        if not parts:
            await _reply(chat_id, thread_id,
                         "Usage: <code>/watch &lt;submission_id&gt; [label]</code>")
            return True
        await store.add_watch(ad.netuid, parts[0],
                              parts[1] if len(parts) > 1 else "")
        await _reply(chat_id, thread_id,
                     f"👁 Watching <code>{esc(parts[0][:12])}</code> on SN{ad.netuid}. "
                     f"Stage changes will be reported here.")
        return True

    if cmd == "unwatch":
        if not rest.strip():
            await _reply(chat_id, thread_id, "Usage: <code>/unwatch &lt;id&gt;</code>")
            return True
        await store.drop_watch(ad.netuid, rest.strip())
        await _reply(chat_id, thread_id, f"Stopped watching <code>{esc(rest.strip())}</code>.")
        return True

    if cmd in ("mute", "unmute"):
        kind = rest.strip()
        if not kind:
            kinds = sorted(set(ad.alerts) | set(GENERIC_ALERTS)
                           | set(await store.seen_kinds(ad.netuid)))
            await _reply(chat_id, thread_id,
                         f"<b>SN{ad.netuid} alert kinds</b>\n"
                         + ", ".join(f"<code>{esc(k)}</code>" for k in kinds)
                         + "\n\n<code>/mute kind</code> in this topic")
            return True
        await store.set_pref(chat_id, thread_id, kind, cmd == "unmute")
        await _reply(chat_id, thread_id,
                     f"{'🔔' if cmd == 'unmute' else '🔕'} <code>{esc(kind)}</code> "
                     f"{'on' if cmd == 'unmute' else 'muted'} for this topic.")
        return True

    if cmd == "pick":
        opts = ad.pick_options(await store.last_state(ad.netuid))
        if not opts:
            await _reply(chat_id, thread_id,
                         f"SN{ad.netuid} has nothing to pick between.")
            return True
        cur = set(await store.get_pick(chat_id, thread_id))
        a = rest.strip().lower()

        if a in ("all", "clear", "reset", "*"):
            await store.set_pick(chat_id, thread_id, [])
            await _reply(chat_id, thread_id,
                         "✅ Showing <b>all</b> of them again.")
            return True
        if a == "mine":
            # "the ones I am in" means the ones still worth acting on: a
            # finished round we entered is a result, not a thing to track.
            keys = [o["key"] for o in opts
                    if o.get("mine") and o.get("active", True)]
            if not keys:
                keys = [o["key"] for o in opts if o.get("mine")]
            if not keys:
                await _reply(chat_id, thread_id,
                             "None of these have an entry from our hotkeys yet.")
                return True
            await store.set_pick(chat_id, thread_id, keys)
            await _reply(chat_id, thread_id,
                         "✅ Narrowed to the "
                         f"{len(keys)} we are competing in.\n"
                         "<code>/mine</code> · <code>/state</code> · "
                         "<code>/board</code> now show only those.")
            return True

        wanted = [w for w in a.replace(",", " ").split() if w]
        if wanted:
            keys = []
            bad = []
            for w in wanted:
                if w.isdigit() and 1 <= int(w) <= len(opts):
                    keys.append(opts[int(w) - 1]["key"])
                else:
                    hit = [o for o in opts if w in o["label"].lower()
                           or w == o["key"].lower()]
                    if hit:
                        keys.append(hit[0]["key"])
                    else:
                        bad.append(w)
            if bad:
                await _reply(chat_id, thread_id,
                             "Don't know: " + ", ".join(f"<code>{esc(b)}</code>"
                                                        for b in bad))
                return True
            await store.set_pick(chat_id, thread_id, sorted(set(keys)))
            await _reply(chat_id, thread_id,
                         f"✅ Narrowed to {len(set(keys))}. "
                         "<code>/pick all</code> to undo.")
            return True

        # No argument: show the menu and what is selected right now.
        L = [f"<b>SN{ad.netuid} — pick what this topic shows</b>", ""]
        for i, o in enumerate(opts, 1):
            on = "☑" if (not cur or o["key"] in cur) else "☐"
            star = " ⭐" if o.get("mine") else ""
            L.append(f"{on} <b>{i}</b>. {esc(o['label'])}{star}"
                     + (f" — <i>{esc(o['note'])}</i>" if o.get("note") else ""))
        L += ["", "⭐ = we have an entry in it", "",
              "<code>/pick 1 3</code> — just those",
              "<code>/pick mine</code> — the ones we are competing in",
              "<code>/pick all</code> — everything (default)"]
        if cur:
            L.append(f"\n<i>currently narrowed to {len(cur)}</i>")
        await _reply(chat_id, thread_id, "\n".join(L))
        return True

    if cmd == "poll":
        await _reply(chat_id, thread_id, f"Polling SN{ad.netuid}…")
        try:
            n = await poller.tick(ad, check_repos=True)
        except Exception as exc:  # noqa: BLE001
            await _reply(chat_id, thread_id, f"Poll failed: <code>{esc(exc)}</code>")
            return True
        s = await store.last_state(ad.netuid)
        await _reply(chat_id, thread_id,
                     f"Done — {n} new event(s).\n\n{ad.render_state(s)}")
        return True

    s = await store.last_state(ad.netuid)
    if not s:
        await _reply(chat_id, thread_id,
                     f"No snapshot for SN{ad.netuid} yet — the first poll is pending. "
                     f"<code>/poll</code> to force it.")
        return True

    # This topic's /pick selection, injected the same way the poller injects
    # `_chain` and `_repos`: view data the renderers may honour, so no renderer
    # signature has to grow a parameter and no other adapter is affected.
    s["_pick"] = await store.get_pick(chat_id, thread_id)

    if cmd == "state":
        # chain_block is shared: every subnet reports operators/uids/entry cost
        # the same way, and no adapter has to remember to render it.
        await _reply(chat_id, thread_id, ad.render_state(s) + chain_block(s))
    elif cmd == "info":
        await _reply(chat_id, thread_id, ad.render_info(s))
    elif cmd == "board":
        limit = int(rest.strip()) if rest.strip().isdigit() else 10
        await _reply(chat_id, thread_id, ad.render_board(s, min(limit, 20)))
    elif cmd == "mine":
        # Every topic ends /mine with the same table of our hotkeys on chain.
        # A chain-only adapter's view already IS that table.
        body = ad.render_me(s)
        extra = "" if "OUR HOTKEYS" in body else ours_chain_block(s)
        if extra and len(body) + len(extra) < 3900:
            await _reply(chat_id, thread_id, body + "\n\n" + extra)
        else:
            await _reply(chat_id, thread_id, body)
            if extra:
                await _reply(chat_id, thread_id, extra)
    elif cmd == "guide":
        # Posted silently and pinned: a topic's primer should be reachable from
        # the pin, not by scrolling past a week of alerts. Pinning is
        # best-effort -- it needs admin rights, and failing to pin must never
        # cost the message itself.
        from .router import send_to
        from ..telegram.bot import call
        sent = await send_to(chat_id, thread_id, ad.render_guide(s), silent=True)
        mid = (sent or {}).get("result", {}).get("message_id") or (sent or {}).get("message_id")
        if mid:
            await call("pinChatMessage", chat_id=chat_id, message_id=mid,
                       disable_notification=True)
    return True


async def tracked_summary() -> str:
    """What this chat watches, for a welcome message."""
    lines = []
    for ad in all_adapters():
        s = await store.last_state(ad.netuid)
        lines.append(f"• <b>SN{ad.netuid}</b> {esc(ad.label)}"
                     + (f" — {s['n_board']} on the board" if s.get("n_board") else ""))
    return "\n".join(lines) or "• nothing yet"


async def events_for_topic(chat_id: int, thread_id: int) -> str | None:
    """Competition events for a bound topic, or None to fall through to the
    chain-wide /events the bot already had."""
    netuid, bound = await store.netuid_for_topic(chat_id, thread_id)
    if not bound:
        return None
    rows = await store.recent_events(netuid, 15)
    if not rows:
        return "Nothing has changed since tracking started."
    head = f"SN{netuid}" if netuid is not None else "all subnets"
    L = [f"<b>Recent events — {head}</b>", ""]
    for r in rows:
        icon, _ = SEVERITY.get(r["severity"], ("•", False))
        L.append(f"{icon} <code>{r['ts']:%m-%d %H:%M}</code> {esc(r['title'])}")
    return "\n".join(L)


async def _digest(chat_id: int, thread_id: int, *, bound: bool = True) -> None:
    # Overview board: subnets where we have something at stake first, then the
    # rest. A flat alphabetical list buries the one line that needed reading.
    live, idle, soonest = [], [], None
    for ad in all_adapters():
        s = await store.last_state(ad.netuid)
        if not s:
            idle.append(f"SN{ad.netuid} {esc(ad.label)} — no data yet")
            continue
        line = ad.render_digest(s)
        try:
            ours = ad.ours_summary(s)
        except Exception:                     # a renderer must never break /state
            ours = ""
        # An adapter with nothing on its watchlist still has something at stake
        # if our hotkeys hold UIDs there -- say so the same way for every subnet.
        ours = ours or ours_line(s)
        try:
            dl = ad.deadline_minutes(s)
        except Exception:
            dl = None
        if isinstance(dl, (int, float)) and (soonest is None or dl < soonest[1]):
            soonest = (ad, dl)
        if ours:
            live.append(f"{line}\n   ▸ <b>{esc(ours)}</b>")
        else:
            idle.append(line)

    L = ["<b>All tracked competitions</b>", ""]
    if live:
        L += [f"<b>🎯 OURS ({len(live)})</b>", ""] + live + [""]
    if idle:
        if live:
            L.append("<b>· tracked ·</b>")
        L += idle
    if soonest is not None:
        ad, dl = soonest
        m = int(dl)
        hm = f"{m//60}h{m%60:02d}m" if m >= 60 else f"{m}m"
        L += ["", f"⏳ next cutoff <b>{hm}</b> — SN{ad.netuid} {esc(ad.label)}"]
    L.append("")
    if bound:
        L.append("<i>Open a subnet topic and type /state for the full picture.</i>")
    else:
        L.append("⚠️ <i>This topic isn't bound to a subnet, so no alerts are routed "
                 "here.</i>\n<code>/bind 100</code> here, or <code>/setup</code> in "
                 "General to create a topic per subnet.")
    await _reply(chat_id, thread_id, "\n".join(L))


async def _setup(chat_id: int, thread_id: int, chat_type: str) -> None:
    """Create one forum topic per tracked subnet and bind it.

    Tracked = every hand-written adapter plus every subnet our coldkeys hold a
    UID on. Running /setup is an explicit request for all of them, so it also
    clears the per-chat opt-outs left by /unbind or a deleted topic.

    Needs the bot to be an administrator with can_manage_topics. If topic
    creation is refused we still bind whatever exists, so a manual layout keeps
    working -- /bind in each topic is always the fallback.
    """
    if chat_type != "supergroup":
        await _reply(chat_id, thread_id,
                     "Run <code>/setup</code> inside the forum supergroup.")
        return

    await store.bind_topic(chat_id, thread_id, None, "digest")
    await store.unskip_topic(chat_id)
    await refresh()
    wanted = topics.tracked_for_setup(await store.held_subnets())
    made, failed = [], []
    for ad in await topics.missing(chat_id, wanted, honour_skips=False):
        if await topics.create_topic(chat_id, ad):
            made.append(ad.netuid)
        else:
            failed.append(ad.netuid)
        await asyncio.sleep(topics.CREATE_GAP_S)

    msg = "✅ Setup done. This topic is the digest."
    if made:
        msg += "\nCreated: " + ", ".join(f"SN{n}" for n in made)
    if failed:
        msg += ("\n⚠️ Could not create: " + ", ".join(f"SN{n}" for n in failed) +
                " — make the bot an admin with <b>Manage Topics</b>, or create the "
                "topic by hand and run <code>/bind &lt;netuid&gt;</code> inside it.")
    await _reply(chat_id, thread_id, msg)
