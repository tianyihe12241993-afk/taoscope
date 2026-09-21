"""Deliver a competition event to the forum topic that owns its subnet.

Telegram routing facts this depends on:

  * `message_thread_id` on sendMessage targets one forum topic. One bot serves
    every subnet -- a second bot token buys nothing and costs a second exclusive
    long-poll loop.
  * The General topic is thread 1, but sending WITH message_thread_id=1 fails on
    some forum configurations where General is hidden. Sending without the field
    always lands in General, so thread ids <= 1 are sent bare.
  * A group accepts ~20 bot messages per minute. Events from one poll are
    therefore delivered with a small gap and the loop never bursts.
"""
from __future__ import annotations

import asyncio
import logging
import re

from . import store
from .base import SEVERITY

log = logging.getLogger("taoscope.comp.router")

SEND_GAP_S = 1.2


def thread_gone(error: str) -> bool:
    """Telegram's answer when the topic a message targets has been deleted."""
    return "message thread not found" in (error or "").lower()


# Telegram rejects a message whose text -- measured after the HTML is parsed, in
# UTF-16 units -- exceeds 4096. The plain-text retry is just as long, so an
# over-long message was lost outright: SN62's /mine renders ~6,700 characters.
MAX_TEXT = 4000


def visible_len(text: str) -> int:
    import html
    plain = html.unescape(_strip(text))
    return len(plain.encode("utf-16-le")) // 2


_TAG = re.compile(r"<(/?)([a-zA-Z-]+)[^>]*>")


def _open_tags(text: str) -> list[str]:
    """The opening tags `text` leaves unclosed, outermost first."""
    stack: list[str] = []
    for m in _TAG.finditer(text):
        if m.group(1):
            for i in range(len(stack) - 1, -1, -1):
                if _TAG.match(stack[i]).group(2) == m.group(2):
                    del stack[i]
                    break
        else:
            stack.append(m.group(0))
    return stack


def split_html(text: str, limit: int = MAX_TEXT) -> list[str]:
    """Split a message into parts Telegram will accept, each one valid HTML.

    Paragraphs stay whole where they fit; a paragraph that alone is too long is
    split by line. A tag left open at a cut (a <pre> table, usually) is closed
    at the end of that part and reopened at the start of the next."""
    if visible_len(text) <= limit:
        return [text]
    pieces: list[tuple[str, str]] = []          # (separator before it, text)
    for para in text.split("\n\n"):
        lines = [para] if visible_len(para) <= limit else para.split("\n")
        pieces.append(("\n\n", lines[0]))
        pieces.extend(("\n", ln) for ln in lines[1:])
    parts, cur = [], ""
    for sep, piece in pieces:
        cand = cur + sep + piece if cur else piece
        if cur and visible_len(cand) > limit:
            # `cur` is self-contained: it starts with any tags reopened at the
            # previous cut, so what is still open is read from it alone.
            still = _open_tags(cur)
            parts.append(cur + "".join(f"</{_TAG.match(o).group(2)}>"
                                       for o in reversed(still)))
            cur = "".join(still) + piece
        else:
            cur = cand
    if cur:
        parts.append(cur)
    return parts


