"""Telegram bot (requirement 6).

Long-polling on purpose: webhooks need a public HTTPS URL, and this box has no
domain yet. Polling works from behind anything and needs no inbound port.
Set TAOSCOPE_TELEGRAM_BOT_TOKEN to switch it on; without it the loop never starts.
"""
import asyncio
import contextvars
import html as _html
import logging
import re

import httpx

from .. import events as events_mod
from ..ai import agent as ai
from ..comp import commands as comp_commands
from ..config import settings
from ..db import pool
from ..hub import hub

log = logging.getLogger("taoscope.telegram")

API = "https://api.telegram.org/bot{token}/{method}"

# filled in once the bot identifies itself; needed to spot @mentions in groups
BOT_USERNAME: str | None = None
TAO_PER_DAY = "(n.emission * (7200.0 / NULLIF(s.tempo,0)) * s.price)"

ICON = {
    "new_subnet": "🆕", "king_change": "👑", "registration": "🚪",
    "my_miners": "⚠️", "emission_move": "📈", "price_alert": "🔔",
}

HELP = (
    "<b>TaoScope</b>\n\n"
    "/link <code>CODE</code> — connect this chat to your account\n"
    "/ask <code>question</code> — ask anything about the network\n"
    "/events — the latest network events\n"
    "/me — your coldkeys, hotkeys and daily earnings\n"
    "/sn <code>64</code> — subnet snapshot\n"
    "/ck <code>5Abc…</code> — any coldkey's footprint\n"
    "/top — biggest earners on the network\n"
    "/alerts — your active price alerts\n"
    "/keys — API credit left across providers, checked live\n"
    "/pods — Lium pods: status, GPU, \$/h, uptime, spend\n"
    "/comphelp — per-subnet competition tracking\n"
    "/help — this message"
)


async def call(method: str, **params):
    if not settings.telegram_bot_token:
        return None
    url = API.format(token=settings.telegram_bot_token, method=method)
    async with httpx.AsyncClient(timeout=70) as cx:
        r = await cx.post(url, json=params)
        if r.status_code != 200:
            # Name the TARGET, not just the error. "message thread not found"
            # with no chat/thread in the line is undiagnosable -- a deleted
            # forum topic looks identical to a bad binding, and the alert that
            # was dropped is invisible. A monitor that fails silently is worth
            # nothing; one that fails loudly but anonymously is barely better.
            target = ""
            if method in ("sendMessage", "editMessageText", "sendPhoto"):
                target = (f" [chat={params.get('chat_id')} "
                          f"thread={params.get('message_thread_id', 'General')}]")
            log.warning("telegram %s failed%s: %s", method, target, r.text[:200])
            return None
        return r.json().get("result")


# The forum topic the message being handled arrived in. A reply must land back
# in the same topic or it appears in General, next to an unrelated subnet -- so
# it is carried in a contextvar rather than threaded through every signature.
CURRENT_THREAD: contextvars.ContextVar[int] = contextvars.ContextVar("thread", default=0)


async def send(chat_id: int, text: str, thread_id: int | None = None):
    tid = CURRENT_THREAD.get() if thread_id is None else thread_id
    params = dict(chat_id=chat_id, text=text, parse_mode="HTML",
                  disable_web_page_preview=True)
    # General is thread 1, and passing message_thread_id=1 is rejected when
    # General is hidden. Omitting the field always lands in General.
    if tid and tid > 1:
        params["message_thread_id"] = tid
    return await call("sendMessage", **params)


def _fmt(n, d=2):
    try:
        return f"{float(n):,.{d}f}"
    except (TypeError, ValueError):
        return "—"


async def _user_for_chat(chat_id: int) -> int | None:
    return await pool().fetchval(
        "SELECT user_id FROM telegram_link WHERE chat_id=$1 AND linked_at IS NOT NULL", chat_id
    )


# ---------------- commands ----------------
async def cmd_link(chat_id: int, arg: str, username: str):
    code = arg.strip()
    if not code:
        return await send(chat_id, "Usage: <code>/link CODE</code> — get your code from Settings.")
    row = await pool().fetchrow("SELECT id, user_id FROM telegram_link WHERE link_code=$1", code)
    if row is None:
        return await send(chat_id, "That code is not valid. Generate a fresh one in Settings.")
    await pool().execute(
        "UPDATE telegram_link SET chat_id=$1, username=$2, linked_at=now(), link_code=NULL WHERE id=$3",
        chat_id, username, row["id"],
    )
    await send(chat_id, "✅ Linked — alerts will arrive here. Try /me or /sn 64.")


