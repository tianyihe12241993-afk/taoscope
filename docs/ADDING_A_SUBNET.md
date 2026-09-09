# Adding a subnet to competition tracking

One file per subnet. Nothing else in the stack changes — you do **not** create a
new bot, a new token, a new process or a new deployment.

## The mental model

| Layer | Who owns it | Changes per subnet? |
|---|---|---|
| Telegram transport, topics, dedup, cooldowns, routing, commands | the framework | no |
| Polling, failure backoff, restart-safe diffing | the framework | no |
| GitHub repo watching, registration/burn events from chain | the framework | no |
| **Which URLs to call, what the fields mean, what counts as news** | **your adapter** | **yes** |

`/state` in the SN100 topic and `/state` in the SN85 topic are the same code
path. The topic's `message_thread_id` selects the adapter behind it. That is why
one bot is enough.

---

## Step by step

### 1. Scaffold

```bash
cd /home/dev/work/taoscope/backend
python devtools/new_adapter.py 85 vidaio "Vidaio"
```

Writes `app/comp/adapters/sn85.py` from `_template.py`.

### 2. Find the subnet's data

Before writing code, find these five things. Most subnets publish all of them
without auth, because their own dashboard is a browser app calling the same API.

| What you need | Where to look |
|---|---|
| The dashboard API base | open the dashboard, DevTools → Network → XHR |
| Leaderboard / standings | usually `/leaderboard`, `/arena/<x>/leaderboard`, `/scores` |
| The rules of the current round | `/recipe`, `/config`, `/competition`, `/round` |
| One entry's detail | `/submissions/<id>`, `/entries/<id>`, `/runs/<id>` |
| The repo that defines the rules | the subnet's GitHub org — watch the one the validator installs |

Check what you found:

```bash
curl -s https://<api>/v1/leaderboard | python3 -m json.tool | head -40
```

If a subnet has **no** public API, the adapter can still be useful with only
`repos` set plus the chain facts the framework supplies for free — repo commits
and registration flips are often the two alerts that matter most anyway.

### 3. Fill in `snapshot()`

Return a flat dict of scalars. **If a source fails, omit its keys.** `fetch_json`
returns `None` on any failure so this stays a one-line guard:

```python
lb = await fetch_json(f"{API}/v1/leaderboard")
if lb and lb.get("items"):
    s["n_board"] = lb.get("total")
    s["king_hk"] = lb["items"][0]["hotkey"]
```

Include a **sorted** list of board hotkeys (`board_hk`) if you want new-entrant
alerts — sorting stops the API's response order from creating false diffs.

### 4. Fill in `diff()`

```python
def diff(self, old, new):
    out = []
    if not old:
        return out                      # first sight is a baseline
    if changed(old, new, "king_hk"):    # never raw .get() comparisons
        out.append(CompEvent(...))
    return out
```

### 5. Register it

```python
# app/comp/adapters/__init__.py
from .sn85 import SN85
ADAPTERS = {a.netuid: a for a in (SN100(), SN85())}
```

### 6. Validate — no Telegram, no chat, nothing posted

```bash
docker compose build backend
docker compose run --rm -T -v $PWD/backend/devtools:/srv/devtools:ro \
  backend python devtools/smoke_comp.py 85
```

It fails loudly if the snapshot is empty, a renderer emits HTML Telegram would
reject, a cold start would spam the chat, or an outage would look like a change.

**Rebuild first.** `smoke_comp.py` runs inside the image, so an adapter you just
registered is invisible until `docker compose build backend`. The error it gives
is `No adapter registered for SN<n>` — which reads like you forgot step 5 when
you actually forgot the build.

**Step 6 mutates conventional key names** — `king_hk`, `comp_id`, `pin`,
`king_elo`, `king_score`, `board_hk` — and asserts your `diff()` fires on at
least one. If your subnet's identity key is `king_uid` and nothing else, step 6
fails with *"diff() fired nothing on a changed board — check changed() usage"*,
which is true but points at the wrong thing. Either use a name it mutates, or
diff on both:

```python
if changed(old, new, "king_uid") or changed(old, new, "king_hk"):
```

Both come off the same record, so either moving is the same event anyway.

### 7. Ship it

```bash
docker compose up -d backend
```

