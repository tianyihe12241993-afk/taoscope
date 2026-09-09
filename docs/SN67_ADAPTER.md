# SN67 adapter — what it does and what it cannot

`backend/app/comp/adapters/sn67.py`. Built 2026-08-20 from what had been an unfilled
template copy (it still pointed at `example-subnet-dashboard.ai` and was not registered).

## The subnet in one paragraph

Miners upload a Python agent. Every day at **15:00Z** a batch closes and the field runs
the same tasks. `qualifying` (~10 tasks, 250+ artifacts) is a selection tournament;
`main` (20 tasks, 1-4 artifacts) is where the crown is contested. Dethroning is a
cascade of three doors in order — `score_margin` +0.10 **absolute**, then
`cost_reduction` ≤0.90x, then `runtime_reduction` ≤0.90x, the latter two requiring a
non-regressing score. An agent can lose on score and still take the crown by being
cheaper or faster.

## API traps encoded here

| trap | reality |
| --- | --- |
| transport | **MCP JSON-RPC over POST**, not REST — `fetch_json` (GET-only) cannot be used. `_MCP` in the adapter is a small async client |
| the handshake | `notifications/initialized` answers **202 Accepted with an empty body**. Treating non-200 as failure kills the session before any tool call runs |
| `get_miner` | **omits any artifact not yet bound to a batch**, so it cannot see an open window at all. A tracker built on it reports "nothing landed" for everything you just submitted |
| the open window | only `get_latest_submissions` sees it — one row per miner, latest only, network-wide. Since **cascade order is submission order**, its ordering is also the cascade-position list |
| uid on a receipt | **frozen at submission time.** It is not a liveness check. Two hotkeys were deregistered on 2026-08-20 while their artifacts sat in a sealed batch and every receipt still read uid 192 / uid 53 |
| `status` | reads `initializing` for ~13h after cutoff. `stage_progress` is the real signal: per-validator resolved/total plus an ETA. A batch that has been "initializing" for three hours produces no alert |
| `get_champion` | returns **`{"error": ...}` inside a 200** (`weights_unavailable`). That is an unknown, not an empty result — `_MCP.call` maps it to `None` so the key is omitted |
| `total_count` | **counts a different thing per stage** — 272 artifacts during `checking_for_duplicates`, 2,670 task-runs once scoring starts (272 x ~10 qualifying tasks). Labelling both "field" reported a field 10x its real size; `_unit()` names the unit from the stage |
| timestamp filtering | `'…T15:00:00.111116Z' > '…T15:00:00Z'` is **False** as a string compare (`.` < `Z`), silently dropping every row in the first 111ms after cutoff. Parse to datetime — this bug cost six rows and mis-stated a cascade position by six places |

## Where "our hotkeys" comes from

Not a hardcoded list. `my_coldkey ⋈ neuron_live` on netuid 67, out of the chain tables
the collector already refreshes every 15 minutes. Two reasons: a hand-maintained map
drifts (the miner's own tracker silently omitted a hotkey for a full day of
submissions), and a deregistered hotkey simply **vanishes** from `neuron_live`, which is
exactly the signal worth alerting on.

Build names come from `/work/sn67-harnyx/runs/SUBMISSIONS.json`, so nothing is typed twice.

## The deploy pin

`get_validators` exposes `validator_version` and `source_revision` per validator. Only
healthy ones judge anything, and they move together — on 2026-08-20 all five healthy
validators ran `20260820.post5` while the three unhealthy ones sat on builds from April,
May and July. The adapter takes the **modal version across the healthy set**, so the
`deploy` alert fires when the build that actually scores us changes, not when something
lands on main.

`source_revision` is **not** a commit in the public `harnyx/harnyx` repo — the validator
is built elsewhere. Do not present the two as the same thing.

## Alerts

