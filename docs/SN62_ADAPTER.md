# SN62 adapter — what it does and what it cannot

`backend/app/comp/adapters/sn62.py`. Built 2026-08-21 from what had been an unfilled template
copy (it still pointed at `example-subnet-dashboard.ai` and was not registered).

Ridges runs a **continuous queue, not rounds**. There is no submission window and no deadline, so
the countdown that dominates most adapters does not exist here. What replaces it is the
**qualify bar** — and unlike a round boundary, the bar can move at any moment.

## What the subnet actually pays for

Emission is **not** rank-based (`utils/incentives.py`). An agent banks only by beating the
*current leader* on one of two routes:

| route | condition |
| --- | --- |
| performance | `(you − leader) / leader ≥ perf_threshold` (0.03) |
| cost | `score ≥ leader` **and** `(leader_cost − your_cost) / leader_cost ≥ cost_threshold` (0.06) |

What it banks is then multiplied by `1 + √(hours_since_last_approval / 12)` and decays with a
**336 h half-life**. Two consequences the renderers are built around:

* You do not need rank 1 to earn — you need to keep **landing improvements**. `/board` therefore
  always carries the bar, not just the standings.
* **A stall is worth money.** 12 h with nobody approved is 2.0x, 48 h is 3.0x, 7 d is 4.7x for
  whoever breaks it — and it resets to 1.0x the instant anyone is approved. So `stall` (crossing
  upward) and `approved` (the reset) are both first-class alerts.

## API traps encoded here

| trap | reality |
| --- | --- |
| **rate limiting** | **three requests in a burst is enough to get a 429**, there is no `Retry-After` and no `x-ratelimit-*`, and even 2 s apart a request 429s roughly half the time. `base.fetch_json` maps every non-200 to `None`, so an adapter obeying "omit the key on failure" goes **silently blind** on whichever endpoints land inside the throttle. See below. |
| host | `www.ridges.ai` / `api.ridges.ai` are Cloudflare-blocked from this box; `agent-upload.ridges.ai` serves the identical read API unauthenticated |
| `/evaluation-sets/{id}/overview` | Cloudflare-challenged **even on `agent-upload`** — returns an HTML interstitial, not JSON. Do not add it |
| two status fields | a row carries both `status` (`AgentStatus`) and `competition_state.status` (`AgentCompetitionStatus`). Only the second distinguishes `approved` from `didnt_qualify` from `finished` |
| `finished` | means **evaluated, not earning**. `didnt_qualify` is a scored agent that banked nothing. Only `approved` banks |
| `rank` | non-null on ~21 of 170 rows; every unscored agent has `rank: null` |
| `pass_rate` in the funnel | **cumulative survival from `total`**, not the stage's own pass rate. `screener_1` reads 0.39 when 67 of the 116 agents that reached it — 58% — got through |
| `/upload/eval-pricing` | priced in USD and quoted in alpha, so it **drifts continuously** (2.1172 → 2.0637 α in 40 minutes, measured) |
| `/validator/connected-validators-info` | returns `[]` unauthenticated. Not evidence that validators are down — not used |
| `/retrieval/evaluations-for-agent` | 200 KB for one agent (it embeds every patch). Never poll it |
| identity | agents are keyed by **hotkey or agent_id**, never uid — the platform does not use uids for agents at all |

### The throttle, and why it is the trap that matters

The first version of this adapter passed `smoke_comp.py` with **ALL CHECKS PASSED** while the
leader, the funnel, the upload price and the entire leaderboard were simply missing from the
snapshot — they were requests 4 through 8 of a burst of 8. Every check was green because every
check tests behaviour *given* a snapshot, and a half-empty snapshot is a legal snapshot.

So SN62 does not use `base.fetch_json`. `_get()` paces calls 2.5 s apart behind a lock, retries a
429 three times with backoff, and returns `None` **only** on a real failure. On top of that:

* the three slow-moving sources (thresholds, price, weights) are cached for 30 min by `_slow()`
* the 220 KB leaderboard is fetched only when the 1 KB set summary says the field moved
* the 80 KB problem list is fetched once per evaluation set

which keeps a routine poll to three requests.

## The repo watch

`ridgesai/ridges` is watched by the framework, which already announces *that* it moved and links
the compare. `_repo_impact()` adds the only thing that decides whether to stop and read it:
**which** of the files that define the competition were in the diff — `utils/incentives.py`
(the reward formula), `inference_gateway/` (the model allowlist and per-run USD budget),
`ridges_harbor/` (how a patch is applied and gated), `validator/`, `execution/`, `miners/`.

A commit touching none of them stays silent — one alert is enough. A commit touching any of them
fires `repo_impact` (critical) naming the paths and why each matters.

It costs **one GitHub request per real commit and none otherwise**: it reads the sha the
framework's watcher already stored and only calls `/compare` when that sha moved. That is
deliberate — the watcher is already over the 60/hour unauthenticated budget.

`ridgesai/ridges-bench` (the verifier gates) is the natural second watch and is **not** added,
because each repo costs 6 requests an hour whether or not it moved. Add it once
`TAOSCOPE_GITHUB_TOKEN` is set.

## Alerts

| kind | severity | fires when |
| --- | --- | --- |
| `rules` | critical | a new evaluation set opened, or `perf`/`cost`/screener thresholds moved |
| `repo_impact` | critical | a commit touched a rules-defining file |
| `king_change` | critical | the leader changed — carries the new bar |
| `bar` | warn | same leader, new score or cost |
| `approved` | warn | someone banked — **the stall multiplier just reset** |
| `price` | warn | the upload burn moved ≥10% from the last announced quote |
| `stall` | good | the stall multiplier crossed 2.0 / 2.5 / 3.0 / 4.0x upward |
| `threat` | info | a rival reached the validator stage (past both 0.76 screeners) |
| `field` | info | new agents reached a score |
| `weights` | info | the set of hotkeys the chain pays changed |
| `our_run` | info / good / warn | one of our agents was accepted, or changed pipeline stage |
| `dereg` | warn | a hotkey of ours left the metagraph — whatever it submitted still scores and earns nothing |