Then in the group: `/setup` (creates and binds a topic per subnet), or `/bind 85`
inside a topic you already made.

---

## The three rules an adapter must not break

**1. Absence is not a change.** A failed fetch must omit its key, and every
optional comparison must go through `changed(old, new, *keys)`. Comparing raw
`.get()`s turns an API outage into a fake *"the pin changed → None"*. On
2026-08-18 the SN100 API returned `503 no healthy backends` and a watcher built
the naive way announced that every patch we were holding had been invalidated.

**2. First sight is a baseline, never an alert.** `diff({}, snapshot)` must
return `[]`. Without it, every restart replays the entire competition into the
chat, and after two of those the topic gets muted.

**3. A field earns a place only if a change in it triggers an action.** Not "it
is interesting" — *what would I do differently the moment this moves?* If the
answer is nothing, leave it out of `diff()` and put it in `render_state()`
instead, where it costs nothing to ignore.

---

## Alert shape

Every event renders the same way, so a burst of them stays readable:

```
<icon> HEADLINE          what happened, in three words
old → new                the change itself, never implied
context                  the numbers you would look up next
action                   only when there is one, in italics
```

Set `CompEvent.icon` when the adapter knows something more informative than the
severity — the pipeline stage, a crown, a repo. Leave it empty to fall back to
the severity glyph. **Never put an icon in the title**: the router adds exactly
one, and a title that carries its own opens the message with `• •`.

Prefer alerting on a *transition that changes a decision* over a raw delta.
Queue depth moving 1→2 is not news; the queue **draining** is, because it means
nothing is ahead of a submission. `_diff_queue()` in `sn100.py` is the worked
example.

## Preview an alert before it fires

```bash
python devtools/preview_alerts.py 85
```

And to read the *views* as a phone shows them — `smoke_comp.py` proves a renderer emits HTML
Telegram will accept, which is a different question from whether it is readable:

```bash
python devtools/preview_views.py 85
```

SN62's `/board` passed the HTML check while printing `$0.123$ smCat`, because an approval marker
had been pushed against a right-justified cost column and read as part of the number.

Fabricates a plausible "before", runs your `diff()`, and prints every resulting
alert exactly as the router formats it — including whether it buzzes. Nothing is
sent. Use it to read your own alerts before they arrive at 3am.

## Model the situation, not the label

A dashboard's `stage`/`status` field is what the platform's state machine is
doing, which is often not what the *run* is doing. On SN100 a submission cycles
`similarity → llm_review → scoring → provisioning → failed → queued` every ~30s
while it waits for hardware, so `stage` says "scoring" after 596 cycles and zero
GPUs.

Derive a phase from evidence — was a machine ever allocated, did a heartbeat
arrive, is there a score — and **diff on the phase**. Diffing on the raw label
produces an alert every poll that says nothing, and a `/state` that lies.

Then label the counts honestly. If the API calls something "running" and it does
not mean running, do not pass that word through to the operator.

## Recurring alerts — deadlines and countdowns

A competition with a submission window needs more than one warning. The window
opening is news (a decision starts), and so is each step toward it closing —
but only while there is still an action attached.

Fire once per threshold per window, with the boundary in the dedup key:

```python
DEADLINE_MARKS = (360, 180, 60, 30, 15)          # minutes

for mark in sorted(DEADLINE_MARKS):               # ASCENDING — see below
    if new_min <= mark < old_min:
        out.append(CompEvent(kind="deadline", ...,
                             dedup_key=f"dl:{boundary}:{mark}"))
        break
```

**Iterate the marks ascending.** In declaration order (360 first) a poller that
was down and resumes at t-10m matches `10 <= 360 < 400` and announces *"closes
in 6h"* at the most urgent moment there is. Ascending fires the most urgent
applicable mark, which is the only correct one after a gap.

**Let the same mark change severity with the situation.** `warn` at t-30m when
nothing of ours is submitted; `info` at t-30m when it already is. The clock is
identical — the decision is not, and only one of them should buzz at 3am.

The same test applies to a **recurring result**. SN15 completes a race every night; that is worth
a buzz when we were in it and `info` when we were not, because the nightly result of a contest we
skipped is exactly the message that gets a topic muted.

