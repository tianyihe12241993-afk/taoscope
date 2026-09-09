"""Natural-language Q&A over the platform's own data.

Claude is given read-only tools rather than SQL, so a question can only reach
data the tools expose. Identity comes from a contextvar, never a tool argument.
"""
import logging
import time

from anthropic import AsyncAnthropic

from ..config import settings
from ..db import pool
from ..hub import hub
from . import tools as t

log = logging.getLogger("taoscope.ai")

# Per-model prices are fetched from OpenRouter once and cached, so switching
# TAOSCOPE_AI_MODEL keeps the recorded cost honest instead of billing every model
# at Opus rates.
_PRICE_CACHE: dict[str, tuple[float, float]] = {}
_FALLBACK_PRICE = (5.0, 25.0)  # $/1M in, out

MAX_TOOL_ROUNDS = 8

SYSTEM = """You are TaoScope, an assistant for a Bittensor miner. You answer from live
chain data via your tools — never from memory, and never invent a number.

How to answer:
- Be direct and short. This is a Telegram chat: 2-5 sentences, no preamble, no headers,
  no bullet lists. Plain sentences with a few concrete numbers beat a long explanation.
  You may use **bold** sparingly for a subnet name. Never use tables or code blocks.
- Always ground claims in a tool call. If the tools cannot answer, say so plainly.
- If a field comes back null or absent, say it is unknown. NEVER substitute, round up,
  or infer a number that a tool did not return — a wrong number here costs real money.
  A null "pct_miners_earning" means no miner earns anything, not 100%.
- Prices and rewards are in TAO (τ) unless asked otherwise. Say "τ12.3", not "12.3 TAO".
- Refer to subnets as SN<netuid> plus their name, e.g. "SN64 Chutes".

What matters to a miner, and the traps:
- A "miner" is a UID earning incentive. A validator permit only grants the right to
  set weights — a permitted UID can still earn entirely as a miner, so never describe
  someone as a validator just because they hold a permit.
- Reward figures default to the MINER share. Every subnet emits the same alpha per day;
  ~18% is the owner cut and the rest splits between miners and validators. That split
  miner/validator pools are each half of what reaches UIDs. What varies enormously is
  how many UIDs share the miner half: sometimes hundreds, sometimes ONE. Quote
  miner_tao_per_day for what miners collectively earn, and pct_miners_earning plus
  median_earner_tao_per_day for what one miner can realistically expect.
- A subnet's total reward means little on its own. What matters is reward per rival
  operator, and what share of miners earn ANYTHING — on most subnets only 2-25% of
  non-validator UIDs have any incentive at all. Always mention that share when
  recommending somewhere to mine.
- NEVER recommend on "% of miners earning" alone. It is meaningless without the amount.
  A subnet where 92% earn but the median earner makes τ0.0003/day is a trap, not an
  opportunity — at a τ0.9 entry that is thousands of days to break even. Before naming
  a pick you MUST look at median_earner_tao_per_day AND payback_days together, and say
  both numbers. Prefer a meaningful median earner (roughly τ0.05/day or better) with
  payback measured in days or weeks, not years.
- "payback_days" is how long a median EARNING miner takes to repay the registration
  burn. A subnet where almost nobody earns can still show a short payback; say so.
- Registration being open, free UID slots, and immunity period all affect whether
  someone can actually get in and survive.
- Validators out-earn miners heavily. Keep the two apart when discussing earnings.

Routing — pick the right tool instead of saying you cannot answer:
- "who is top / best / #1 / biggest earner on SN<n>" -> subnet_leaderboard(n).
- "how is SN<n> doing", "tell me about SN<n>" -> get_subnet(n), which also names the
  single coldkey that dominates it.
- "top miners/operators on the network" (no subnet named) -> top_operators.
- "new / upcoming / about-to-launch subnets", "where can I get in early"
  -> prelaunch_subnets (registered but not yet emitting), and screen_subnets with a
  low max_age_days for ones that recently started.
- A bare subnet name with no number -> find_subnet first to get the netuid.
- The user's own miners -> my_positions; it already knows who is asking.

You have a tool for essentially every question about subnets, miners, coldkeys and
prices. Before replying that something cannot be answered, call the tool that looks
closest — an empty result is a real answer, a refusal without trying is not."""


def available() -> bool:
    return bool(settings.openrouter_api_key or settings.anthropic_api_key)


def via_openrouter() -> bool:
    return bool(settings.openrouter_api_key)


def model_id() -> str:
    """OpenRouter namespaces models by vendor; the direct API does not."""
    m = settings.ai_model
    if via_openrouter() and "/" not in m:
        return f"anthropic/{m}"
    if not via_openrouter() and "/" in m:
        return m.split("/", 1)[1]
    return m


