# TaoScope

Your own Bittensor terminal: every subnet, every miner, every coldkey — pulled
straight from the chain, stored as history, served over an authenticated web UI
that runs 24/7 on this VPS.

Built to replace flipping between taomarketcap and taostats.

---

## Why it reads the chain directly

Everything here comes from two `bittensor` SDK calls against `finney`. No API keys,
no rate limits, no scraping, and nothing is stale by a vendor's cache interval:

| Call | Cadence | Cost | What it gives |
|---|---|---|---|
| `all_subnets()` | every 12s (~1 block) | ~0.7s | price, pool reserves, emission, tempo, on-chain identity |
| `get_all_metagraphs_info()` | every 15 min | ~6-10s | all 30k neurons: hotkeys, coldkeys, emission, stake, dividends, validator permits |

That second call is the whole product in one request — it returns every subnet's
full metagraph at once, which is what makes the coldkey/hotkey view cheap.

---

## Running it

Standing up a **new** host (Docker install, `.env`, first boot, moving the
history over) is its own runbook: **[DEPLOY.md](DEPLOY.md)**. On a host that is
already configured:

```bash
cd taoscope
docker compose up -d          # start everything
docker compose ps             # health
docker compose logs -f backend   # collector output
./ops/taoscope status         # one-shot summary
```

Containers use `restart: unless-stopped` and Docker is enabled at boot, so a VPS
reboot brings the whole stack back by itself. A full `down`/`up` has been tested:
migrations are idempotent and re-run safely, and data survives in named volumes.

**Credentials** live in `.env` (chmod 600). `TAOSCOPE_ADMIN_EMAIL` /
`TAOSCOPE_ADMIN_PASSWORD` create the first account on boot; changing them later
does **not** overwrite an existing account.

### Putting it on a domain (HTTPS)

By default it serves plain HTTP on port 80 at the host's own IP.
To switch to a real hostname with automatic TLS:

1. Point an `A` record at the VPS IP.
2. Set `SITE_ADDRESS=tao.yourdomain.com` in `.env`.
3. `docker compose up -d caddy`

Caddy provisions and renews the Let's Encrypt certificate on its own. Nothing else
changes — the app already talks to `/api` and `/ws` on its own origin, so cookies
and websockets follow the new hostname automatically.

> While the site is on a bare IP it is HTTP-only, so the login password crosses
> the network in the clear. Get the domain on before treating it as private.

---

## What's where

```
backend/          FastAPI + the collector
  app/chain/      SDK client (thread-serialised), transforms, the poll loops
  app/api/        REST + websocket endpoints
  migrations/     idempotent SQL, applied on every boot
frontend/         Next.js 15 app router, hand-rolled SVG charts
ops/Caddyfile     reverse proxy, TLS, security headers
```

### Data model

| Table | Cadence | Purpose |
|---|---|---|
| `subnet_live` | every 12s (upsert) | current state of all 129 subnets |
| `neuron_live` | every 15 min (upsert) | current state of all ~30k neurons |
| `subnet_snapshot` | 60s | price/pool/emission history (hypertable) |
| `neuron_snapshot` | 15 min | per-miner history (hypertable, compressed after 2d) |
| `alpha_candle_1m/1h` | continuous aggregate | OHLC candles, maintained by Timescale |
| `subnet_meta` | you | your links, notes, tags — **overrides chain identity** |
| `coldkey_label` | you | name a coldkey, flag it as yours |

Growth is ~935 MB/day uncompressed; compression kicks in after 2 days and brings
steady state to roughly 2–3 GB/month. There is ~129 GB free, so this runs for
years. To slow it down, raise `TAOSCOPE_NEURON_POLL` in `.env`.

---

## Things the chain taught us (don't re-learn these)

- **`get_all_metagraphs_info()` returns everything at once.** Do not loop
  `metagraph(netuid)` 129 times.
- **Hyperparameters use `u64::MAX` as "disabled".** That value overflows both
  `int` and `bigint`. `to_i64()` stores it as NULL instead of clamping it to a
  number that would read as a real limit. One subnet currently trips this.
- **`moving_price` is a `float` on `DynamicInfo` but a `Balance` on
  `MetagraphInfo`.** Everything goes through `to_f()`.
- **`active` does NOT mean "the miner is running."** It means the UID set weights
  within `activity_cutoff` blocks — verified exactly against `last_update`. On SN64
  that's 13 of 256. The UI says "setting weights" for this reason; calling it
  "active" would imply 243 dead miners, which is false.