**A countdown that lives on the CURRENT round's record goes negative.** SN15's
`qualifying_closes_at` belongs to the running race, so it is in the past for most of the day and
the next one is not knowable until the next race record is created. Omit the key rather than
render a negative countdown or reach for a guessed boundary.

Announce the window *opening* too, with the full schedule in the body. It is the
moment the decision starts, and the one alert a miner can act on calmly.

## Severity → does the phone buzz

| Severity | Buzzes | Use for |
|---|---|---|
| `info` | no | board churn, queue depth, score drift |
| `good` | no | our run advanced a stage, we scored |
| `warn` | **yes** | our run died, registration opened, sealed weights moved |
| `critical` | **yes** | rules changed, repo moved, new king, emission split changed |

Getting this wrong is how a monitor gets muted, and a muted monitor is worth
nothing. When unsure, pick the quieter one — you can always raise it later.

---

## What every adapter gets for free

- Polling on its own cadence, in its own task, with backoff to 15 min on
  repeated failure — one dead dashboard cannot stall the other subnets
- Restart-safe diffing against the stored snapshot
- Cooldown/dedup per event kind (`store.COOLDOWN`)
- Routing to the right forum topic, with per-topic `/mute`
- The GitHub repo watcher (ETag-conditional, so `304`s are free)
- Registration open/close + entry cost, from our own chain tables
- `/state /info /board /mine /events /watch /unwatch /poll /mute` — all of them
  argument-free inside a bound topic
- Inclusion in the cross-subnet digest via `render_digest()`

## Renderer constraints

Telegram accepts only `<b> <i> <u> <s> <code> <pre> <a> <blockquote>`. An unknown
tag makes it reject the **whole** message, so `esc()` everything that came from
an API and let `smoke_comp.py` check the output. `two_col()` gives aligned
monospace that stays readable on a phone. Keep `<pre>` rows under ~40 characters.

### Colour

Telegram's HTML has **no colour markup** — the tag list above is the whole of it. The one place a
client paints text is a syntax-highlighted code block, so `base.diff_block(lines)` emits
`<pre><code class="language-diff">`, where the **first character of each line** is the colour:

```
"+ ..."  green      "- ..."  red      "  ..."  plain
```

`router.BANNER` uses the same three characters for event severity — keep the meanings aligned
(`+` = ours/good, `-` = red/critical). SN62's `/board` uses it to mark our own rows.

Make the marker a **real character in a real column**, never styling alone: not every client
highlights, and a board that only reads correctly in colour is unreadable on the ones that do
not. And `esc()` everything before it goes in — a `<pre>` is still HTML-parsed, so one
API-supplied `<` makes Telegram reject the **whole** message rather than mis-render one row.

`two_col()` packs **two pairs per row, each `ljust` to `width` (18)** — it is not
one pair per line. A cell whose `"label value"` reaches 18 characters eats the
gap and collides with its neighbour: `margin 0.02->0.005warmup 8 rnds`. Keep each
cell ≤ ~17 characters and put prose *outside* the `<pre>`, never in a cell.

## Troubleshooting

| Symptom | Cause |
|---|---|
| Events logged but nothing in Telegram | no topic bound — `/bind <netuid>` in the topic; the log says so explicitly |
| `/state` says "isn't bound to a subnet" | you are in an unbound topic; it still shows the digest |
| Nothing ever fires | `diff()` compares keys that `snapshot()` never sets — `smoke_comp.py` step 6 catches this |
| Chat floods after a deploy | `diff()` is missing `if not old: return []` |
| Message never arrives, no error | a renderer emitted an unsupported tag — run `smoke_comp.py` |
| `/setup` cannot create topics | bot needs admin + **Manage Topics** |
| `No adapter registered for SN<n>` after registering it | stale image — `docker compose build backend` |
| Step 6 fails but `diff()` looks right | it mutates `king_hk`/`comp_id`/`pin`/`board_hk` — diff on one of those |
| Two-column block runs together | a `two_col()` cell hit the 18-char width — shorten it |
| Countdown names the wrong threshold after downtime | marks iterated in declaration order; sort ascending |
| One subnet silent for days, everything else fine | its poll is failing — check `comp_state.fails`; field notes 17-19 |
| `/bind <n>` bound General and `/setup` now skips that subnet | fixed 2026-08-24: `/bind` in General creates the topic instead |
| Snapshot is missing half its keys but every check passes | the dashboard rate-limited the burst — field note 14 |
| An alert provably never fires and logs nothing | `changed()` on a key absent from `old` — field note 15 |
| One alert kind arrives every single poll | a continuously drifting value diffed raw — bucket it or use hysteresis |
| `can't open file '/srv/devtools/smoke_comp.py'` | devtools is not baked into the image — mount it (field note 13) |
| Alert says "0 revealed" on a commit-reveal subnet | that is the timelock, not an empty field — render the mechanism (field note 11) |