async def cmd_me(chat_id: int):
    uid = await _user_for_chat(chat_id)
    if not uid:
        return await send(chat_id, "This chat isn't linked yet. Use <code>/link CODE</code>.")
    rows = await pool().fetch(
        f"""
        SELECT n.netuid, s.name, count(*) AS uids, sum({TAO_PER_DAY}) AS tpd
        FROM my_coldkey m
        JOIN neuron_live n ON n.coldkey = m.coldkey
        JOIN subnet_live s ON s.netuid = n.netuid
        WHERE m.user_id = $1
        GROUP BY n.netuid, s.name ORDER BY tpd DESC NULLS LAST
        """,
        uid,
    )
    if not rows:
        return await send(chat_id, "No registered coldkeys found. Add one in the web UI first.")
    total = sum(r["tpd"] or 0 for r in rows)
    lines = [f"<b>Your positions</b> — τ{_fmt(total)}/day",
             f"<i>across {sum(r['uids'] for r in rows)} hotkeys on {len(rows)} subnets</i>", ""]
    for r in rows[:20]:
        lines.append(f"SN{r['netuid']} {r['name']} — {r['uids']} uid(s), τ{_fmt(r['tpd'])}/day")
    await send(chat_id, "\n".join(lines))


async def cmd_keys(chat_id: int):
    """What is left on every API key we monitor, and how long it lasts.

    Checks the providers live when asked -- a reading up to fifteen minutes
    old was the wrong answer to "can I start a run right now". The sweep is
    the poller's own, so anything it notices alerts exactly as usual."""
    from ..credits import poller as cp

    await call("sendChatAction", chat_id=chat_id, action="typing")
    refresh_error = await cp.refresh()
    rows = await cp.all_rows()
    if not rows:
        return await send(chat_id, "No API providers configured yet.\n"
                                   "<i>Set the keys in .env and restart.</i>")
    L = ["<b>API credit</b>", ""]
    for r in rows:
        name = r.get("provider", "?")
        if r.get("dead"):
            L.append(f"🔑 <b>{_html.escape(name)}</b> — <b>KEY REJECTED</b>")
            continue
        unit = r.get("unit") or "$"
        # "—" reads as a failure. A provider that simply has no balance to
        # report (Vercel bills per plan; Parallel refuses API keys) is fine,
        # and its note says why.
        left = cp.money(r.get("remaining"), unit) if r.get("remaining") is not None \
            else "<i>n/a</i>"
        line = f"💳 <b>{_html.escape(name)}</b> — {left}"
        sev = cp.severity_for(cp.bucket(r.get("remaining"), name))
        line += {"critical": " 🚨", "warn": " ⚠️"}.get(sev, "")
        L.append(line)
        bits = []
        if r.get("used") is not None:
            bits.append(f"used {cp.money(r['used'], unit)}")
        if r.get("used_today") is not None:
            bits.append(f"today {cp.money(r['used_today'], unit)}")
        run = cp.runway(r.get("remaining"), r.get("burn_per_h"))
        if run:
            bits.append(f"<b>{run}</b>")
        if r.get("note"):
            bits.append(f"<i>{_html.escape(str(r['note']))}</i>")
        if bits:
            L.append("   " + " · ".join(bits))
        if r.get("_error"):
            L.append(f"   <i>last poll: {_html.escape(str(r['_error']))}</i>")
    L.append("")
    # Age, always. When the live check failed these numbers are the stored
    # sweep, and a stale reading must never render as a confident balance --
    # that is exactly how a 3-hour-old reading went unnoticed.
    import datetime as _dt
    if refresh_error is None:
        L.append("<i>checked just now</i>")
    else:
        L.append(f"⚠️ <b>live check failed: {_html.escape(refresh_error)}</b> — showing the last stored reading")
        ages = [r["_at"] for r in rows if r.get("_at")]
        if ages:
            mins = (_dt.datetime.now(_dt.timezone.utc) - max(ages)).total_seconds() / 60
            stale = mins > (settings.credits_poll / 60) * 3
            L.append(("⚠️ <b>" if stale else "<i>")
                     + f"read {int(mins)} min ago"
                     + ("</b> — the poller looks stuck" if stale else "</i>"))
    L.append("<i>Alerts fire when a balance crosses "
             f"{_html.escape(settings.credits_thresholds)}.</i>")
    await send(chat_id, "\n".join(L))


