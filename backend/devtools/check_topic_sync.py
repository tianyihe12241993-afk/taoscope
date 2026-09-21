"""Every subnet our hotkeys are on gets exactly one standard topic.

Covers, against the real database with Telegram stubbed (nothing is sent):

  1. sync creates "SN<n> · <label>" for each held subnet with no topic, binds
     it, posts the guide there and pins it -- and a second sync creates nothing
  2. /unbind in a subnet topic is remembered: the sync does not recreate it
  3. a deleted topic ("message thread not found") loses its binding once, is
     not recreated, and the message still reaches General
  4. /setup clears those opt-outs and fills every gap
  5. a refused creation is not retried every cycle
  6. chain events route to the subnet's topic, skip what the topic already
     reports, and fall back to General
  7. the chain-only view renders Telegram-legal HTML, and its generic earning
     alert is silent on a cold start and fires on a real flip

    docker compose run --rm -T --no-deps \\
      -v $PWD/backend/app:/srv/app:ro -v $PWD/backend/devtools:/srv/devtools:ro \\
      -v $PWD/backend/migrations:/srv/migrations:ro \\
      backend python devtools/check_topic_sync.py
"""
import asyncio
import html.parser
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import db, events as events_mod               # noqa: E402
from app.comp import adapters, commands, poller, router, store, topics  # noqa: E402
from app.config import settings                        # noqa: E402
from app.telegram import bot                           # noqa: E402

CHAT = -999_000_222          # no real group can collide with this
EXISTING_TID = 500

calls: list[tuple[str, dict]] = []
next_tid = [9000]
refuse_create = [False]
gone_threads: set[int] = set()


async def fake_call_ex(method, **params):
    calls.append((method, params))
    if method == "createForumTopic":
        if refuse_create[0]:
            return None, "Bad Request: not enough rights to create a topic"
        next_tid[0] += 1
        return {"message_thread_id": next_tid[0], "name": params["name"]}, ""
    if method == "sendMessage" and params.get("message_thread_id") in gone_threads:
        return None, "Bad Request: message thread not found"
    return {"message_id": len(calls)}, ""


async def fake_call(method, **params):
    return (await fake_call_ex(method, **params))[0]


ALLOWED = {"b", "i", "u", "s", "a", "code", "pre", "em", "strong", "blockquote"}


class Check(html.parser.HTMLParser):
    def __init__(self):
        super().__init__(); self.stack = []; self.bad = []

    def handle_starttag(self, tag, attrs):
        (self.stack.append(tag) if tag in ALLOWED else self.bad.append(tag))

    def handle_endtag(self, tag):
        if not self.stack or self.stack.pop() != tag:
            self.bad.append(f"/{tag}")


def legal(text: str) -> bool:
    """Parses as Telegram HTML. Length is the router's job (it splits)."""
    c = Check(); c.feed(text or "")
    return not c.bad and not c.stack


def made() -> list[str]:
    return [p["name"] for m, p in calls if m == "createForumTopic"]


def sent_to(tid: int) -> list[str]:
    return [p["text"] for m, p in calls if m == "sendMessage"
            and (p.get("message_thread_id") or 0) == tid]


async def reset():
    await db.pool().execute("DELETE FROM comp_topic WHERE chat_id=$1", CHAT)
    await db.pool().execute("DELETE FROM comp_topic_skip WHERE chat_id=$1", CHAT)
    topics._failed_at.clear()
    calls.clear()


async def main() -> int:
    # Belt and braces: every Telegram entry point is stubbed.
    settings.telegram_bot_token = "stub"
    bot.call_ex = fake_call_ex
    bot.call = fake_call
    topics.CREATE_GAP_S = 0

    await db.connect()
    await db.migrate()
    try:
        return await checks()
    finally:
        # Pass or fail, leave nothing behind: a fake chat bound to real subnets
        # makes the live poller deliver their alerts to a chat that does not
        # exist.
        await reset()
        await db.close()