async def _send_one(chat_id: int, thread_id: int, text: str,
                    silent: bool) -> tuple[dict | None, int]:
    """One message, at most 4096 visible characters. (sent, thread it went to)."""
    from ..telegram import bot          # late: bot imports comp.commands

    params = dict(chat_id=chat_id, text=text, parse_mode="HTML",
                  disable_web_page_preview=True, disable_notification=silent)
    if thread_id and thread_id > 1:
        params["message_thread_id"] = thread_id
    sent, err = await bot.call_ex("sendMessage", **params)
    if sent is None and "message_thread_id" in params and thread_gone(err):
        # A deleted topic 400s forever. Falling back to General on every send
        # kept the dead binding alive -- thread 3 failed every 15 minutes for
        # days and each credit alert reached General twice. Drop the binding
        # once, say so once, and deliver this message to General.
        netuid = await store.topic_gone(chat_id, thread_id)
        what = f"SN{netuid}" if netuid is not None else "the digest"
        log.warning("chat=%s thread=%s (%s) was deleted — binding removed",
                    chat_id, thread_id, what)
        params.pop("message_thread_id")
        thread_id = 0
        await bot.call("sendMessage", chat_id=chat_id, parse_mode="HTML",
                       disable_notification=True,
                       text=(f"🧹 The topic for <b>{what}</b> no longer exists, so "
                             f"its binding was removed and it will not be "
                             f"recreated automatically."
                             + (f"\n<code>/bind {netuid}</code> inside a topic, or "
                                f"<code>/setup</code>, to bring it back."
                                if netuid is not None else "")))
        sent, err = await bot.call_ex("sendMessage", **params)
    if sent is None:
        # Never lose an event to a formatting problem: HTML that Telegram
        # rejects takes the whole message with it, so retry as plain text.
        params.pop("parse_mode", None)
        params["text"] = _strip(text)
        sent = await bot.call("sendMessage", **params)
    return sent, thread_id


async def send_to(chat_id: int, thread_id: int, text: str,
                  *, silent: bool = False) -> dict | None:
    """Send to a topic (thread <= 1 is General). Returns the first part sent."""
    first = None
    for i, part in enumerate(split_html(text)):
        sent, thread_id = await _send_one(chat_id, thread_id, part, silent)
        if i == 0:
            first = sent
    return first


def _strip(text: str) -> str:
    return re.sub(r"<[^>]+>", "", text)


# severity -> the leading character of a `diff` line.
#
# Telegram's HTML has no colour markup whatsoever -- the whole tag list is
# <b> <i> <u> <s> <code> <pre> <a> <blockquote>. The one place a client will
# paint text is inside a syntax-highlighted code block, so a headline that has
# to stand out is emitted as a one-line `language-diff` block: a `-` line is
# rendered red and a `+` line green by the highlighter, and the block itself is
# boxed, which separates an alert from the wall of ordinary messages.
#
# Only the severities that should interrupt someone get the banner. `info` stays
# plain text on purpose: routine churn that shouts is how a topic gets muted,
# and a red box for a leaderboard wobble trains people to ignore red boxes.
BANNER = {
    "critical": "-",     # red
    "warn":     "!",     # highlighted as "changed" where the client supports it
    "good":     "+",     # green
}


def format_event(ev: dict) -> str:
    """One icon, one headline, then the change itself.

    The adapter's icon wins when it set one -- a stage glyph says more than a
    severity glyph. Titles must never carry their own icon, or the two collide
    and the message opens with "• •".

    The title is inserted verbatim, never re-escaped: adapters already run their
    interpolated values through esc(), and escaping twice would surface a literal
    "&amp;" inside the banner.
    """
    fallback, _ = SEVERITY.get(ev["severity"], ("•", False))
    icon = ev.get("icon") or fallback
    mark = BANNER.get(ev["severity"])
    if mark:
        out = ('<pre><code class="language-diff">'
               f"{mark} {icon} {ev['title']}"
               "</code></pre>")
    else:
        out = f"{icon} <b>{ev['title']}</b>"
    if ev.get("body"):
        out += f"\n{ev['body']}"
    return out


async def deliver(netuid: int, events: list[dict]) -> None:
    if not events:
        return
    dests = await store.targets(netuid)
    if not dests:
        log.info("SN%s: %d event(s) with no topic bound — run /bind %s in a topic",
                 netuid, len(events), netuid)
        return
    for ev in events:
        icon, loud = SEVERITY.get(ev["severity"], ("•", False))
        text = format_event(ev)
        for chat_id, thread_id, prefs in dests:
            if prefs.get(ev["kind"]) is False:
                continue
            await send_to(chat_id, thread_id, text, silent=not loud)
            await asyncio.sleep(SEND_GAP_S)
        await store.mark_delivered(ev["id"])