async def cmd_pods(chat_id: int):
    """Lium pod status and what each one is costing."""
    import datetime as _dt
    from ..credits import poller as cp

    rows = {r["provider"]: r for r in await cp.all_rows()}
    lium = rows.get("lium")
    if not lium:
        return await send(chat_id, "Lium is not being monitored. "
                                   "<i>Set TAOSCOPE_LIUM_API_KEY.</i>")
    pods = lium.get("pods") or {}
    if not pods:
        return await send(chat_id, "<b>Lium pods</b>\n\nNo pods running — "
                                   f"nothing is burning. Balance "
                                   f"{cp.money(lium.get('remaining'))}.")

    now = _dt.datetime.now(_dt.timezone.utc)
    L = ["<b>Lium pods</b>", ""]
    total = 0.0
    for pid, pod in sorted(pods.items(), key=lambda kv: kv[1].get("name") or ""):
        icon = "🟢" if pod.get("status") == "RUNNING" else "🔴"
        L.append(f"{icon} <b>{_html.escape(str(pod.get('name') or pid))}</b> · "
                 f"{_html.escape(str(pod.get('status')))}")
        bits = []
        if pod.get("gpus") and pod.get("gpu"):
            bits.append(f"{pod['gpus']}× {_html.escape(str(pod['gpu']))}")
        if pod.get("price") is not None:
            bits.append(f"{cp.money(pod['price'])}/h")
        # Uptime and spend are derived at render time, never stored: a stored
        # "65.3h" is wrong the moment it is written.
        try:
            started = _dt.datetime.fromisoformat(pod["created_at"])
            if started.tzinfo is None:
                started = started.replace(tzinfo=_dt.timezone.utc)
            hours = (now - started).total_seconds() / 3600
            bits.append(f"up {hours:.1f}h")
            if pod.get("price") is not None:
                spent = hours * float(pod["price"])
                bits.append(f"spent {cp.money(spent)}")
        except Exception:  # noqa: BLE001
            pass
        L.append("   " + " · ".join(bits))
        if pod.get("removal_at"):
            L.append(f"   ⏳ <b>removal scheduled</b> "
                     f"<code>{_html.escape(str(pod['removal_at']))}</code>")
        if pod.get("status") == "RUNNING" and pod.get("price") is not None:
            total += float(pod["price"])

    L.append("")
    L.append(f"<b>burn</b> {cp.money(total)}/h · balance "
             f"{cp.money(lium.get('remaining'))}")
    run = cp.runway(lium.get("remaining"), lium.get("burn_per_h"))
    if run:
        L.append(f"<b>{run}</b> at this rate")
    await send(chat_id, "\n".join(L))


async def cmd_sn(chat_id: int, arg: str):
    try:
        netuid = int(arg.strip())
    except ValueError:
        return await send(chat_id, "Usage: <code>/sn 64</code>")
    s = await pool().fetchrow(
        "SELECT netuid,name,symbol,price,market_cap_tao,emission_share,realized_tao_per_hour,"
        "num_uids,max_uids,unique_coldkeys,top_coldkey_pct,burn_tao,registration_allowed"
        " FROM subnet_live WHERE netuid=$1", netuid,
    )
    if s is None:
        return await send(chat_id, f"No subnet {netuid}.")
    await send(chat_id, "\n".join([
        f"<b>SN{s['netuid']} {s['name']}</b>",
        f"price τ{_fmt(s['price'],6)}  ·  mcap τ{_fmt(s['market_cap_tao'],0)}",
        f"emission {_fmt((s['emission_share'] or 0)*100)}%  ·  ≈τ{_fmt((s['realized_tao_per_hour'] or 0)*24,1)}/day",
        f"uids {s['num_uids']}/{s['max_uids']}  ·  {s['unique_coldkeys']} operators",
        f"top coldkey {_fmt(s['top_coldkey_pct'],1)}%",
        f"reg {'open' if s['registration_allowed'] else 'closed'} at τ{_fmt(s['burn_tao'],4)}",
    ]))