async def checks() -> int:
    held = await store.held_subnets()
    assert len(held) >= 2, f"test needs at least two held subnets, have {held}"
    first, second, *_ = sorted(held)
    print(f"held subnets: {', '.join(f'SN{n}' for n in sorted(held))}")

    assert CHAT not in await store.forum_chats()
    print("a fake chat is never auto-synced             OK")

    # --- 1. sync fills the gaps, once ---------------------------------------
    await reset()
    await store.bind_topic(CHAT, 0, None, "digest")
    await store.bind_topic(CHAT, EXISTING_TID, first, "already here")
    new, failed = await topics.sync_chat(CHAT)
    want = sorted(n for n in held if n != first)
    assert sorted(new) == want and not failed, (new, want, failed)
    for n in want:
        ad = adapters.get(n)
        assert f"SN{n} · {ad.label}" in made(), (n, made())
        tid, _ = await store.topic_for(CHAT, n)
        guide = sent_to(tid)
        assert guide and f"SN{n}" in guide[0] and legal(guide[0]), (n, guide)
    pins = [p for m, p in calls if m == "pinChatMessage"]
    assert len(pins) == len(want), (len(pins), len(want))
    assert (await store.topic_for(CHAT, first))[0] == EXISTING_TID
    assert any("New subnet topic" in t for t in sent_to(0))
    print(f"sync created {len(want)} topics, guide pinned in each  OK")

    calls.clear()
    new, _ = await topics.sync_chat(CHAT)
    assert not new and not made(), made()
    print("second sync creates nothing                  OK")

    # --- 2. /unbind is remembered --------------------------------------------
    tid_second, _ = await store.topic_for(CHAT, second)
    calls.clear()
    await commands.handle(CHAT, tid_second, "unbind", "", "supergroup")
    assert await store.topic_for(CHAT, second) is None
    assert second in await store.skipped_topics(CHAT)
    await topics.sync_chat(CHAT)
    assert not made(), made()
    print("/unbind is not undone by the sync            OK")

    # --- 3. a deleted topic ---------------------------------------------------
    third = sorted(held)[2] if len(held) > 2 else first
    tid3, _ = await store.topic_for(CHAT, third)
    gone_threads.add(tid3)
    calls.clear()
    res = await router.send_to(CHAT, tid3, "<b>an alert</b>")
    assert res, "the alert was lost"
    assert await store.topic_for(CHAT, third) is None
    assert third in await store.skipped_topics(CHAT)
    general = sent_to(0)
    assert any("an alert" in t for t in general), general
    assert any("no longer exists" in t for t in general), general
    await router.send_to(CHAT, 0, "again")
    assert len([m for m, p in calls if m == "sendMessage"
                and p.get("message_thread_id") == tid3]) == 1
    await topics.sync_chat(CHAT)
    assert not made(), made()
    print("deleted topic: unbound once, alert in General OK")

    # a deleted DIGEST is unbound too, and nothing is skipped
    await store.bind_topic(CHAT, 3, None, "digest")
    gone_threads.add(3)
    await router.send_to(CHAT, 3, "credit alert")
    net, bound = await store.netuid_for_topic(CHAT, 3)
    assert not bound
    print("deleted digest topic: unbound                OK")

    # --- 4. /setup clears the opt-outs ----------------------------------------
    gone_threads.clear()
    calls.clear()
    await commands.handle(CHAT, 0, "setup", "", "supergroup")
    for n in (second, third):
        assert await store.topic_for(CHAT, n), f"/setup did not restore SN{n}"
    assert not await store.skipped_topics(CHAT)
    for ad in adapters.ADAPTERS.values():
        assert await store.topic_for(CHAT, ad.netuid), f"/setup missed SN{ad.netuid}"
    print("/setup restores skipped + adapter subnets    OK")

    calls.clear()
    await commands.handle(CHAT, 0, "topics", "", "supergroup")
    listing = sent_to(0)[-1]
    assert legal(listing) and f"SN{first}" in listing, listing
    print("/topics renders                              OK")

    # --- 5. refused creation backs off ---------------------------------------
    await reset()
    await store.bind_topic(CHAT, 0, None, "digest")
    refuse_create[0] = True
    new, failed = await topics.sync_chat(CHAT)
    assert not new and sorted(failed) == sorted(held), (new, failed)
    n_tries = len(made())
    await topics.sync_chat(CHAT)
    assert len(made()) == n_tries, "retried a refused creation immediately"
    refuse_create[0] = False
    print("refused creation is not retried every cycle  OK")

    # --- 6. chain events route by subnet -------------------------------------
    await reset()
    await store.bind_topic(CHAT, 0, None, "digest")
    await topics.sync_chat(CHAT)

    async def subs(kind):
        return [(CHAT, 1)]
    events_mod.subscribers = subs

    covered = next((n for n in held if "dereg" in adapters.get(n).covers), None)
    plain = next(n for n in sorted(held) if "dereg" not in adapters.get(n).covers)
    tid_plain, _ = await store.topic_for(CHAT, plain)

    calls.clear()
    await bot.notify_event({"kind": "my_miners", "netuid": plain, "severity": "warn",
                            "title": f"Deregistered on SN{plain} <x>", "body": "1 lost",
                            "user_id": 1})
    got = sent_to(tid_plain)
    assert got and "&lt;x&gt;" in got[0] and legal(got[0]), got
    assert not sent_to(0)
    loud = [p for m, p in calls if m == "sendMessage"][0]
    assert loud["disable_notification"] is False
    print("dereg goes to its topic, loud, escaped       OK")

    if covered is not None:
        calls.clear()
        await bot.notify_event({"kind": "my_miners", "netuid": covered,
                                "severity": "warn", "title": "x", "user_id": 1})
        assert not [c for c in calls if c[0] == "sendMessage"], calls
        print(f"SN{covered} reports its own dereg: not doubled   OK")

    calls.clear()
    await bot.notify_event({"kind": "registration", "netuid": plain,
                            "severity": "info", "title": "reg opened"})
    assert not [c for c in calls if c[0] == "sendMessage"]
    print("registration not doubled in a topic          OK")

    calls.clear()
    await bot.notify_event({"kind": "king_change", "netuid": 99999,
                            "severity": "info", "title": "new top earner"})
    g = sent_to(0)
    assert g and "/sn 99999" in g[0], g
    assert [p for m, p in calls if m == "sendMessage"][0]["disable_notification"] is True
    print("no topic -> General, silent for info         OK")

    # --- 7. chain-only view ---------------------------------------------------
    chain_only = [n for n in sorted(held) if n not in adapters.ADAPTERS]
    for n in chain_only:
        ad = adapters.get(n)
        s = await ad.snapshot()
        assert s, f"SN{n} empty snapshot"
        s["_chain"] = await poller._chain_facts(n)
        s["_ours_chain"] = await store.ours_on_chain(n)
        for name, text in (("state", ad.render_state(s) + commands.chain_block(s)),
                           ("info", ad.render_info(s)), ("board", ad.render_board(s)),
                           ("mine", ad.render_me(s)), ("guide", ad.render_guide(s))):
            assert legal(text), (n, name, text)
        assert ad.diff({}, s) == [] and ad.diff(s, s) == []
        assert poller._diff_ours_chain({}, s) == []
        assert poller._diff_ours_chain(s, s) == []
    print(f"chain view renders for {len(chain_only)} subnets             OK")

    on = {"_ours_chain": {"n": 3, "earning": 2, "tpd": 1.5, "uids": []}}
    off = {"_ours_chain": {"n": 3, "earning": 0, "tpd": 0.0, "uids": []}}
    ev = poller._diff_ours_chain(on, off)
    assert len(ev) == 1 and ev[0].severity == "warn", ev
    ev = poller._diff_ours_chain(off, on)
    assert len(ev) == 1 and ev[0].severity == "good", ev
    assert poller._diff_ours_chain(on, {}) == []
    print("earning flip fires; an unread sweep does not OK")

    # --- 8. an over-long message is split, not lost ---------------------------
    calls.clear()
    table = "<b>OURS</b>\n<pre>" + "\n".join(f"row {i:04d} " + "x" * 60
                                             for i in range(150)) + "</pre>"
    await router.send_to(CHAT, 0, table)
    parts = sent_to(0)
    assert len(parts) >= 3, len(parts)
    assert all(legal(p) and router.visible_len(p) <= 4096 for p in parts)
    assert all("row 0149" in "".join(parts) for _ in [0])
    print(f"10k-char table split into {len(parts)} legal parts     OK")

    print("\nALL TOPIC CHECKS PASSED")
    return 0


raise SystemExit(asyncio.run(main()))