---

## Field notes from real adapters (read before writing snapshot())

Every one of these produced a wrong adapter first. The full list with examples lives in the
docstring of `backend/app/comp/adapters/_template.py`; `docs/SN98_ADAPTER.md` is a worked case.

1. **Verify the response SHAPE against the live API** — don't infer it from a field name or a
   sibling adapter. SN98's `per_validator` reads like a mapping and is a list.
2. **The "current" thing is often not in the list endpoint** — SN98's open round is absent from
   `/api/v1/rounds`, 404s by id, and lives at a different endpoint that lacks the block height.
3. **Boolean flags lie** — SN98's `is_king` is False on every row including the champion's.
4. **Identify ourselves by a stable key, never uid** — uids get reassigned; a stale uid silently
   reports a stranger's score as ours.
5. **In-flight data is often private** — render "no published board yet", never a stale round as
   if it were live.
6. **Bucket anything that counts down** — otherwise diffing it fires an event every poll.
7. **An adapter with no topic bound is silent** — polling looks healthy and the events are dropped.
   `/bind <netuid>` after registering.
8. **Derive ownership from the artifact ref when you can** — SN91 deploys every generator under
   one Hippius namespace, so `gen_ref.startswith("tony/")` marks our rows in the standings and in
   the pending-commit list. It needs no watchlist, survives a uid being recycled, and picks up a
   fresh registration with no code change. Prefer it over a hand-maintained hotkey list wherever
   the subnet exposes a stable per-miner prefix.

   **The strongest form of this is the COLDKEY, and it needs no cooperation from the subnet at
   all.** `my_coldkey` already holds ours and `neuron_live` already holds the metagraph, so
   `SELECT hotkey FROM neuron_live WHERE netuid=$1 AND coldkey = ANY($2)` answers "what have I
   registered here" for *every* subnet, and most platforms then expose a by-coldkey or by-hotkey
   lookup for the artifacts. SN62 does the whole of `/mine` this way in one API request per
   coldkey. Two rules come with it:

   * **Unknown is not empty.** If the metagraph has no rows for the netuid, return `None` and omit
     the counts — never `0`. A chain poller that missed a beat would otherwise report every
     hotkey you own as deregistered.
   * **Report a registered hotkey with nothing submitted.** It is capacity you are paying to hold
     and not using, and it is invisible on every dashboard because the platform only knows about
     agents, not about slots.
9. **A status feed can be stale while the chain is not** — SN91's `status/round.json` keeps
   serving the previous round for hours after a stage ends, so a stage read alone will say `duel`
   when the duel finished. Cross-check against a block-derived clock (`chain.json`) and cache-bust
   the URL; a CDN will happily hand back the last document all day.
10. **One validator can be permanently broken** — SN91 publishes three receipts per round and one
    validator emits `rejected: contract_digest_mismatch` on *every* round, including ones that
    dethroned. Filter on `status == "scored"` or the adapter reports every round as a failure.

11. **A count of a commit-reveal field is a LIE for most of the window.** SN91 seals every
    rival commitment until `reveal_margin_blocks` (25 blocks ≈ 5 min) before the boundary, so
    `n_pending` reads 0 for ~11h55m of a 12h window. The countdown alert printed
    `0 commitments revealed`, which a tired operator reads as *"the field is empty, we would
    walk this round"* when it means *"the field is hidden"*. Render the mechanism, not the
    number: `0 revealed — sealed until ~5 min before the boundary`. This is
    absence-is-not-a-change one layer up — the value is present and honest, the *inference* from
    it is what breaks.

