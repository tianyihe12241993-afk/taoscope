"""Exercise every topic-aware command against the live database.

Telegram sends are captured, not performed -- the point is to prove that a
thread id resolves to the right subnet and that each command renders, without
posting anything into a real chat.
"""
import asyncio, os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import db
from app.comp import commands, router, store

SENT = []
async def fake_send(chat_id, thread_id, text, *, silent=False):
    SENT.append((chat_id, thread_id, text)); return {"ok": True}
router.send_to = fake_send
commands.router.send_to = fake_send

CHAT, SN100_THREAD, DIGEST_THREAD, UNBOUND = -1009999999999, 777, 1, 999


async def run(cmd, arg="", thread=SN100_THREAD):
    SENT.clear()
    handled = await commands.handle(CHAT, thread, cmd, arg, "supergroup")
    body = SENT[-1][2] if SENT else ""
    head = body.splitlines()[0] if body else "(no reply)"
    print(f"  /{cmd:8s} {('@'+str(thread)):>6s} handled={handled!s:5s} "
          f"{len(body):4d}ch  {head[:78]}")
    return body


async def main():
    await db.connect()
    print("== unbound topic: answers, but says it is unbound ==")
    b = await run("state", thread=UNBOUND)
    assert "isn't bound" in b and "SN100" in b, b

    print("\n== bind, then every command with NO arguments ==")
    await run("bind", "100")
    for c in ("state", "info", "board", "mine", "events"):
        if c == "events":
            body = await commands.events_for_topic(CHAT, SN100_THREAD)
            print(f"  /events   @{SN100_THREAD} {len(body or ''):4d}ch  "
                  f"{(body or '').splitlines()[0][:78]}")
            assert body is not None
            continue
        b = await run(c)
        assert len(b) > 60, f"/{c} rendered almost nothing"
        assert "SN100" in b or "leaderboard" in b or "Our SN100" in b

    print("\n== explicit netuid overrides the binding (works in a DM) ==")
    b = await run("state", "100", thread=UNBOUND)
    assert "SN100" in b

    print("\n== digest topic ==")
    await run("bind", "digest", thread=DIGEST_THREAD)
    b = await run("state", thread=DIGEST_THREAD)
    assert "All tracked competitions" in b and "SN100" in b, b

    print("\n== watch / unwatch / mute round-trip ==")
    await run("watch", "deadbeefcafe0000 smoke-test-entry")
    assert any(r["ref"] == "deadbeefcafe0000" for r in await store.watched(100))
    await run("unwatch", "deadbeefcafe0000")
    assert not any(r["ref"] == "deadbeefcafe0000" for r in await store.watched(100))
    await run("mute", "queue")
    prefs = [t for t in await store.all_topics()
             if t["chat_id"] == CHAT and t["thread_id"] == SN100_THREAD][0]["prefs"]
    assert prefs.get("queue") is False, prefs
    await run("unmute", "queue")

    print("\n== an unrelated command must fall through to the chain bot ==")
    SENT.clear()
    assert await commands.handle(CHAT, SN100_THREAD, "top", "", "supergroup") is False
    assert not SENT

    print("\n== topics list ==")
    await run("topics")

    # leave no test rows behind
    await store.unbind_topic(CHAT, SN100_THREAD)
    await store.unbind_topic(CHAT, DIGEST_THREAD)
    await db.pool().execute("DELETE FROM comp_watch WHERE ref='deadbeefcafe0000'")
    await db.close()
    print("\nALL COMMAND CHECKS PASSED")


asyncio.run(main())