def _client() -> AsyncAnthropic:
    """OpenRouter serves the Anthropic Messages API, so the same SDK works
    against it — only the base URL and key change."""
    if via_openrouter():
        return AsyncAnthropic(
            api_key=settings.openrouter_api_key,
            base_url=settings.openrouter_base_url,
            timeout=90.0, max_retries=2,
        )
    return AsyncAnthropic(api_key=settings.anthropic_api_key, timeout=90.0, max_retries=2)


async def _prices() -> tuple[float, float]:
    """($/1M input, $/1M output) for the active model."""
    m = model_id()
    if m in _PRICE_CACHE:
        return _PRICE_CACHE[m]
    if not via_openrouter():
        _PRICE_CACHE[m] = _FALLBACK_PRICE
        return _FALLBACK_PRICE
    try:
        import httpx
        async with httpx.AsyncClient(timeout=15) as cx:
            r = await cx.get(f"{settings.openrouter_base_url}/v1/models")
            for entry in r.json().get("data", []):
                if entry.get("id") == m:
                    pr = entry.get("pricing", {})
                    got = (float(pr.get("prompt", 0)) * 1e6, float(pr.get("completion", 0)) * 1e6)
                    _PRICE_CACHE[m] = got
                    log.info("pricing for %s: $%.2f/$%.2f per 1M", m, *got)
                    return got
    except Exception:  # noqa: BLE001
        log.warning("could not fetch pricing for %s; using fallback", m)
    _PRICE_CACHE[m] = _FALLBACK_PRICE
    return _FALLBACK_PRICE


async def _over_daily_limit(chat_id: int) -> bool:
    used = await pool().fetchval(
        "SELECT count(*) FROM ai_usage WHERE chat_id=$1 AND ts > now() - interval '24 hours'"
        "   AND error IS NULL",
        chat_id,
    )
    return bool(used and used >= settings.ai_daily_limit)


async def ask(question: str, chat_id: int, user_id: int | None) -> str:
    """Answer one question. Returns text ready to send to Telegram."""
    if not available():
        return ("Question answering isn't switched on yet — the server needs an "
                "ANTHROPIC_API_KEY. Commands like /sn 64, /top and /me still work.")
    if await _over_daily_limit(chat_id):
        return f"Daily question limit reached ({settings.ai_daily_limit}). It resets 24h after the first one."

    token = t.current_user_id.set(user_id)
    started = time.monotonic()
    in_tok = out_tok = cache_tok = rounds = 0
    answer, err = "", None

    try:
        runner = _client().beta.messages.tool_runner(
            model=model_id(),
            max_tokens=1500,
            thinking={"type": "adaptive"},
            output_config={"effort": settings.ai_effort},
            system=[{
                "type": "text",
                "text": SYSTEM + f"\n\nCurrent block: {hub.status.get('block')}. "
                                 f"TAO is ${hub.tao_usd:.2f}.",
                "cache_control": {"type": "ephemeral"},
            }],
            tools=t.ALL_TOOLS,
            messages=[{"role": "user", "content": question}],
        )

        async for message in runner:
            rounds += 1
            u = getattr(message, "usage", None)
            if u:
                in_tok += getattr(u, "input_tokens", 0) or 0
                out_tok += getattr(u, "output_tokens", 0) or 0
                cache_tok += getattr(u, "cache_read_input_tokens", 0) or 0
            if message.stop_reason == "refusal":
                answer = "I can't answer that one."
                break
            answer = "".join(
                b.text for b in message.content if getattr(b, "type", None) == "text"
            ).strip() or answer
            if rounds >= MAX_TOOL_ROUNDS:
                log.warning("tool round cap hit for chat %s", chat_id)
                break

        answer = answer or "I couldn't find an answer for that."
    except Exception as exc:  # noqa: BLE001
        err = f"{type(exc).__name__}: {exc}"
        log.exception("ai ask failed")
        answer = "Something went wrong reaching the model. Try again in a moment."
    finally:
        t.current_user_id.reset(token)

    p_in, p_out = await _prices()
    # cached prefix reads bill at roughly a tenth of the input rate
    cost = (in_tok * p_in + out_tok * p_out + cache_tok * p_in * 0.1) / 1e6
    try:
        await pool().execute(
            "INSERT INTO ai_usage (chat_id, user_id, question, answer, input_tokens,"
            " output_tokens, cache_read_tokens, cost_usd, tool_calls, ms, error)"
            " VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11)",
            chat_id, user_id, question[:2000], answer[:4000], in_tok, out_tok,
            cache_tok, cost, rounds, int((time.monotonic() - started) * 1000), err,
        )
    except Exception:  # noqa: BLE001
        log.exception("could not record ai usage")

    return answer