14. **A dashboard that RATE-LIMITS makes an adapter silently blind, and every check still
    passes.** SN62's read API 429s on the *fourth* request of a burst, sends no `Retry-After`
    and no `x-ratelimit-*` header. `base.fetch_json` maps every non-200 to `None`, and the
    correct response to `None` is to omit the key — so the adapter did exactly the right thing
    and produced a snapshot missing the leader, the funnel, the upload price and the whole
    leaderboard. `smoke_comp.py` reported **ALL CHECKS PASSED**, because all six checks test
    behaviour *given* a snapshot and a half-empty snapshot is a legal one. Curl the endpoints
    **back to back**, not one at a time, before believing a shape; if any 429s, the adapter needs
    its own paced, retrying client (`sn62._get`) and `fetch_json` must not be used.

15. **`changed()` cannot gate a field that is absent from `old` most of the time.** It returns
    False whenever a key is missing from *either* side — first sight is a baseline in both
    directions — which is right for observations and wrong for a field whose PRESENCE is the
    event. SN62's `repo_range` is written only on the tick after a commit, so routed through
    `changed()` its alert could never fire, ever, with no error and no log line. Where presence
    is the signal, compare explicitly (`new.get(k) and new[k] != old.get(k)`) and do the
    baselining where the value is produced instead. Say so in a comment — it reads like a rule
    violation and the next reader will try to "fix" it.

16. **A price quoted in alpha but priced in USD drifts continuously.** SN62's upload burn moved
    2.1172 → 2.0637 α in forty minutes with no rule change at all. Diffed raw it is an alert
    every poll; rounded into buckets it flaps whenever it sits on a boundary. Use **hysteresis**:
    keep the last value you announced and only replace it on a move past a band (10%). The same
    shape applies to any chain-denominated cost.

17. **`seed_watchlist()` reads a file ANOTHER PROGRAM owns — be shape-tolerant, and never let it
    take the subnet with it.** SN100's watchfile started holding a single submission *object*
    where the adapter expected a *list* of them. `for r in rows` iterated the dict's keys,
    `r.get(...)` hit a `str`, and the AttributeError escaped a try/except that only wrapped
    `json.loads`. Because `_sync_watchlist()` runs FIRST in a tick — before `snapshot()` — the
    whole subnet went dark for **3.4 days**: no board, no crown, no repo watch, all for a broken
    auxiliary feature. Accept an object *or* a list, skip non-mappings, take the id under either
    name the tooling has used. The framework now isolates this step, so a watchlist you cannot
    read costs only the watchlist.

18. **Log the TRACEBACK when a poll fails.** That outage hid behind
    `SN100 poll failed (378): 'str' object has no attribute 'get'` — a line naming neither file
    nor function nor line, on a subnet nobody was looking at. `log.exception`, not `log.warning`.
    This repo already makes the same argument about anonymous Telegram errors; it applies to the
    poller first.

19. **Nothing watches the watcher.** A subnet can fail every poll for days and the only trace is a
    warning in a log plus a `fails` counter in `comp_state` that no command surfaces. If you add
    one thing after reading this list, make it an alert on consecutive poll failures.
12. **Publish the correction WITH the number when a rule cuts both ways.** SN91's tie-aware
    cohort lets up to 3 tied finalists advance — and then judges every one of them at a
    family-wise `bootstrap_alpha / k`, so a k=3 cohort tightens each member's bar by more than
    the whole dethrone margin is worth. An alert that said only "advanced to the duel" would be
    read as good news. The receipt carries `cohort_k`/`cohort_alpha`; surface them on the
    verdict so the reader sees the bar they were actually judged against.
13. **`devtools/` is not in the backend image.** `docker compose run --rm backend python
    devtools/smoke_comp.py <n>` fails with *"can't open file '/srv/devtools/smoke_comp.py'"*.
    Mount it: `docker compose run --rm -v "$PWD/backend/devtools:/srv/devtools" backend python
    devtools/smoke_comp.py <n>`.

## Shared behaviour every adapter gets free

- `base.chain_block(s)` — operators / uids / entry cost / registration / emission, appended to
  `/state` centrally. ⚠ `pct()` already scales by 100.
- operator-growth + registration events from `poller._diff_chain`.
- `/guide` — a pinnable primer built from `alerts: dict[kind, meaning]`, links and repos. Override
  `render_guide()` to add subnet-specific rules.
