# SN98 adapter — what it does and what it cannot

`backend/app/comp/adapters/sn98.py`. Built 2026-08-20 from what had been an unfilled template
copy (it still pointed at `example-subnet-dashboard.ai` and was not registered).

## API traps encoded here

| trap | reality |
| --- | --- |
| `per_validator` | a **LIST of dicts**, not a mapping — the dict access crashed `snapshot()` |
| the open round | **absent from `/api/v1/rounds`** (that list lags) and `/api/v1/rounds/<next>` **404s**. Only `/miner/rounds/current` → `submission_round` has it, and it carries no block height — `current_block` comes from `/api/v1/rounds/current` |
| `is_king` | **False on every row**, including the champion's own defence entry. Use `entry_kind == "champion_defense"` |
| in-flight rounds | roster + scoreboards **403**; `n_miners`, `standings`, `agent_runs` all null |
| identity | matched by **hotkey ss58, never uid** — uids are reassigned when a registration displaces a neuron, so a stale uid silently tracks a stranger |
| `required_score` | **not the next round's bar.** It is `champion_score_before + margin`, the bar that applied *inside* the round reporting it. Over ten published rounds it never equalled the following round's bar; on 2026-08-25 it read **31.00** while the incoming champion sat at **54.20** |

## What it cannot tell you

**Who submitted to the open round.** The platform withholds the field until a round completes, so
the board is a *result feed*, not a live ladder. The adapter says so rather than showing the last
completed round as if it were current.

The closest proxy is the **operator count** under `⛓ CHAIN`: a competitor must burn-register a
hotkey before they can submit, so registrations precede submissions.

## The board is colour-coded

`/board` renders through `base.diff_block`, so the **first character of every row**
is what a highlighting client paints:

```
  #   consensus  uid     v0
  1       54.20   93   54.2
+ 3       40.60   89   40.6  sn98-11     <- green, and says WHICH hotkey
- 6       30.00   59   30.0  K           <- red, the champion setting the bar
```

Three things this fixes, beyond the colour:

* the old marker was a trailing `*` appended straight onto a right-justified
  score, so it read as part of the number — the `$0.123$` collision
  `devtools/preview_views.py` exists to catch;
* `*` and `K` were mutually exclusive, so on the one board that matters most —
  the one where we hold the crown — the crown vanished. Colour and tag are now
  separate columns and both survive;
* **our rows are always shown**, even below the `limit` cut, under a `...`
  break. A board that answers "where am I" by truncating us out of it answers
  nothing, and the field only grows.

Ours is matched on the **hotkey**, the same key `_ours()` matches on — colouring
by uid would paint a stranger's row green after a registration reassigned it.

Verify with `devtools/preview_sn98_board.py`, which renders the live board plus
four shapes it does not currently contain (our row below the cut, ours *is* the
champion, none of ours placed, validators fetch failed) and asserts the marker
column, the header alignment and the tag spacing — 10 checks.

## Alerts

`round_published` (warn) · `king_change` (critical) · `round_open` (warn) ·
`window_closing` (warn → critical at <1h) · `new_entrant` (info), plus the generic
`registration` / `operators` / `repo`.

Countdowns are **bucketed** (<1h/<3h/<6h) so only crossing a threshold speaks — a raw block count
would fire an event every poll.

## Repos watched, in order of how much damage a commit does

1. `neverplayalone/neverplayalone_bench` — `crafting_v2/configs/iron.yaml` defines duration,
   target catalog and scoring bands. A commit changes the game.
2. `neverplayalone/neverplayalone_bench@feat/crafting-mission-v3` — diamond tier, 900s, iron tools
   given. **If it merges, every iron optimisation is worthless.**
3. `neverplayalone/neverplayalone_subnet` — miner/validator code.

## Operating

Identity file: `/work/sn98-npa/artifacts/our_hotkeys.json` (hotkey → ss58).

Deploy: `docker compose build backend && docker compose up -d --no-deps backend`.
The DB is the named volume `taoscope_db` and all `/setup` + `/bind` state is in postgres, so a
backend-only rebuild loses nothing. **Always `--no-deps`.**

⚠ **`/bind 98` in a topic is required** — without it `router.deliver()` logs "no topic bound" and
drops every event while polling looks perfectly healthy.
