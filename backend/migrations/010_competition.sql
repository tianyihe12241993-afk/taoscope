-- Off-chain competition tracking.
--
-- The chain tables say what a subnet EMITS. They cannot say who is winning its
-- competition, what the current rules are, or whether our submission is queued
-- or running -- all of that lives in each subnet's own dashboard, in a shape
-- that subnet invented. So competition state is stored as opaque jsonb produced
-- by a per-subnet adapter, and only the routing/dedup machinery is shared.

CREATE TABLE IF NOT EXISTS comp_subnet (
  netuid    int PRIMARY KEY,
  slug      text NOT NULL,
  label     text NOT NULL,
  enabled   boolean NOT NULL DEFAULT true,
  added_at  timestamptz NOT NULL DEFAULT now()
);

-- Last snapshot per subnet. Persisted rather than held in memory so a restart
-- diffs against reality instead of re-announcing every event it already sent.
CREATE TABLE IF NOT EXISTS comp_state (
  netuid     int PRIMARY KEY,
  data       jsonb NOT NULL DEFAULT '{}'::jsonb,
  updated_at timestamptz,
  polls      bigint NOT NULL DEFAULT 0,
  fails      bigint NOT NULL DEFAULT 0,
  last_error text
);

-- Coarse history: one row per poll that actually changed something.
CREATE TABLE IF NOT EXISTS comp_snapshot (
  netuid int NOT NULL,
  ts     timestamptz NOT NULL DEFAULT now(),
  data   jsonb NOT NULL,
  PRIMARY KEY (netuid, ts)
);

CREATE TABLE IF NOT EXISTS comp_event (
  id        bigserial PRIMARY KEY,
  ts        timestamptz NOT NULL DEFAULT now(),
  netuid    int NOT NULL,
  kind      text NOT NULL,
  severity  text NOT NULL DEFAULT 'info',
  dedup_key text NOT NULL DEFAULT '',
  title     text NOT NULL,
  body      text NOT NULL DEFAULT '',
  detail    jsonb NOT NULL DEFAULT '{}'::jsonb,
  delivered boolean NOT NULL DEFAULT false
);
CREATE INDEX IF NOT EXISTS ix_comp_event_ts ON comp_event (netuid, ts DESC);
CREATE INDEX IF NOT EXISTS ix_comp_event_dedup ON comp_event (netuid, kind, dedup_key, ts DESC);

-- Topic routing. netuid NULL = the digest/General topic of that chat.
CREATE TABLE IF NOT EXISTS comp_topic (
  chat_id   bigint NOT NULL,
  thread_id int NOT NULL DEFAULT 0,
  netuid    int,
  title     text NOT NULL DEFAULT '',
  bound_at  timestamptz NOT NULL DEFAULT now(),
  prefs     jsonb NOT NULL DEFAULT '{}'::jsonb,
  PRIMARY KEY (chat_id, thread_id)
);
CREATE INDEX IF NOT EXISTS ix_comp_topic_netuid ON comp_topic (netuid);

-- What WE are running. Seeded from disk by an adapter, editable with /watch.
CREATE TABLE IF NOT EXISTS comp_watch (
  netuid   int NOT NULL,
  ref      text NOT NULL,
  label    text NOT NULL DEFAULT '',
  uid      int,
  active   boolean NOT NULL DEFAULT true,
  added_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (netuid, ref)
);

-- One row per watched git repo. etag is kept because a GitHub 304 does not
-- count against the unauthenticated 60/hour rate limit, and polling several
-- repos every few minutes would otherwise exhaust it.
CREATE TABLE IF NOT EXISTS comp_repo (
  netuid       int NOT NULL,
  repo         text NOT NULL,
  branch       text NOT NULL DEFAULT '',
  sha          text,
  etag         text,
  subject      text,
  author       text,
  committed_at timestamptz,
  checked_at   timestamptz,
  PRIMARY KEY (netuid, repo, branch)
);