async def cmd_ck(chat_id: int, arg: str):
    ck = arg.strip()
    if len(ck) < 40:
        return await send(chat_id, "Usage: <code>/ck 5Abc…</code>")
    rows = await pool().fetch(
        f"""SELECT n.netuid, s.name, count(*) uids, sum({TAO_PER_DAY}) tpd
            FROM neuron_live n JOIN subnet_live s USING (netuid)
            WHERE n.coldkey=$1 GROUP BY n.netuid, s.name ORDER BY tpd DESC NULLS LAST""",
        ck,
    )
    if not rows:
        return await send(chat_id, "That coldkey has no registered UIDs.")
    total = sum(r["tpd"] or 0 for r in rows)
    lines = [f"<b>{ck[:10]}…{ck[-6:]}</b>",
             f"τ{_fmt(total)}/day · {sum(r['uids'] for r in rows)} hotkeys · {len(rows)} subnets", ""]
    for r in rows[:20]:
        lines.append(f"SN{r['netuid']} {r['name']} — {r['uids']} uid(s), τ{_fmt(r['tpd'])}/day")
    await send(chat_id, "\n".join(lines))


async def cmd_top(chat_id: int):
    rows = await pool().fetch(
        f"""SELECT n.coldkey, count(DISTINCT n.netuid) nets, count(*) hks, sum({TAO_PER_DAY}) tpd
            FROM neuron_live n JOIN subnet_live s USING (netuid)
            GROUP BY n.coldkey ORDER BY tpd DESC NULLS LAST LIMIT 10"""
    )
    lines = ["<b>Top earners</b>", ""]
    for i, r in enumerate(rows, 1):
        lines.append(f"{i}. {r['coldkey'][:8]}…{r['coldkey'][-4:]} — τ{_fmt(r['tpd'])}/day "
                     f"({r['hks']} hk / {r['nets']} sn)")
    await send(chat_id, "\n".join(lines))


async def cmd_alerts(chat_id: int):
    uid = await _user_for_chat(chat_id)
    if not uid:
        return await send(chat_id, "This chat isn't linked yet.")
    rows = await pool().fetch(
        "SELECT a.netuid, s.name, a.direction, a.threshold, a.active, a.fired_at"
        " FROM price_alert a LEFT JOIN subnet_live s USING (netuid)"
        " WHERE a.user_id=$1 ORDER BY a.id", uid,
    )
    if not rows:
        return await send(chat_id, "No alerts set. Add them on the Market page.")
    lines = ["<b>Your alerts</b>", ""]
    for r in rows:
        state = "fired" if r["fired_at"] else ("armed" if r["active"] else "off")
        lines.append(f"SN{r['netuid']} {r['name'] or ''} {r['direction']} τ{_fmt(r['threshold'],6)} — {state}")
    await send(chat_id, "\n".join(lines))


# Telegram delivers chat housekeeping as a "message" with no text and one of
# these fields set: someone renamed a topic, joined, pinned something. None of
# it is addressed to the bot, and treating it as input made the bot answer a
# topic rename with "Ask me something, e.g. ...".
SERVICE_FIELDS = frozenset({
    "forum_topic_created", "forum_topic_edited", "forum_topic_closed",
    "forum_topic_reopened", "general_forum_topic_hidden",
    "general_forum_topic_unhidden", "new_chat_members", "left_chat_member",
    "new_chat_title", "new_chat_photo", "delete_chat_photo", "pinned_message",
    "group_chat_created", "supergroup_chat_created", "channel_chat_created",
    "message_auto_delete_timer_changed", "migrate_to_chat_id",
    "migrate_from_chat_id", "video_chat_started", "video_chat_ended",
    "video_chat_scheduled", "video_chat_participants_invited",
    "write_access_allowed", "proximity_alert_triggered", "successful_payment",
    "users_shared", "chat_shared", "giveaway_created", "giveaway_completed",
    "boost_added", "chat_background_set",
})


def is_service_message(msg: dict) -> bool:
    return not SERVICE_FIELDS.isdisjoint(msg)


