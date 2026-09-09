# Deploying TaoScope on a fresh VPS

Everything runs in Docker. A new host needs no Python, no Node, no Postgres —
only Docker Engine with the Compose plugin. From a bare Ubuntu box to a live
site is four commands plus the `.env`.

---

## 1. What the host needs

| | Minimum | Why |
|---|---|---|
| RAM | 4 GB | measured on the live box: db 3.4 GB (mostly cache), backend 550 MB, frontend 85 MB, caddy 28 MB |
| vCPU | 2 | the 15-minute `get_all_metagraphs_info()` call is the only real burst |
| Disk | 40 GB | history grows ~0.3 GB/day (6.4 GB for the first 23 days); it is a time-series, it only goes up |
| Ports | 80, 443 open inbound | Caddy. Postgres binds to `127.0.0.1:55432` and is never exposed |
| Egress | unrestricted HTTPS + wss | the `bittensor` SDK talks to `finney` over websocket, and adapters poll subnet APIs and GitHub |

Docker Engine + Compose plugin (Ubuntu 22.04/24.04):

```bash
curl -fsSL https://get.docker.com | sudo sh
sudo usermod -aG docker "$USER"   # then log out and back in
docker compose version            # must print v2.x
```

## 2. Clone and configure

```bash
git clone https://github.com/tianyihe12241993-afk/taoscope.git
cd taoscope
cp .env.example .env
chmod 600 .env
```

Now edit `.env`. The four that must change on a new host:

```bash
openssl rand -hex 32     # -> TAOSCOPE_JWT_SECRET
openssl rand -hex 16     # -> POSTGRES_PASSWORD
```

* `TAOSCOPE_ADMIN_EMAIL` / `TAOSCOPE_ADMIN_PASSWORD` — the first account, created
  on first boot only. Changing them later does **not** rewrite an existing account.
* `TAOSCOPE_ALLOWED_EMAILS` — who may sign in at all.
* `SITE_ADDRESS` — leave `:80` while on a bare IP; set to the hostname once DNS
  points here and Caddy will get the certificate itself.
* `TAOSCOPE_WORKSPACE` — the host directory mounted read-only at `/work`. On a
  box with no miner workspace, leave the default; the competition adapters treat
  an absent watchfile as normal, keep every board and repo watch, and lose only
  the "our runs" overlay.

Keys that are safe to leave empty — each disables exactly one feature and
nothing else: `TAOSCOPE_TELEGRAM_BOT_TOKEN` (no bot), `TAOSCOPE_GITHUB_TOKEN`
(repo watching falls back to 60 req/hour per IP, shared by every adapter),
`TAOSCOPE_OPENROUTER_API_KEY` / `TAOSCOPE_ANTHROPIC_API_KEY` (no Q&A),
`TAOSCOPE_LIUM_API_KEY` and friends (that provider is skipped, never reported
as a zero balance).

### The one trap when running a second instance

The Telegram bot uses **long polling** (`getUpdates`). Two hosts holding the
same bot token fight over every update and Telegram answers one of them 409
Conflict — you get a bot that randomly drops messages, on both boxes.

One token, one host. For a second instance either leave
`TAOSCOPE_TELEGRAM_BOT_TOKEN` empty, or make a second bot with @BotFather.

## 3. Start

```bash
docker compose up -d          # builds backend + frontend, pulls db + caddy
docker compose ps             # all four up; db shows (healthy)
docker compose logs -f backend
```

Migrations run automatically at backend start and are idempotent. The first
boot is empty by design: the 12s chain poll fills the subnet tables within a
minute, and the first full neuron sweep (~30k neurons, 6-10s) lands within
15 minutes. History accumulates from that moment — it is not backfilled.

## 4. Verify

```bash
./ops/taoscope status                    # services, /api/status, row counts
curl -sI http://<server-ip>/ | head -3   # expect 200 — and NO Location header
curl -s  http://localhost/api/status     # block height must advance between calls
```

A `Location:` header on the root page is the signature of the RSC exploit that
hit this stack once (CVE-2025-55182, Next 15.1.6). `next` is pinned to a patched
release in `frontend/package.json` — keep it pinned, and re-run that `curl -sI`
after every frontend rebuild.

## 5. Domain and HTTPS

1. Point an `A` record at the VPS IP.
2. `SITE_ADDRESS=tao.example.com` in `.env`.
3. `docker compose up -d caddy`

Caddy provisions and renews the certificate on its own. Also set
`TAOSCOPE_COOKIE_SECURE=true` and `TAOSCOPE_PUBLIC_URL=https://tao.example.com`
and restart the backend — until then the login password crosses the wire in
clear text, which is the reason to do this before treating the site as private.

## 6. Moving the existing history to the new box (optional)

Fresh instances start with no history. To carry it over:

```bash
# on the old host
./ops/taoscope backup                 # writes backup-YYYYMMDD-HHMM.sql.gz (~3 GB for 3 weeks)
scp backup-*.sql.gz new-host:~/taoscope/
```

On the new host, restore **before the backend has ever started** -- a database
that already ran the migrations has tables that collide with the dump's:

```bash
docker compose up -d db                        # db only, NOT the backend
psql() { docker compose exec -T db psql -U taoscope -d taoscope "$@"; }

psql -c "CREATE EXTENSION IF NOT EXISTS timescaledb;"
psql -c "SELECT timescaledb_pre_restore();"    # required: see below
gunzip -c backup-*.sql.gz | psql
psql -c "SELECT timescaledb_post_restore();"

docker compose up -d                           # now bring up the rest
```

The two `*_restore()` calls are not optional. The history lives in hypertables
with continuous aggregates and compression policies, and a plain
`psql < dump.sql` replays those catalog rows while Timescale's own triggers are
live -- it fails partway and leaves a database that looks populated but has
broken chunk metadata. `pre_restore` sets `timescaledb.restoring` on the
database (so it survives across separate `psql` invocations) and `post_restore`
clears it and re-arms the background jobs.

Verify before trusting it:

```bash
psql -c "SELECT count(*) FROM timescaledb_information.hypertables;"   # expect 2
psql -c "SELECT max(ts) FROM subnet_snapshot;"                       # the old host's last poll
```

## 7. Day-to-day

```bash
./ops/taoscope status | logs [svc] | restart [svc] | backup | psql
docker compose build frontend && docker compose up -d frontend   # after a UI change
docker compose build backend  && docker compose up -d backend    # after a backend change
```

`restart: unless-stopped` plus Docker's own boot enablement means a VPS reboot
brings the whole stack back unattended.