Plus the generic `registration` / `operators` / `repo`.

**Deliberately not alerted:** queue wait times, screener/validator average scores, agent-count
growth, and the weight *values*. All drift every poll and none of them changes a decision;
they are rendered in `/info` where they cost nothing to ignore.

## Continuous values, and how each is tamed

Three fields would otherwise fire an event every single poll:

| field | tamed by |
| --- | --- |
| `time_multiplier` | `tm_mark` — bucketed to the marks 2.0 / 2.5 / 3.0 / 4.0, upward crossings only |
| `upload_alpha` | `upload_ref` — 10% hysteresis, not rounding, so a quote sitting on a boundary cannot flap |
| queue waits, averages | not diffed at all |

## Colour on `/board`

Telegram's HTML has no colour markup at all. The only place a client paints text is inside a
syntax-highlighted code block, so `/board` is emitted through `base.diff_block()` as
`<pre><code class="language-diff">`, where the **first character of each line** is what gets
coloured: `+` green (ours), `-` red, space plain. `router.BANNER` already used the same three
characters for event severity — the meanings are kept aligned.

The marker is a real character in a real column, not styling, and that is deliberate: not every
client highlights, so on the ones that do not the board is still ordinary aligned monospace with
our rows marked. Nothing is lost when the colour is.

Two things the colour alone would not have fixed:

* **Rows are matched on hotkey *or* agent_id.** `agent_id` is one upload; the hotkey is us across
  every version. Matching only on `agent_id` would leave an earlier version of ours on the ladder
  unmarked *and* report the agent as missing.
* **An agent still in the pipeline has no score and therefore no row.** It is listed under the
  table with its stage instead of vanishing — including when *nothing* in the set has been scored
  yet, which is exactly when ours is in flight.

Agent names are miner-controlled free text and are `esc()`'d before entering the block. A `<pre>`
is still HTML-parsed: one rival named with a stray `<` and Telegram rejects the whole message,
so the board would go blank rather than mis-render one row. `check_sn62_board.py` covers all of it.

## Our own agents — `/mine`

Identity comes from the **chain**, not from a list anybody maintains:

```
my_coldkey (or SN62_COLDKEYS)          our coldkeys
  -> neuron_live WHERE netuid=62       the hotkeys they have registered here
     -> /retrieval/agents-by-coldkey   every agent those hotkeys uploaded
```

A hotkey registered five minutes ago is tracked with nothing typed in, a recycled uid can never
make us report a stranger's agent as ours, and it is **cheaper** than the watchlist it replaced:
`agents-by-coldkey` returns `{hotkey: [agent, ...]}` for every hotkey under a coldkey in **one**
request, where per-hotkey lookups would have been 23 against an API that throttles at one every
few seconds.

Only the **current set** is listed. A set is a different competition; a set-26 agent says nothing
about where we stand today.

### Three states that must never be blurred

| state | what it means | why it is separate |
| --- | --- | --- |
| registered + idle | capacity we hold and are not using | every upload burns alpha, so an idle registered hotkey is a budget line |
| submitted + finished | a burned attempt | the stage says *why* it died — `REJECTED by the hardcoding judge` is not the same problem as `failed screener 1` |
| submitted + **not** registered | scores normally, **earns nothing** | the quiet one: scoring and earning are separate gates and the dashboard shows no sign of it |

And a fourth that must never be *inferred*: `_registered()` returns `None` when the metagraph has
no rows for this netuid, which is **unknown**, not "we own nothing". Conflating them would report
every hotkey we hold as deregistered the first time the chain poller missed a beat — so `/mine`
prints *metagraph unavailable* and the `dereg` alert cannot fire.

### As of 2026-08-21

1 coldkey, **23 hotkeys registered** on netuid 62, **5 uploads to set 27 — all dead**: three
`failed_pre_screening` (the LLM hardcoding judge), one `failed_screening_1`, one `cancelled`.
Nothing of ours has ever reached a validator in this set. 18 registered hotkeys are idle.

## Validating a change

```bash
docker compose build backend                       # devtools run INSIDE the image
docker compose run --rm -T -v "$PWD/backend/devtools:/srv/devtools:ro" \
  backend python devtools/smoke_comp.py 62         # the five production invariants
docker compose run --rm -T -v "$PWD/backend/devtools:/srv/devtools:ro" \
  backend python devtools/check_sn62_paths.py      # the paths perturb() cannot reach
docker compose run --rm -T -v "$PWD/backend/devtools:/srv/devtools:ro" \
  backend python devtools/preview_views.py 62      # how it reads on a phone
docker compose run --rm -T -v "$PWD/backend/devtools:/srv/devtools:ro" \
  backend python devtools/check_sn62_board.py      # colour, escaping, unplaced agents
docker compose run --rm -T -v "$PWD/backend/devtools:/srv/devtools:ro" \
  backend python devtools/check_sn62_mine.py       # chain-derived identity, dereg, idle
```

`check_sn62_paths.py` exists because `fabricate.perturb()` deliberately skips lists, and five of
this adapter's eleven event kinds are driven by list-valued fields (`scored_hk`, `earning_hk`,
`evaluating`, `repo_hot`, `ours`). `smoke_comp.py` step 6 reports **ALL CHECKS PASSED** without
ever executing any of them.