| kind | severity | fires when |
| --- | --- | --- |
| `deploy` | 🚨 critical | the healthy quorum's validator build changed |
| `king_change` | 🚨 critical | the champion artifact changed hands |
| `our_hotkey` | 🚨 critical | a hotkey of ours left the chain (or a new one appeared) |
| `batch_done` | ⚠️ | a race finished — our scores and the crown |
| `batch_stage` | ⚠️/• | warn only for `running_and_scoring_tasks`, the stage that spends time |
| `our_entry` | ✅ | one of ours entered the open window, with its cascade position |
| `validators` | ⚠️/• | the healthy quorum changed size (1h cooldown) |
| `repo` | 🚨 critical | `harnyx/harnyx` moved — free from the framework |

## What it cannot tell you

**Results on a hotkey that has since deregistered.** `our_hk` holds live slots only, so a
race result on a dead hotkey does not appear in `/mine` or in `batch_done`. The
`our_hotkey` critical alert reports the transition itself, which is the part that changes
what you would do.

**Whether a sealed batch will score a dead hotkey's artifact.** Unknown, and unknowable
after the fact: finney discards historical state, so a metagraph at a past cutoff block
returns `UnknownBlock: State already discarded`. It has to be checked while the window is
open.

**Batch membership before the batch prepares tasks.** `get_artifact.batch_contexts` is
`[]` for hours after cutoff. That is normal, not a failure.

## Live status — 2026-08-20

| | |
| --- | --- |
| registered | `ADAPTERS` in `adapters/__init__.py`, polling every 300s |
| bound topic | thread **267** in the "Bittensor" supergroup (`/bind 67`) |
| smoke gate | `devtools/smoke_comp.py 67` — all six checks pass |
| framework suite | `smoke_commands`, `test_clean_error`, `test_silence` — unchanged and passing |
| repo baseline | ⚠️ **not taken yet** — `comp_repo` has no `harnyx/harnyx` row |

The missing repo baseline is **not** an adapter fault. The shared GitHub watcher
is over its unauthenticated budget (12 repos across six adapters, 60/hour per
IP), and it parked before SN67's first repo tick landed. Set
`TAOSCOPE_GITHUB_TOKEN` and restart the backend; the baseline is taken on the
next repo tick and the first `repo` alert follows the next commit.

First sight is a baseline, never an alert, so nothing is missed by the delay —
but nothing is *detected* during it either, which is the whole point of the
watcher.

## Cost per poll

Steady state is **3 MCP calls**: `list_miner_task_batches`, `get_validators`,
`get_latest_submissions`. The main-stage board is fetched only when
`last_batch_id` actually moves (it is immutable once a batch completes), and our
own results cost one `get_miner` per live hotkey with a 1.5s gap — the API 429s
on bursts. At 300s that is ~36 calls/hour steady, spiking to ~40 on the one poll
after a race finishes.

## Forward development

Ordered by value, all of it optional:

1. **`/watch` on a specific artifact.** `seed_watchlist()` already feeds
   `comp_watch` from `runs/SUBMISSIONS.json`, but nothing reads it back — the
   adapter resolves our results through `get_miner` per hotkey instead. Wiring
   `store.watched()` into `_ours()` would let a single artifact be followed
   across batches, including on a hotkey that has since dereg'd.
2. **Per-task detail on a finished race.** `get_miner_task_batch_results` and
   `get_task_results` expose per-task, per-validator scores. That is the data
   behind "which tasks scored 0.00 and why", and would make `/mine` answer it
   without leaving Telegram.
3. **Similarity-round outcome for our own entries.**
   `get_miner_task_batch_similarity_round` returns the duplicate votes and the
   classification (`near_duplicate` / `notable_change` / `novel`) that set the
   emission multiplier. Worth an alert of its own when one of ours is judged a
   duplicate — that is terminal for the build.
4. **A cutoff reminder.** `deadline_minutes()` is implemented and the framework
   surfaces the nearest deadline across subnets, but there is no timed nudge.
   Cascade order is submission order, so a reminder at T-5m has real value.