def _question_for_us(msg: dict, text: str) -> tuple[bool, str]:
    """Should we answer this, and what is the question once the mention is stripped?

    Private chat: any plain text. Group or channel: only an @mention of this bot or
    a reply to something it said — which is exactly what Telegram's default privacy
    mode lets a bot see, so no BotFather change is needed.
    """
    chat_type = (msg.get("chat") or {}).get("type", "private")
    if chat_type == "private":
        return True, text

    if BOT_USERNAME:
        mention = f"@{BOT_USERNAME}"
        if mention.lower() in text.lower():
            cleaned = re.sub(re.escape(mention), "", text, flags=re.IGNORECASE).strip()
            return True, cleaned

    replied = msg.get("reply_to_message") or {}
    # In a forum, Telegram threads EVERY message in a topic against the service
    # message that created that topic. When the bot created the topic itself
    # (/setup calls createForumTopic), that root is authored by the bot -- so a
    # naive "is this a reply to me?" makes the entire topic an AI chat and
    # bills every line typed in it. The root's message_id is the thread id.
    if (not replied
            or is_service_message(replied)
            or replied.get("message_id") == msg.get("message_thread_id")):
        return False, text

    author = (replied.get("from") or {})
    if author.get("is_bot") and (author.get("username") or "").lower() == (BOT_USERNAME or "").lower():
        return True, text

    return False, text


def to_telegram_html(text: str) -> str:
    """Model output -> Telegram HTML.

    Escape everything first, then re-introduce only the tags Telegram allows.
    Doing it in this order means a stray < or & in the answer can never break the
    parse, which would otherwise make Telegram reject the whole message.
    """
    out = _html.escape(text, quote=False)
    out = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", out, flags=re.S)
    out = re.sub(r"(?<![\w*])\*(?!\s)([^*\n]+?)(?<!\s)\*(?![\w*])", r"<i>\1</i>", out)
    out = re.sub(r"`([^`\n]+)`", r"<code>\1</code>", out)
    out = re.sub(r"^#{1,6}\s*", "", out, flags=re.M)
    return out


async def answer_question(chat_id: int, question: str, message_id: int | None):
    if not question:
        return await send(chat_id, "Ask me something, e.g. <i>which open subnet has the least competition?</i>")
    await call("sendChatAction", chat_id=chat_id, action="typing")
    user_id = await _user_for_chat(chat_id)
    reply = await ai.ask(question, chat_id, user_id)

    sent = await call("sendMessage", chat_id=chat_id, text=to_telegram_html(reply),
                      parse_mode="HTML", disable_web_page_preview=True,
                      reply_to_message_id=message_id)
    if sent is None:
        # never lose an answer to a formatting problem
        log.warning("HTML send failed for chat %s; retrying as plain text", chat_id)
        await call("sendMessage", chat_id=chat_id, text=reply,
                   disable_web_page_preview=True, reply_to_message_id=message_id)


async def welcome(chat_id: int, members: list[dict]) -> None:
    """Greet people who join, once, with what this chat actually is.

    Names come from the user, so they are escaped -- a display name containing
    "<" would otherwise make Telegram reject the whole message.
    """
    names = [_html.escape(m.get("first_name") or m.get("username") or "there",
                          quote=False)
             for m in members if not m.get("is_bot")]
    if not names:
        return
    who = ", ".join(names[:5]) + (f" +{len(names) - 5}" if len(names) > 5 else "")
    tracked = await comp_commands.tracked_summary()
    await send(chat_id, (
        f"👋 Welcome, <b>{who}</b>!\n\n"
        f"This chat tracks Bittensor subnets — the chain, and each subnet's own "
        f"competition. Every subnet has its own topic; alerts land there by "
        f"themselves.\n\n"
        f"<b>Tracked now</b>\n{tracked}\n\n"
        f"<b>Inside a subnet topic</b> (no arguments needed)\n"
        f"/state — who's winning, and where we stand\n"
        f"/info — rules, caps, registration cost\n"
        f"/board — the leaderboard\n"
        f"/mine — our own runs\n\n"
        f"<b>Anywhere</b>\n"
        f"/sn <code>100</code> — chain snapshot of any subnet\n"
        f"/top — biggest earners on the network\n"
        f"/ask <code>question</code> — ask in plain English\n"
        f"/help · /comphelp"))