- **Instantaneous `tao_in_emission` is noisy** — it swings with where a subnet sits
  in its tempo (SN107 read 13.8% instantaneously vs 4.9% by moving-price share).
  Emission share is computed from `moving_price`, which is the stable measure.
- **Neuron `emission` is alpha per tempo**, so TAO/day is
  `emission × (7200 / tempo) × price`.
- **A validator is a UID that receives dividends, not one that holds a permit.**
  Small subnets hand permits to the top-K by stake, and 250+ permit holders
  network-wide earn incentive with zero dividends — they are miners. No UID
  without a permit has dividends. `app/api/roles.py` is the single definition;
  every UID gets `role` (plus `also_mines` for the 42 validators that also earn
  incentive) and every coldkey reads `miner`, `validator` or `both`. The
  subnet page filters both tables by role, and the screener's top miner is the
  best miner-role hotkey by its own emission, so validator emission never enters.

---

## Sign-in & security

Password login works today. **Google sign-in is built but dormant** — Google rejects
bare IP addresses as OAuth redirect URIs, so it needs a domain first. Settings →
Google sign-in lists the exact steps; once `TAOSCOPE_PUBLIC_URL` and the client
id/secret are set it turns itself on.

Sign-up is **closed**: only addresses in `email_allowlist` may sign in with Google,
managed in Settings or seeded from `TAOSCOPE_ALLOWED_EMAILS`.

What is enforced, and verified by test:

| Control | Behaviour |
|---|---|
| Sessions | Server-side `jti` per login; logout revokes it immediately, so a stolen JWT dies with the session. Revocable per-device in Settings. |
| CSRF | Every mutation needs `X-Requested-With: taoscope`; a cross-site page cannot set it past our CORS policy. Bearer callers are exempt. |
| Brute force | 8 failures per (IP, account) in 15 min → 429, plus a looser per-IP ceiling. **Scoped per account on purpose** — a bare per-IP counter lets anyone spraying a bogus address lock the real user out from the same NAT. |
| id_token | Verified against Google's JWKS with audience + issuer + nonce checks. Never trusted unverified. |
| Audit | Every login, allowlist change and coldkey edit lands in `audit_log`. |
| Cookies | httpOnly + SameSite=Lax; set `TAOSCOPE_COOKIE_SECURE=true` the moment TLS is live. |
| Database | Bound to 127.0.0.1 only; never reachable from the internet. |

> Until a domain is in place the site is HTTP, so the password crosses the network
> in the clear. That is the one materially weak link left.

## Telegram bot — exact setup

The bot uses **long-polling, not webhooks**, specifically so it works with no domain
and no inbound port. Five minutes end to end.

