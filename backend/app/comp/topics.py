"""One forum topic per subnet we hold a UID on -- created, bound and primed.

Every path that makes a topic goes through create_topic(), so a topic made by
/setup, by /bind in General, or by the automatic sync looks the same:

    name     "SN<netuid> · <label>"
    binding  comp_topic row -> alerts route here from the next poll
    primer   the subnet's /guide, posted silently and pinned

sync_all() runs from the competition supervisor. It only ever ADDS topics:

  * a subnet whose UIDs we lose keeps its topic (the deregistration alert has
    to land somewhere, and the history is worth keeping)
  * a topic the operator deleted or /unbind-ed is recorded in comp_topic_skip
    and never recreated automatically -- /setup or /bind <netuid> brings it back
  * a creation Telegram refuses is retried after an hour, not every cycle
"""
from __future__ import annotations

import asyncio
import logging
import time

from . import router, store
from .adapters import all_adapters, ensure_chain, get as get_adapter
from .base import SubnetAdapter, esc

log = logging.getLogger("taoscope.comp.topics")

# Each topic costs a createForumTopic, a message and a pin. A group takes about
# 20 bot messages a minute, so a first sync of a dozen subnets is paced.
CREATE_GAP_S = 3.0
RETRY_AFTER_S = 3600

_failed_at: dict[tuple[int, int], float] = {}


def topic_name(ad: SubnetAdapter) -> str:
    # Telegram caps a topic name at 128 characters.
    return f"SN{ad.netuid} · {ad.label}"[:128]


async def create_topic(chat_id: int, ad: SubnetAdapter) -> int | None:
    """Create, bind and prime one subnet topic. Returns its thread id."""
    from ..telegram import bot          # late: bot imports comp.commands

    res = await bot.call("createForumTopic", chat_id=chat_id, name=topic_name(ad))
    tid = (res or {}).get("message_thread_id")
    if not tid:
        return None
    await store.bind_topic(chat_id, tid, ad.netuid, ad.label)
    await store.unskip_topic(chat_id, ad.netuid)
    s = await store.last_state(ad.netuid)
    sent = await router.send_to(chat_id, tid, ad.render_guide(s), silent=True)
    mid = sent.get("message_id") if isinstance(sent, dict) else None
    if mid:
        # Best effort: the primer is still in the topic if pinning is refused.
        await bot.call("pinChatMessage", chat_id=chat_id, message_id=mid,
                       disable_notification=True)
    return tid


async def missing(chat_id: int, netuids: dict[int, str], *,
                  honour_skips: bool = True) -> list[SubnetAdapter]:
    """Adapters for the subnets in `netuids` this chat has no topic for."""
    bound = {t["netuid"] for t in await store.all_topics()
             if t["chat_id"] == chat_id and t["netuid"] is not None}
    skipped = await store.skipped_topics(chat_id) if honour_skips else set()
    out = []
    for netuid, name in sorted(netuids.items()):
        if netuid in bound or netuid in skipped:
            continue
        out.append(ensure_chain(netuid, name))
    return out


async def sync_chat(chat_id: int) -> tuple[list[int], list[int]]:
    """Give this chat a topic for every held subnet. (made, failed)."""
    held = await store.held_subnets()
    made, failed = [], []
    for ad in await missing(chat_id, held):
        key = (chat_id, ad.netuid)
        if time.time() - _failed_at.get(key, 0) < RETRY_AFTER_S:
            continue
        tid = await create_topic(chat_id, ad)
        if tid is None:
            _failed_at[key] = time.time()
            failed.append(ad.netuid)
            log.warning("SN%s: could not create a topic in chat=%s (needs admin "
                        "with Manage Topics); retrying in %ss",
                        ad.netuid, chat_id, RETRY_AFTER_S)
        else:
            _failed_at.pop(key, None)
            made.append(ad.netuid)
            log.info("SN%s: created topic %s in chat=%s", ad.netuid, tid, chat_id)
        await asyncio.sleep(CREATE_GAP_S)
    if made:
        names = ", ".join(f"<b>SN{n}</b> {esc(get_adapter(n).label)}" for n in made)
        await router.send_to(
            chat_id, 0,
            f"📡 New subnet topic{'s' if len(made) > 1 else ''} — our hotkeys "
            f"are registered there:\n{names}\n\n<i>Each has its guide pinned. "
            f"/topics lists every binding.</i>", silent=True)
    return made, failed


async def sync_all() -> None:
    for chat_id in await store.forum_chats():
        try:
            await sync_chat(chat_id)
        except Exception:  # noqa: BLE001 -- one chat must not stop the others
            log.exception("topic sync failed for chat=%s", chat_id)


def tracked_for_setup(held: dict[int, str]) -> dict[int, str]:
    """What /setup covers: every hand-written adapter plus every held subnet."""
    out = {a.netuid: a.label for a in all_adapters()}
    for n, name in held.items():
        out.setdefault(n, name)
    return out