async def handle(update: dict):
    # handle() is awaited inside the poll loop, not spawned as a task, so the
    # contextvar persists between updates. Reset it first or a membership
    # greeting inherits the topic of whatever message came before it.
    CURRENT_THREAD.set(0)
    # Channels deliver channel_post, groups and DMs deliver message. Handling only
    # the latter makes the bot look dead in a channel.
    msg = (update.get("message")
           or update.get("edited_message")
           or update.get("channel_post")
           or update.get("edited_channel_post"))

    if not msg:
        # membership changes tell us where the bot has been added
        member = update.get("my_chat_member") or update.get("chat_member")
        if member:
            chat = member.get("chat", {})
            status = (member.get("new_chat_member") or {}).get("status")
            log.info("membership change: chat=%s type=%s title=%r status=%s",
                     chat.get("id"), chat.get("type"), chat.get("title"), status)
            hub.status["telegram_last_chat"] = {
                "chat_id": chat.get("id"), "type": chat.get("type"),
                "title": chat.get("title"), "status": status,
            }
            if status in ("administrator", "member"):
                await send(chat["id"],
                           "👋 TaoScope is connected to this chat.\n\n"
                           "Send <code>/link CODE</code> (get the code from Settings → Telegram) "
                           "to receive alerts here, or /help to see the commands.")
        else:
            log.debug("ignoring update kinds: %s",
                      [k for k in update if k != "update_id"])
        return

    chat = msg.get("chat", {})
    chat_id = chat["id"]
    thread_id = int(msg.get("message_thread_id") or 0)
    CURRENT_THREAD.set(thread_id)
    text = (msg.get("text") or "").strip()
    username = ((msg.get("from") or {}).get("username")
                or chat.get("title") or chat.get("username") or "")

    hub.status["telegram_last_chat"] = {
        "chat_id": chat_id, "type": chat.get("type"),
        "title": chat.get("title") or chat.get("username"), "text": text[:60],
    }
    if "new_chat_members" in msg:
        # The one service message worth answering. A bot joining is already
        # covered by the my_chat_member greeting above, so only greet people.
        await welcome(chat_id, msg["new_chat_members"])
        return
    if is_service_message(msg):
        log.debug("ignoring service message in chat=%s thread=%s: %s",
                  chat_id, thread_id, sorted(SERVICE_FIELDS & msg.keys()))
        return
    if not text:
        # a photo, sticker, poll or forward: nothing to answer
        return

    log.info("telegram msg chat=%s type=%s text=%r", chat_id, chat.get("type"), text[:60])

    if not text.startswith("/"):
        # plain text: answer it if it was addressed to us
        wanted, question = _question_for_us(msg, text)
        if wanted:
            await answer_question(chat_id, question, msg.get("message_id"))
        return

    cmd, _, arg = text.partition(" ")
    cmd = cmd.split("@")[0].lower()

    # Competition commands are topic-aware: the thread decides the subnet, so
    # they resolve before the chain-wide handlers below.
    if await comp_commands.handle(chat_id, thread_id, cmd.lstrip("/"),
                                  arg.strip(), chat.get("type") or ""):
        return

    if cmd in ("/start", "/help"):
        await send(chat_id, HELP)
    elif cmd == "/link":
        await cmd_link(chat_id, arg, username)
    elif cmd == "/me":
        await cmd_me(chat_id)
    elif cmd == "/sn":
        await cmd_sn(chat_id, arg)
    elif cmd == "/ck":
        await cmd_ck(chat_id, arg)
    elif cmd == "/top":
        await cmd_top(chat_id)
    elif cmd == "/keys":
        await cmd_keys(chat_id)
    elif cmd == "/pods":
        await cmd_pods(chat_id)
    elif cmd == "/alerts":
        await cmd_alerts(chat_id)
    elif cmd == "/events":
        await cmd_events(chat_id)
    elif cmd == "/ask":
        await answer_question(chat_id, arg.strip(), msg.get("message_id"))
    else:
        # an unknown slash command in a DM is probably just a question
        if (msg.get("chat") or {}).get("type") == "private":
            await answer_question(chat_id, text.lstrip("/"), msg.get("message_id"))
        else:
            await send(chat_id, "Unknown command. /help")


