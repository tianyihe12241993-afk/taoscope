# SN15 adapter — what it does and what it cannot

`backend/app/comp/adapters/sn15.py`. Built 2026-08-24 from what had been an unfilled template
copy (it still pointed at `example-subnet-dashboard.ai` and was not registered).

ORO is the opposite shape to SN62: **a hard daily deadline** instead of a continuous queue.
Qualifying closes **19:00 UTC**, the race runs overnight on 90 hidden problems, and it completes
around 03:00 UTC. There is a window to miss, so the countdown is the most valuable thing this
adapter says.

## The trap this subnet is built around

**Elimination and the crown are decided on different numbers.**

| | judged on | effect |
| --- | --- | --- |
| elimination | **raw** `race_score` | bottom ~65% cut (221 of 342 in race 132) |
| the crown | **weighted** `= anchor + mean(delta)` over a 3-race window | ranks the survivors |

`anchor` is that race's `top50_mean`, which absorbs difficulty swings so a delta is comparable
across races. An unrun race seeds `delta = 0.0`.

In race 124 **Bolt-1 was ranked #2 by weighted score and eliminated anyway** on a raw 0.4889. A
good window does not protect you. So `/board` prints both numbers side by side and marks the cut
rows — never one number as a proxy for the other.

And **you cannot win on debut, at any score**: two of a new agent's three window slots are seeds,
so its edge is divided by three. Race 132's winner had only the **4th-best raw score** of the day.
`/mine` counts an agent's seeds and says so.

## API traps encoded here

| trap | reality |
| --- | --- |
| **mid-race everything is sealed** | while `status = RACE_RUNNING`, every qualifier's `race_score`, `weighted_score`, `race_rank` and `window` are `null` — and so is the race's own `top50_mean`. You get `scored_count` and nothing else, including for the incumbent |
| the leaderboard's **sort** | `score_type` defaults to `qualifying`, which ranks a saturated public suite capped at 0.9333 and shared by hundreds. Every one of the visible top 25 was measured as an already-**eliminated** agent. Only the envelope's counters are read here; the ordering is never shown |
| the leaderboard's **`?q=`** | is fine, and is the only way to find an agent by hotkey — there is no by-hotkey endpoint. Used for exactly that, never for ranking |
| `challenge_threshold` | decays continuously with `days_as_top` (`top_score + 0.011 × 2^(−days/3.5)`), so it moves every poll. Rendered, **never** diffed |
| `qualifying_closes_at` | belongs to the *current* race record, so it is in the **past** once the race starts. The countdown key is omitted then rather than going negative |
| `evaluations/pending` | `items` is capped at 100 of ~300; only the `summary` is used |
| rate limiting | none — seven requests back to back all returned 200 (measured 2026-08-24), so unlike SN62 this API does not need a paced client |

## Phase is derived, not passed through

`status` is the platform's state machine, not the miner's reality. What a miner needs to know is
whether they can still enter, whether the field is sealed, or whether results are readable — so
`phase` comes from evidence (`race_completed_at` → `race_started_at` → neither), and the same is
true per-agent: `_phase_of()` builds a sentence, because the two fields that come closest
(`is_active_qualifier`, `eliminated_at`) are **both false for an agent sitting in the queue**.

## The deadline ladder

`DEADLINE_MARKS = (15, 30, 60, 180, 360)` minutes, iterated **ascending** — after downtime a poller
resuming at t-10m must announce *"closes in 10m"*, not *"closes in 6h"* at the most urgent moment
there is. Fires once per mark per window (the boundary is in the dedup key), and only on a
**tightening** deadline.

**The same mark changes severity with the situation.** At t-30m with nothing of ours entered it is
`critical`; at t-30m with an entry in it is `info`. Identical clock, different decision, and only
one of them should wake anybody at 3am. `race_done` follows the same rule: a race we did not enter
is `info`, because a nightly result for a contest we skipped is how a topic gets muted.

**Known gap:** the first appearance of `close_mark` is a baseline, so a cold start *inside* the
last minutes of a window is silent. That is deliberate — the alternative fires a deadline warning
on every deploy. The window opening is covered by `race_open`.

## Our own agents

Chain-derived like SN62, with one addition this subnet forced: **`neuron_snapshot` as well as
`neuron_live`**, so a hotkey we *used to* hold is still recognised as ours.

Our `sn15-1` (`5GhJ6fqd…R9tA`, uid 53) **deregistered on 2026-08-22** with an eliminated agent still
on the platform. A registered-only view would have dropped both without a word — the opposite of
what `/mine` is for. It now reads *"0 registered here · 1 formerly held"* and flags the agent as
unable to resubmit.

### As of 2026-08-24

`sn15-v1` v1 — qualified **0.8000** (gate 0.55), raced **0.4283**, **eliminated 2026-08-20**, and its
hotkey then deregistered. `sn15-oro/STATE.md` still says *"registered uid 53, one submit away"*; both
halves are now stale.

## Alerts

| kind | severity | fires when |
| --- | --- | --- |
| `window_closing` | info / warn / critical | a deadline mark is crossed — severity depends on whether we have an entry in |
| `race_open` | warn | a new race record exists and qualifying is open again |
| `race_done` | info / warn | a race completed — winner, the new anchor, how many were cut, where we placed |
| `king_change` | critical | the crown moved |
| `bar` | info | the crown's score changed (the decaying threshold never fires on its own) |
| `suite` | critical | the qualifying suite changed — every local measurement is against the wrong problems |
| `repo_impact` | critical | a commit touched a rules-defining file |
| `eliminated` | warn | one of ours was cut — names the raw score, since that is what did it |
| `our_run` | info | one of ours changed state |
| `dereg` | warn | a hotkey of ours left the metagraph |

Deliberately **not** alerted: the payout, the queue depth, `scored_count`, `days_as_top` and the
challenge threshold. All drift continuously and none changes a decision; they are rendered where
they cost nothing to ignore.

## Validating a change

```bash
docker compose build backend                       # devtools run INSIDE the image
docker compose run --rm -T -v "$PWD/backend/devtools:/srv/devtools:ro" \
  backend python devtools/smoke_comp.py 15
docker compose run --rm -T -v "$PWD/backend/devtools:/srv/devtools:ro" \
  backend python devtools/check_sn15_paths.py     # deadlines, sealed board, elimination
docker compose run --rm -T -v "$PWD/backend/devtools:/srv/devtools:ro" \
  backend python devtools/preview_views.py 15
```

`check_sn15_paths.py` exists because the completed-race board, every deadline mark and the
elimination alert were **all unreachable from a live snapshot** while race 133 was running — and
`fabricate.perturb()` skips lists, which here is the board and the window.
