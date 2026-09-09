"""The bot must stay silent unless it was actually spoken to.

Regression test for two real incidents:

  * Renaming a forum topic made the bot reply "Ask me something, e.g. ...".
    Telegram delivers a rename as a service message -- a `message` with no text
    and `forum_topic_edited` set.

  * Worse, and only visible once /setup created the topic: Telegram threads
    EVERY message in a forum topic against that topic's creation service
    message. Because the bot created the topic, that root is authored by the
    bot, so "is this a reply to me?" was true for every line typed in the
    topic -- silently turning it into a billed AI chat.

Nothing here touches Telegram or the model; both are stubbed and the test
asserts on what WOULD have been sent.
"""
import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import db                        # noqa: E402
from app.telegram import bot              # noqa: E402

CHAT, THREAD, ROOT = -1004351639318, 14, 14   # topic root id == thread id
BOT = {"is_bot": True, "username": "bittensorshibibot"}
HUMAN = {"is_bot": False, "username": "operator"}

ASKED: list[str] = []
SENT: list[str] = []


async def fake_answer(chat_id, question, message_id=None):
    ASKED.append(question)


async def fake_send(chat_id, text, thread_id=None):
    SENT.append(text)
    return {"ok": True}


async def fake_call(method, **params):
    SENT.append(f"{method}:{params.get('text', '')}")
    return {"ok": True}


def msg(**kw):
    base = {"message_id": 900, "chat": {"id": CHAT, "type": "supergroup",
                                        "title": "Bittensor"},
            "from": HUMAN, "message_thread_id": THREAD}
    base.update(kw)
    return {"update_id": 1, "message": base}


# The topic root: a service message authored by the bot, because /setup made it.
TOPIC_ROOT = {"message_id": ROOT, "from": BOT,
              "forum_topic_created": {"name": "SN100 · BASE / Prism"}}

# (name, update, should reach the model, should send anything at all)
CASES = [
    ("topic renamed",
     msg(forum_topic_edited={"name": "SN100 · Prism"}), False, False),
    ("topic created",
     msg(forum_topic_created={"name": "SN100"}), False, False),
    ("a pin",
     msg(pinned_message={"message_id": 5}), False, False),
    ("plain chatter in a bot-created topic",
     msg(text="ok that looks good", reply_to_message=TOPIC_ROOT), False, False),
    ("a photo with no caption",
     msg(photo=[{"file_id": "x"}]), False, False),
    ("another bot joins",
     msg(new_chat_members=[BOT]), False, False),
    ("a person joins",
     msg(new_chat_members=[{"is_bot": False, "first_name": "Ken"}]), False, True),
    ("a person with markup in their name joins",
     msg(new_chat_members=[{"is_bot": False, "first_name": "<b>x</b>"}]), False, True),
    ("a genuine reply to something the bot said",
     msg(text="why is that?",
         reply_to_message={"message_id": 901, "from": BOT, "text": "king changed"}),
     True, False),
    ("an explicit @mention",
     msg(text="@bittensorshibibot what should I mine?"), True, False),
]


async def main() -> int:
    await db.connect()
    bot.BOT_USERNAME = "bittensorshibibot"
    bot.answer_question = fake_answer
    bot.send = fake_send
    bot.call = fake_call

    failures = 0
    for name, update, should_ask, should_send in CASES:
        ASKED.clear(); SENT.clear()
        await bot.handle(update)
        ok = bool(ASKED) == should_ask and bool(SENT) == should_send
        failures += not ok
        want = ("asks model" if should_ask else
                "welcomes" if should_send else "SILENT")
        got = (f"asked={ASKED[0]!r}" if ASKED else
               f"sent={SENT[0][:34]!r}" if SENT else "nothing")
        print(f"  {'OK ' if ok else 'BAD'} {name:44s} want {want:10s} → {got}")

    # a welcome must never carry unescaped user markup
    ASKED.clear(); SENT.clear()
    await bot.handle(msg(new_chat_members=[{"is_bot": False,
                                            "first_name": "<b>evil</b>"}]))
    escaped = SENT and "&lt;b&gt;evil&lt;/b&gt;" in SENT[0] and "<b>evil</b>" not in SENT[0]
    failures += not escaped
    print(f"  {'OK ' if escaped else 'BAD'} {'welcome escapes a hostile name':44s} "
          f"want escaped   → {'escaped' if escaped else SENT[:1]}")

    await db.close()
    if failures:
        print(f"\n{failures} CASE(S) FAILED")
        return 1
    print("\nSILENCE RULES HOLD")
    return 0


raise SystemExit(asyncio.run(main()))