async def cmd_events(chat_id: int):
    # Inside a bound subnet topic, "recent events" means that competition's
    # events -- not network-wide chain churn the topic is not about.
    scoped = await comp_commands.events_for_topic(chat_id, CURRENT_THREAD.get())
    if scoped is not None:
        return await send(chat_id, scoped)
    uid = await _user_for_chat(chat_id)
    rows = await pool().fetch(
        "SELECT kind, title, body, ts FROM chain_event"
        " WHERE user_id IS NULL OR user_id = $1 ORDER BY ts DESC LIMIT 12",
        uid,
    )
    if not rows:
        return await send(chat_id, "No events recorded yet.")
    lines = ["<b>Recent events</b>", ""]
    for r in rows:
        lines.append(f"{ICON.get(r['kind'], '•')} {r['title']}")
    await send(chat_id, "\n".join(lines))


async def notify_event(ev: dict) -> None:
    """Push one detected event to every chat subscribed to its kind."""
    if not settings.telegram_bot_token:
        return
    kind = ev["kind"]
    text = f"{ICON.get(kind, '•')} <b>{ev['title']}</b>"
    if ev.get("body"):
        text += f"\n{ev['body']}"
    if ev.get("netuid") is not None:
        text += f"\n\n<i>/sn {ev['netuid']} for detail</i>"
    try:
        for chat_id, user_id in await events_mod.subscribers(kind):
            # personal events (deregistrations) go only to their owner
            if ev.get("user_id") is not None and ev["user_id"] != user_id:
                continue
            await send(chat_id, text)
    except Exception:  # noqa: BLE001
        log.exception("notify_event failed")


# ---------------- alert engine ----------------
async def check_alerts():
    """Fire price alerts. Runs alongside the poller."""
    rows = await pool().fetch(
        "SELECT a.id, a.netuid, a.direction, a.threshold, a.user_id, s.price, s.name"
        " FROM price_alert a JOIN subnet_live s USING (netuid)"
        " WHERE a.active AND a.fired_at IS NULL AND a.notify_telegram"
    )
    for a in rows:
        price = float(a["price"] or 0)
        hit = (a["direction"] == "above" and price >= a["threshold"]) or \
              (a["direction"] == "below" and price <= a["threshold"])
        if not hit:
            continue
        chat_id = await pool().fetchval(
            "SELECT chat_id FROM telegram_link WHERE user_id=$1 AND linked_at IS NOT NULL",
            a["user_id"],
        )
        if chat_id:
            await send(chat_id, f"🔔 <b>SN{a['netuid']} {a['name']}</b> is {a['direction']} "
                                f"τ{_fmt(a['threshold'],6)} — now τ{_fmt(price,6)}")
        await pool().execute("UPDATE price_alert SET fired_at=now(), active=false WHERE id=$1", a["id"])


# ---------------- loops ----------------
async def poll_loop():
    if not settings.telegram_bot_token:
        hub.status["telegram"] = "disabled (no bot token)"
        log.info("telegram disabled (no bot token)")
        return
    me = await call("getMe")
    if not me:
        hub.status["telegram"] = "token rejected"
        log.error("telegram token rejected; bot not started")
        return
    global BOT_USERNAME
    BOT_USERNAME = me.get("username")
    hub.status["telegram"] = f"online @{BOT_USERNAME}"
    log.info("telegram bot online: @%s", BOT_USERNAME)

    offset = None
    while True:
        try:
            updates = await call(
                "getUpdates", offset=offset, timeout=50,
                allowed_updates=["message", "edited_message", "channel_post",
                                 "edited_channel_post", "my_chat_member", "chat_member"],
            ) or []
            for u in updates:
                offset = u["update_id"] + 1
                try:
                    await handle(u)
                except Exception:  # noqa: BLE001
                    log.exception("telegram handler failed")
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            log.exception("telegram poll failed")
            await asyncio.sleep(5)


async def alert_loop():
    if not settings.telegram_bot_token:
        return
    while True:
        try:
            await check_alerts()
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            log.exception("alert check failed")
        await asyncio.sleep(60)


def start() -> list[asyncio.Task]:
    return [asyncio.create_task(poll_loop()), asyncio.create_task(alert_loop())]
