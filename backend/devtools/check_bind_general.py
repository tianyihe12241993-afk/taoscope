"""`/bind <netuid>` in General must create a topic, not hijack General.

The bug this covers, seen live on 2026-08-24: `/bind 15` typed in the General
topic bound thread 0 to SN15. Three things went wrong at once and none of them
was visible —

  1. SN15's alerts were routed to General instead of a topic of its own
  2. the digest binding on thread 0 was OVERWRITTEN (bind_topic upserts on
     (chat_id, thread_id))
  3. `/setup` then SKIPPED SN15 forever, because it counted as already bound

so no command the operator would naturally reach for could undo it.

Runs against a real database, with Telegram stubbed — nothing is sent.
"""
import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import db                                    # noqa: E402
from app.comp import commands, router, store, topics  # noqa: E402
from app.telegram import bot                          # noqa: E402

CHAT = -999_000_111          # a chat id no real group can collide with
NEW_TID = 4242

sent: list = []
created: list = []


async def fake_call(method, **params):
    if method == "createForumTopic":
        created.append(params.get("name"))
        return {"message_thread_id": NEW_TID, "name": params.get("name")}
    return {"message_id": 1}


async def fake_send(chat_id, thread_id, text, silent=False, **kw):
    sent.append((thread_id, text))
    return True


async def main() -> int:
    await db.connect()
    bot.call = fake_call
    commands.call = fake_call          # imported inside the branch, but be safe
    router.send_to = fake_send
    topics.CREATE_GAP_S = 0            # /setup paces real creations; not here

    async def reset():
        await db.pool().execute("DELETE FROM comp_topic WHERE chat_id=$1", CHAT)
        await db.pool().execute("DELETE FROM comp_topic_skip WHERE chat_id=$1", CHAT)

    # --- 1. General starts as the digest, as /setup leaves it -----------------
    await reset()
    await store.bind_topic(CHAT, 0, None, "digest")
    sent.clear(); created.clear()

    ok = await commands.handle(CHAT, 0, "bind", "15", "supergroup")
    assert ok
    assert created == ["SN15 · ORO"], created
    print(f"created a topic named {created[0]!r}          OK")

    net, bound = await store.netuid_for_topic(CHAT, NEW_TID)
    assert bound and net == 15, (net, bound)
    print("new topic is bound to SN15                  OK")

    net, bound = await store.netuid_for_topic(CHAT, 0)
    assert bound and net is None, (net, bound)
    print("General restored to the digest              OK")

    tg = await store.targets(15)
    mine = [t for t in tg if t[0] == CHAT]
    assert len(mine) == 1 and mine[0][1] == NEW_TID, mine
    print("exactly ONE delivery target, not two        OK")

    assert any(t == NEW_TID for t, _ in sent), sent
    print("greeting posted in the new topic            OK")

    # --- 1b. the ACTUAL live state: General already hijacked by the subnet ----
    # This is the state the bug left behind, and the one the fix has to recover
    # from -- not the clean digest state above.
    await reset()
    await store.bind_topic(CHAT, 0, 15, "ORO")        # what /bind 15 used to do
    created.clear(); sent.clear()

    await commands.handle(CHAT, 0, "bind", "15", "supergroup")
    assert created == ["SN15 · ORO"], created
    net, bound = await store.netuid_for_topic(CHAT, NEW_TID)
    assert bound and net == 15
    net, bound = await store.netuid_for_topic(CHAT, 0)
    assert bound and net is None, f"General still bound to {net}"
    mine = [t for t in await store.targets(15) if t[0] == CHAT]
    assert len(mine) == 1 and mine[0][1] == NEW_TID, mine
    print("recovers from a hijacked General            OK")

    # Put the clean state back for step 2.
    await reset()
    await store.bind_topic(CHAT, 0, None, "digest")
    await store.bind_topic(CHAT, NEW_TID, 15, "ORO")

    # --- 2. /setup must now leave SN15 alone (it is properly bound) -----------
    created.clear()
    await commands.handle(CHAT, 0, "setup", "", "supergroup")
    assert "SN15 · ORO" not in created, created
    print("/setup does not duplicate the SN15 topic    OK")

    # --- 3. binding INSIDE a real topic is unchanged --------------------------
    await reset()
    created.clear()
    await commands.handle(CHAT, 777, "bind", "62", "supergroup")
    assert not created, "created a topic when already inside one"
    net, bound = await store.netuid_for_topic(CHAT, 777)
    assert bound and net == 62, (net, bound)
    print("/bind inside a topic binds that topic       OK")

    # --- 4. no Manage Topics -> change NOTHING, and say why -------------------
    await reset()
    await store.bind_topic(CHAT, 0, None, "digest")
    sent.clear()

    async def refuse(method, **params):
        return None if method == "createForumTopic" else {"message_id": 1}

    bot.call = refuse
    await commands.handle(CHAT, 0, "bind", "15", "supergroup")
    net, bound = await store.netuid_for_topic(CHAT, 0)
    assert bound and net is None, f"General was hijacked on failure: {net}"
    assert not [t for t in await store.targets(15) if t[0] == CHAT]
    said = " ".join(x[1] for x in sent)
    assert "Manage Topics" in said and "nothing was changed" in said, said
    print("creation refused -> General untouched       OK")

    # --- 5. a non-forum group still binds General, as before ------------------
    await reset()
    bot.call = fake_call
    await commands.handle(CHAT, 0, "bind", "15", "group")
    net, bound = await store.netuid_for_topic(CHAT, 0)
    assert bound and net == 15, (net, bound)
    print("plain group falls back to binding it        OK")

    await reset()
    await db.close()
    print("\nALL BIND CHECKS PASSED")
    return 0


raise SystemExit(asyncio.run(main()))