**1. Create the bot** — in Telegram, open [@BotFather](https://t.me/BotFather) and send:

```
/newbot
```

It asks for a display name (anything, e.g. `TaoScope`) then a username, which must
end in `bot` (e.g. `my_taoscope_bot`). It replies with a token like
`8123456789:AAH1a2B3c4D5e6F7g8H9i0J1k2L3m4N5o6P`.

**2. Give the token to the server:**

```bash
cd /home/dev/work/taoscope
nano .env          # set TAOSCOPE_TELEGRAM_BOT_TOKEN=<the token>
docker compose up -d backend
```

**3. Confirm it came up** — this must print `online @your_bot_name`:

```bash
curl -s localhost/api/status | grep telegram
```

If it says `token rejected`, the token was pasted wrong. If it says
`disabled (no bot token)`, the `.env` edit didn't reach the container.

**4. Link your account** — in the web UI go to **Settings → Telegram**, click
**Generate code**, then send this to your bot in Telegram:

```
/link A1B2C3D4
```

It replies "Linked". That's it.

### What it tells you, unprompted

Detected by diffing consecutive chain sweeps — no extra RPC calls. Each kind has a
cooldown so a flapping value can't spam you. Toggle any of them in Settings.

| Event | Fires when | Cooldown |
|---|---|---|
| 🆕 New subnet | A netuid appears on the network | 24h |
| 👑 Top-earner change | The coldkey taking the biggest slice of a subnet changes (and holds ≥8%) | 6h |
| 🚪 Registration flip | A subnet opens or closes registration, with the entry cost | 2h |
| ⚠️ My miners | One of **your** UIDs gets taken over — i.e. you were deregistered | 1h |
| 🔔 Price alert | A threshold you set on the Market page is crossed | per alert |

Deregistration alerts are scoped per user: they only ever reach the account that
registered that coldkey.

### Where the bot listens

Telegram delivers different update types per chat kind, and handling only one makes
the bot look dead everywhere else:

| Chat kind | Update type | Notes |
|---|---|---|
| Direct message | `message` | works out of the box |
| Group | `message` | commands starting with `/` reach the bot even with privacy mode on |
| **Channel** | **`channel_post`** | the bot must be an **administrator** to read or post |

All four are handled, plus `my_chat_member`, so the bot greets a chat the moment it
is added and `/api/status` reports `telegram_last_chat` — useful for confirming the
bot is actually seeing a chat without stealing updates from the poller.

> Do not call `getUpdates` by hand while the backend is running. Long-polling is
> exclusive: a second caller gets a 409 and confirmed updates are consumed and gone.

### When the bot must stay silent

Two traps, both found in production, both now covered by
`devtools/test_silence.py`:

A person joining **is** answered — that is the one service message worth a
reply, and the welcome names the tracked subnets and the commands. A bot joining
is not: `my_chat_member` already covers that.

**Service messages are `message` objects.** Renaming a topic, joining a chat,
pinning something — Telegram delivers each as a `message` with no `text` and one
field like `forum_topic_edited` set. Treated as input, a topic rename made the
bot reply *"Ask me something, e.g. …"*. Anything in `SERVICE_FIELDS`, and
anything with empty text, is now dropped before a handler can react.

**In a forum, every topic message looks like a reply to the topic's root.**
Telegram threads messages in a topic against the `forum_topic_created` service
message. `/setup` calls `createForumTopic`, so that root is authored by **the
bot** — and the ordinary "is this a reply to something I said?" test was
therefore true for every line typed in the topic, quietly turning it into a
billed AI chat. The root's `message_id` equals the thread id, which is what the
check now excludes.

> Neither cost anything here: an empty question short-circuits before the model
> is called, and `ai_usage` confirmed no spend. It would not have stayed that
> way once someone typed a sentence in the topic.

### Asking it questions (Claude)

Set **either** `TAOSCOPE_OPENROUTER_API_KEY` or `TAOSCOPE_ANTHROPIC_API_KEY` and restart
the backend. Without one, every command still works and questions get a polite "not
switched on" reply.

**OpenRouter serves the Anthropic Messages API** at `/api/v1/messages`, so the same SDK
and tool-calling code runs against it unchanged — only the base URL and key differ
(`app/ai/agent.py` → `_client()`). Model ids are namespaced there, so
`claude-opus-5` is sent as `anthropic/claude-opus-5`; `model_id()` adds or strips the
prefix automatically, so `TAOSCOPE_AI_MODEL` accepts either form.

- **Direct chat:** just type a question.
- **Group:** `/ask <question>` works immediately. A plain `@yourbot <question>` does
  **not**, unless you turn privacy mode off (below). Replies to the bot work once it
  has spoken. Anything else in the group is ignored, so chatter costs nothing.

### Detecting a new subnet

Two distinct events, and the second is the one that pays:

| Event | Signal | Detection latency |
|---|---|---|
| **Registered** — a netuid is created (costs ~τ588 today) | the netuid appears in `all_subnets()` | **≤12s** (fast tick) |
| **Started** — the owner makes the start call and emissions begin | `alpha_out_emission` flips 0 → 1 | **≤12s** |

A registered subnet pays **nothing** until it is started, and the gap can be weeks:
SN86 kaunan sat registered 25 days with 139 UIDs taken and no emissions. That gap is
the opportunity — a UID there costs τ0.0005 with 117 slots free, and it earns from the
moment the subnet switches on.

`alpha_out_emission > 0` was validated as **exactly equivalent** to the chain's
`is_subnet_active(netuid)` — zero mismatches across all 128 subnets — so activation is
derived from the data already fetched every 12s rather than 128 RPC calls costing ~26s.

Both fire Telegram alerts (`new_subnet`, `subnet_started`), appear in the activity feed,
and are queryable: `GET /api/prelaunch`, the **Pre-launch** screener preset, a
`pre-launch` pill in the table, and the `prelaunch_subnets` tool for the bot.

> Detection is only as fresh as the process being up. A subnet that starts while the
> collector is down is picked up on the next boot, but reported by its `started_at`
> stamp rather than in real time.

### Group privacy mode — the thing that silently blocks @mentions

Telegram bots default to **privacy mode ON**, and a bot in that mode only receives:

- messages starting with `/` (`/ask …`, `/sn 64`, `/ask@yourbot …`)
- replies to its own messages
- service messages

A plain `@yourbot what should I mine?` is **not delivered at all** — the bot never sees
it, so it cannot answer and nothing appears in the logs. Check with:

```bash
curl -s "https://api.telegram.org/bot$TOKEN/getMe" | grep can_read_all_group_messages
```

`false` means privacy is on. To allow bare @mentions: BotFather → `/setprivacy` →
pick the bot → **Disable**, then remove and re-add the bot to the group for it to take
effect. Otherwise just use `/ask`.

The model gets **read-only tools**, not SQL: `network_summary`, `find_subnet`,
`get_subnet`, `subnet_leaderboard`, `screen_subnets`, `get_coldkey`, `top_operators`,
`my_positions`, `recent_events`, `quote_order` (see `app/ai/tools.py`).

`subnet_leaderboard(netuid)` answers "who is top on SN<n>" — it ranks the UIDs inside
one subnet and tags each `miner` or `validator`, which matters because validators
out-earn miners through dividends and would otherwise be reported as the top "miner".
The system prompt carries an explicit routing table (which question maps to which
tool) plus an instruction to try the closest tool before ever saying something cannot
be answered — without it a cheaper model will decline a question the tools can
answer. A question can therefore only
reach what a tool exposes and can never mutate anything. Identity for `my_positions`
comes from a **contextvar, never a tool argument** — so the model cannot be talked
into reading another account's positions.

The system prompt carries the mining rules that matter (reward per rival, the share of
miners that earn anything, what payback_days really means), so answers reflect how this
platform reads the data rather than generic chain trivia.

Measured cost per question (same question, same tools, real runs):

| Model | Cost | Latency | Verdict |
|---|---|---|---|
| `google/gemini-2.5-flash` **(default)** | **$0.0038** | ~12s | correct once the prompt was hardened |
| `anthropic/claude-sonnet-5` | $0.0065 | ~9s | most consistently correct — the safe upgrade |
| `anthropic/claude-haiku-4.5` | $0.0083 | ~8s | fine |
| `openai/gpt-5.1` | $0.0155 | ~20s | correct, slower, more tool calls |
| `anthropic/claude-opus-5` | $0.030 | ~14s | best reasoning, 8x the price |

Switching is one line: `TAOSCOPE_AI_MODEL=` in `.env`, then
`docker compose up -d backend`. Cost is computed from OpenRouter's live per-model
pricing, so the recorded spend stays honest when you change models.

**Why the prompt is defensive.** The cheaper models initially failed in two specific
ways, both caught by checking their answers against the database: one recommended a
subnet on "92% of miners earn" while the median earner made **τ0.00034/day** (a
~2600-day payback), and one reported "100.0% earning" for a subnet where **0 of 242**
miners earn anything. The system prompt now forbids recommending on percentage alone
without the median earner amount and payback, and forbids substituting a number for a
null field. After that change all the cheap models gave sound answers — so the fix was
prompt engineering, not paying 8x for a bigger model.

Capped by `TAOSCOPE_AI_DAILY_LIMIT`
per chat per 24h. Every call is logged to `ai_usage` with tokens, latency and dollar
cost; Settings -> Ask the bot shows 24h and 30d spend. Tune `TAOSCOPE_AI_MODEL` /
`TAOSCOPE_AI_EFFORT` to trade cost for depth.

**Telegram formatting:** the model may emit `**markdown**`, but messages are sent with
`parse_mode=HTML`. `to_telegram_html()` escapes the answer *first*, then re-introduces
only the tags Telegram allows — so a stray `<` or `&` can never break the parse and
cause Telegram to reject the whole message. If an HTML send still fails, the answer is
resent as plain text rather than lost.

### Commands

```
/me       your coldkeys, hotkeys and daily earnings
/sn 64    subnet snapshot
/ck 5Abc… any coldkey's footprint
/top      biggest earners on the network
/ask ...  ask anything in plain English
/events   the latest network events
/alerts   your active price alerts
/keys     API credit per provider, checked live when you ask (alerts still
          come from the 15-min poller; both share one sweep)
/help
```

---

## Competition tracking (per-subnet, off-chain)

The chain tables say what a subnet *emits*. They cannot say who is winning its
competition, what this week's rules are, or whether our submission is queued or
running — all of that lives in each subnet's own dashboard, in a shape that
subnet invented. That layer lives in `app/comp/`.

### The topology, and why it is one bot

**One bot. One forum supergroup. One topic per subnet. One adapter per subnet.**

The mechanism that makes this work is `message_thread_id`: every message in a
forum carries the topic it was sent in, so `/state` typed in the SN100 topic is
*already* an SN100 command. No arguments, no `@botname` suffix, no second bot.

Running a bot per subnet looks tempting and is worse:

| | one bot + topics | one bot per subnet |
|---|---|---|
| `/state` in a topic | unambiguous — the thread names the subnet | **every** bot in the group receives **every** `/command` (privacy mode only filters non-slash text), so you get N replies or must type `/state@sn100_bot` |
| Long-polling | one exclusive loop | N loops, N tokens, N deployments |
| Shared logic | cooldowns, dedup, HTML escaping, AI — written once | forked N ways |
| Rate limit | 20 msg/min **per group**, shared | 20 msg/min per bot — the one genuine advantage |

That last row is the only real argument for splitting, and coalescing events per
poll (which you want anyway, for noise) keeps you far under it. Because the
transport is abstracted behind `comp/router.py`, adding a second token later is a
config change rather than a redesign.

Channels were the other option and are worse: broadcast-only, no per-topic mute,
and commands in a channel are awkward at best.

### Wiring it up

The bot must be an **administrator with Manage Topics**. Then, in the group:

```
/setup          creates and binds a topic per tracked subnet, and makes the
                topic you ran it in the cross-subnet digest
```

After that, **topics look after themselves**. Every 5 minutes the competition
supervisor checks which subnets our coldkeys (`my_coldkey`) hold a UID on and
creates any missing topic (`TAOSCOPE_TELEGRAM_AUTO_TOPICS`, default on). It only
ever adds: a subnet we get deregistered from keeps its topic, and a topic you
delete or `/unbind` is remembered in `comp_topic_skip` and not recreated until
you run `/setup` or `/bind <netuid>`. A deleted topic is noticed on the first
send that fails with `message thread not found`: its binding is removed once,
General is told, and that alert is delivered to General instead.

Or bind topics you already have, one command inside each:

```
/bind 100       this topic is SN100 (any netuid on chain, adapter or not)
/bind digest    this topic is the cross-subnet summary
/topics         every binding, plus held subnets that have no topic
/unbind         stop routing here, and stop auto-creating this subnet's topic
```

### The standard topic

Every subnet topic is the same shape, whether the subnet has a hand-written
adapter or only the chain view (`app/comp/chain_adapter.py`, used for any
subnet we hold a UID on with no adapter):

| | every topic |
|---|---|
| name | `SN<netuid> · <label>` |
| first message | the subnet's `/guide`, pinned |
| `/state` | the adapter's view, then the shared `⛓ CHAIN` block and one `🔑 OURS` line (uids · earning · τ/day · best rank) |
| `/mine` | the adapter's view, then the same `🔑 OUR HOTKEYS` table (uid, hotkey, rank, τ/day, immune, validator permit) |
| `/board` | the adapter's leaderboard, or the top earners by emission on chain |
| `/mute` with no kind | the kinds this topic can actually receive |
| alerts | the adapter's own, plus `registration`, `operators`, `our_earning` from the poller and `my_miners` (deregistration) and `king_change` from the chain sweep |

Chain events are routed like competition events: an event about a subnet goes to
that subnet's topic when one exists, otherwise to General. An adapter that reports
deregistration itself declares `covers = frozenset({"dereg"})` (SN62, SN67, SN114)
so the generic alert does not arrive twice. Severity decides whether the phone
buzzes for chain events too.

Messages longer than Telegram's 4096 visible characters are split at paragraph or
line boundaries, with any open `<pre>` closed and reopened across the cut; before
this, SN62's `/mine` (≈6,700 characters) was rejected and lost.

The `/` menu is registered at startup (`setMyCommands`) with one list for groups
and one for private chats.

### Commands — no arguments needed inside a bound topic

```
/state     crown, emission split, field size, queue, our runs, our UIDs
/info      rules, caps, registration cost, operators, watched repos
/board     leaderboard
/mine      our submissions in detail, then every hotkey we hold here
/events    what changed here recently
/guide     post and pin this subnet's primer
/watch <submission_id> [label]   ·  /unwatch <id>
/poll      force a refresh now
/mute <kind> · /unmute <kind>
/help      in a subnet topic: these commands; in General: everything
```

Every one also takes an explicit netuid (`/state 100`), so they work in a DM or
an unbound topic.

### What arrives unprompted

Every alert has the same shape, because a wall of alerts is only readable when
they all read the same way:

```
<icon> HEADLINE          what happened, in three words
old → new                the change itself, never implied
context                  the numbers you would look up next
action                   only when there is one, in italics
```

Exactly **one** icon, and the adapter picks it (`CompEvent.icon`) when it knows
something better than the severity — a pipeline stage, a crown, a repo. Titles
must never carry their own icon or the two collide and the message opens `• •`.

Severity decides only whether your phone buzzes: `info`/`good` are sent
silently, `warn`/`critical` are not. Routine board churn must never make a
noise, because a monitor you mute is worth nothing.

| Event | Severity | Fires when |
|---|---|---|
| `rules` | 🚨 critical | competition id, param band, budget caps, recipe pin or dataset changed — every held patch may be invalid |
| `repo` | 🚨 critical | a watched GitHub repo moved, with the compare link |
| `emission` | 🚨 critical | the arena split or burn share changed |
| `king_change` | 🚨 critical | a new hotkey holds rank 1 |
| `our_run` | ⚠️/✅ | one of **our** submissions changed **phase** (see below) — infra-class failures say so explicitly, because those re-open the slot for 30 minutes and a rejection never does |
| `registration` | ⚠️ | registration opened or closed, with the entry cost |
| `sealed` | ⚠️ | the set of hotkeys actually being paid changed |
| `new_entrant` | • info | new names on the board |
| `king_score` | • info | rank 1 held, score moved |
| `queue` | • info | the line **emptied**, went from empty to occupied, or moved by 3+ — not every ±1. Every number is labelled |

### `stage` is not progress — the phase model

The single most misleading field this platform exposes. A Prism submission loops

```
similarity → llm_review → scoring → provisioning → failed(install) → queued(rate_limit_requeue)
```

roughly every 30 seconds while it waits for a GPU, so `stage` reads **"scoring"**
on a run that has never touched a B200. Measured on our own `58ceb802`:

```
4,168 events · 596 complete cycles · 4.7 hours
distinct pod ids: NONE      heartbeats: 0
591 × "B200s are currently out of capacity on Lium"
```

Reporting `stage` therefore meant an alert every poll that said nothing, and a
`/state` that claimed we were being scored when we were not. The adapter now
derives a **phase** from what actually happened:

| Phase | Derived from | Reads as |
|---|---|---|
| `running` | a `pod_id` or a heartbeat exists | ▶️ training on a GPU |
| `done` | `terminated` | 🏁 finished |
| `dead` | last event is `failed`/`rejected` | 💥 died |
| `waiting` | none of the above | ⏳ waiting for a GPU |

`_diff_ours()` keys on `(phase, class, blocked-reason)` — never on stage — which
collapses 596 cycles into one event. `/mine` shows the retry count and whether a
pod was ever allocated, so "waiting" can never be mistaken for "working".

The same distinction runs through the field counts: **"running" in the platform's
submission list does not mean on a GPU**, so `/state` reports three separate
numbers — `waiting` (in line), `in admission` (looping through the gates), and
`on a GPU` (the only one that spends budget).

A run that is stuck and a run that died are asking different questions, so each
shows only its own reason: `waiting` shows the queue note, `dead` shows the
error that killed it. Showing the stale queue note on a dead run claimed a
capacity problem for a submission that actually failed on `install_deps`.

### Adding a subnet

One file. **`docs/ADDING_A_SUBNET.md` is the full guide**; the short version:

```bash
python devtools/new_adapter.py 85 vidaio "Vidaio"   # scaffold from _template.py
# fill in snapshot() and diff(), register it in adapters/__init__.py
docker compose build backend
docker compose run --rm -T -v $PWD/backend/devtools:/srv/devtools:ro \
  backend python devtools/smoke_comp.py 85          # no Telegram, nothing posted
docker compose up -d backend
# then in the group:  /bind 85   (inside the topic)
```

`app/comp/adapters/_template.py` is a working, commented skeleton;
`app/comp/adapters/sn100.py` is the same shape filled in against a real,
awkward API. Two adapters have their own write-ups because their platforms
lie in specific ways worth reading before you meet them again:
**`docs/SN98_ADAPTER.md`** and **`docs/SN67_ADAPTER.md`** (MCP JSON-RPC rather
than REST; an endpoint that cannot see its own open window).

`smoke_comp.py <netuid>` is the gate. It fails loudly if the snapshot is empty,
a renderer emits HTML Telegram would reject, a cold start would spam the chat,
or an API outage would look like a change.

The framework gives every adapter, for free: polling with failure backoff,
restart-safe diffing, cooldown/dedup, topic routing, the GitHub repo watcher,
registration/burn events from our own chain tables, and every command above.

### Operational notes

- State is diffed against the **stored** snapshot (`comp_state`), not an
  in-memory one, so a restart mid-competition does not re-announce old news.
- Each subnet polls in its own task: a dead dashboard on one subnet cannot stall
  the others, and repeated failures back off to 15 minutes rather than hammering.
- GitHub is polled with the stored **ETag**, which saves the body — a commit
  list is 277 KB or 0. It does **not** save budget: measured against
  api.github.com on 2026-08-20, three consecutive conditional reads that all
  returned `304` still took `x-ratelimit-used` 31 → 34. GitHub's docs say
  otherwise; the header is the authority. The only real remedies are fewer
  requests or a token.
- **`TAOSCOPE_GITHUB_TOKEN` is not optional any more — set it.** Eight adapters
  now watch **14 repos** (SN62 added `ridgesai/ridges` on 2026-08-21, SN15 added
  `ORO-AI/oro` on 2026-08-24), and the repo poll runs every 600s, so the *floor*
  is 84 requests/hour against a 60/hour per-IP unauthenticated cap. Measured
  2026-08-20 19:37Z: `HTTP 403 (0)` on every repo, `harnyx/harnyx` included, and
  the watcher stayed blind for the rest of the hour. A token takes it to
  5000/hour. Any PAT with public-repo read scope is enough.
  When exhausted, `github.py` now **parks until the reset the 403 itself names**
  rather than re-hammering — one warning per window instead of twelve, and the
  budget is allowed to recover. `blocked_for()` reports the remaining park.
  > Careful reading the budget: `GET /rate_limit` is served from the edge and
  > can return a **stale** `remaining` (it read 57/60 while the very same IP was
  > 403ing). Trust the headers on a real request, not that endpoint.
- `/home/dev/work` is mounted **read-only** at `/work`, so the SN100 adapter
  reads the submission ids the miner tooling already writes to
  `base-intel/artifacts/live_submissions.json`. Nothing is typed in twice, and
  the web app can never touch a run.
- Smoke tests run without touching Telegram or the live chat:
  ```bash
  D="-v $PWD/backend/devtools:/srv/devtools:ro"
  docker compose run --rm -T $D backend python devtools/smoke_comp.py 100
  docker compose run --rm -T $D backend python devtools/smoke_commands.py
  docker compose run --rm --no-deps -T $D backend python devtools/test_clean_error.py
  docker compose run --rm -T $D backend python devtools/preview_alerts.py 100
  docker compose run --rm -T $D backend python devtools/test_silence.py
  ```

## Pages

| Page | What it's for |
|---|---|
| **Subnets** (`/`) | Every subnet plus the screener in one view. The filter drawer is hidden by default; open it for presets and sliders. |
| **Operators** | Coldkey leaderboard with **Miners / Validators / All** tabs. Miners is the default: validators out-earn miners so heavily that a combined list buries them. Earnings are split per role, so a coldkey doing both appears in both, ranked by the income that tab is about. |
| **My work** | What your own hotkeys earn, with an earnings curve, per-subnet split and every hotkey. |
| **Market** | Prices, pool depth, slippage quotes. |

## The screener (built into Subnets)

Raw chain fields don't answer *"is this worth mining"*, so two derived metrics do —
both computed in `app/api/screen.py`:

- **`pct_miners_earning`** — the headline. On most subnets only 2–25% of
  miner UIDs earn **anything**; on SN3 and SN4 it is 5 of ~250. Verified
  against `incentive` (a normalised score that does not drain each tempo), so it
  is not a sampling artifact. A big emission share is worthless if you land in the
  95% earning zero. Validators are excluded from this and from the top-miner
  figure, where a validator is a permit holder that actually receives dividends —
  a permit alone is not enough, 250+ permit holders earn purely as miners.
- **`payback_days`** — registration burn ÷ what a *median earning* miner makes per
  day. How long a UID takes to repay itself.
- **`reward_per_operator`** — daily reward ÷ distinct competing coldkeys.

Presets ("Good for miners", "Uncontested", "New subnets", "Cheap entry", "Room to
join", "Where I mine") encode the questions rather than raw column values. Filters
are client-side over 128 rows so the sliders respond instantly; saved filters persist.

**Columns** (the button beside Filters) picks and orders the table's columns —
drag a header, or a row in the picker — and the choice is kept in the browser.
Each subnet shows its on-chain logo, or a coloured initial when none is published.

**Submission** shows the current submission window for every subnet that has a
competition adapter (`app/comp/windows.py` folds each platform's own
round/race/batch/set vocabulary into one shape): `open · closes in 21h`,
`eval · ends in 12h`, `rolling` (submit any time, e.g. Prism's one-shot-per-hotkey
model) or `closed`. Hover for the round name, the platform-specific meaning and
the exact UTC times. Subnets without an adapter show a dash. Tempo and immunity
period are available as optional columns for the on-chain periods. The default set leads with **Reg** — the current
registration burn in the active currency, with a red `closed` marker when
registration is off. The alpha price is an optional column. Hover a subnet name
for its on-chain description. The `τ TAO / $ USD` switch in the toolbar (and in
the header) flips every monetary column at once.

### One trap in the history maths

A backend restart triggers an immediate neuron sweep, so a single 15-minute bucket
can hold two or three complete snapshots. Summing the rows in a bucket therefore
double- or triple-counts earnings (seen live: τ4/day reported as τ522/day). Every
query over `neuron_snapshot` must de-duplicate to the latest row per UID per bucket
— see the `dedup` CTE in `/api/me/history`.

Note also that a miner's `emission` genuinely swings between sweeps. That is **not**
a sampling artifact: `incentive`, which does not drain, moves with it (verified: 3.0
↔ 0.020 then 132.7 ↔ 0.899 on the same UID). Those steps are real score changes.

## Alpha trading (requirement 6 — deliberately not wired to a wallet yet)

The *data and pricing* for trading are already live; only signing is missing.

- `GET /api/market/quote/{netuid}?amount=&side=` — constant-product quote against
  live pool reserves. Same curve the chain uses, so the number matches what an
  order would really pay.
- `GET /api/market/depth/{netuid}` — slippage ladder from 1 to 1000 TAO.
- `GET /api/market/candles/{netuid}?interval=1m|1h` — OHLC from stored history.
- `GET /api/market/movers?window=1h|24h|7d`
- Tables `watchlist`, `price_alert`, `paper_position` exist and are ready.

Wallet linking has its schema and challenge flow in place (`wallet_link`,
`POST /api/me/wallets/challenge`); `/verify` returns 501 until sr25519 verification
lands. To go live, the only new piece is a signing service holding a wallet and
calling `add_stake` / `unstake` (`swap_stake` for subnet-to-subnet). Keep it as a
**separate container with its own key** so the public web app never holds a key.

---

## API quick reference

All routes require auth (cookie or `Authorization: Bearer`), except `/api/status`.

```
POST /api/auth/login                {email, password}
GET  /api/subnets                   all subnets, chain identity merged with your meta
GET  /api/subnets/{n}               one subnet
GET  /api/subnets/{n}/history       60s series
GET  /api/subnets/{n}/miners        every UID, sorted
GET  /api/subnets/{n}/coldkeys      emission grouped by operator
PUT  /api/subnets/{n}/meta          your links/notes (partial update)
GET  /api/screen                    screener rows with the derived metrics
GET  /api/competition               concentration + entry cost, all subnets
GET  /api/coldkeys/top              network-wide operator leaderboard
GET  /api/coldkeys/{ss58}           one operator's whole footprint
GET  /api/hotkeys/{ss58}
GET  /api/search?q=
GET  /api/network                   totals
GET  /api/status                    liveness (no auth)
GET  /api/me/portfolio              your coldkeys -> every hotkey behind them
POST /api/me/coldkeys               register a coldkey
GET  /api/me/telegram               bot link status + code
GET  /api/auth/allowlist            who may sign in (admin)
WS   /ws?token=                     live push every block

Every mutation requires the `X-Requested-With: taoscope` header.
```
