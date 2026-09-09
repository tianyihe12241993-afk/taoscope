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

from . import store
from .base import SEVERITY

log = logging.getLogger("taoscope.comp.router")

SEND_GAP_S = 1.2


async def send_to(chat_id: int, thread_id: int, text: str,
                  *, silent: bool = False) -> dict | None:
    from ..telegram.bot import call     # late: bot imports comp.commands

    params = dict(chat_id=chat_id, text=text, parse_mode="HTML",
                  disable_web_page_preview=True, disable_notification=silent)
    if thread_id and thread_id > 1:
        params["message_thread_id"] = thread_id
    sent = await call("sendMessage", **params)
    if sent is None:
        # Never lose an event to a formatting problem: HTML that Telegram
        # rejects takes the whole message with it, so retry as plain text.
        params.pop("parse_mode", None)
        params["text"] = _strip(text)
        sent = await call("sendMessage", **params)
        if sent is None and "message_thread_id" in params:
            # A deleted topic 400s forever; fall back to General so the alert
            # is seen, and let /topics show the stale binding.
            params.pop("message_thread_id")
            sent = await call("sendMessage", **params)
    return sent


def _strip(text: str) -> str:
    import re
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
