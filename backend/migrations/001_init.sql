-- TaoScope schema. Idempotent: safe to re-run on every boot.
CREATE EXTENSION IF NOT EXISTS timescaledb;

-- ---------- auth ----------
CREATE TABLE IF NOT EXISTS app_user (
  id            serial PRIMARY KEY,
  email         text UNIQUE NOT NULL,
  password_hash text NOT NULL,
  role          text NOT NULL DEFAULT 'admin',
  created_at    timestamptz NOT NULL DEFAULT now()
);

-- ---------- current chain state (upserted every poll) ----------
CREATE TABLE IF NOT EXISTS subnet_live (
  netuid                int PRIMARY KEY,
  name                  text,
  symbol                text,
  price                 double precision,
  moving_price          double precision,
  market_cap_tao        double precision,
  tao_in                double precision,
  alpha_in              double precision,
  alpha_out             double precision,
  subnet_volume         double precision,
  tao_in_emission       double precision,
  alpha_out_emission    double precision,
  emission_share        double precision,
  realized_tao_per_hour double precision,
  tempo                 int,
  last_step             bigint,
  blocks_since_last_step int,
  num_uids              int,
  max_uids              int,
  active_uids           int,
  validator_count       int,
  unique_coldkeys       int,
  top_coldkey           text,
  top_coldkey_pct       double precision,
  top_coldkey_hotkeys   int,
  hhi                   double precision,
  burn_tao              double precision,
  difficulty            double precision,
  registration_allowed  bool,
  pow_registration_allowed bool,
  immunity_period       int,
  max_validators        int,
  activity_cutoff       int,
  min_allowed_weights   int,
  max_weights_limit     double precision,
  weights_rate_limit    int,
  weights_version       int,
  commit_reveal_enabled bool,
  commit_reveal_period  int,
  owner_coldkey         text,
  owner_hotkey          text,
  network_registered_at bigint,
  -- identity published on-chain by the subnet owner
  chain_github          text,
  chain_url             text,
  chain_discord         text,
  chain_logo            text,
  chain_description     text,
  chain_contact         text,
  block                 bigint,
  updated_at            timestamptz NOT NULL DEFAULT now()
);

-- ---------- subnet history (60s) ----------
CREATE TABLE IF NOT EXISTS subnet_snapshot (
  ts               timestamptz NOT NULL,
  netuid           int NOT NULL,
  block            bigint,
  price            double precision,
  moving_price     double precision,
  tao_in           double precision,
  alpha_in         double precision,
  alpha_out        double precision,
  subnet_volume    double precision,
  market_cap_tao   double precision,
  tao_in_emission  double precision,
  emission_share   double precision,
  num_uids         int,
  active_uids      int,
  unique_coldkeys  int,
  top_coldkey_pct  double precision,
  burn_tao         double precision,
  tao_usd          double precision
);
SELECT create_hypertable('subnet_snapshot','ts', chunk_time_interval => interval '1 day', if_not_exists => TRUE);
CREATE INDEX IF NOT EXISTS ix_snap_netuid_ts ON subnet_snapshot (netuid, ts DESC);

-- ---------- neuron history (15min sample) ----------
CREATE TABLE IF NOT EXISTS neuron_snapshot (
  ts                   timestamptz NOT NULL,
  netuid               int NOT NULL,
  uid                  int NOT NULL,
  hotkey               text,
  coldkey              text,
  emission             double precision,
  incentive            double precision,
  dividends            double precision,
  consensus            double precision,
  stake                double precision,
  alpha_stake          double precision,
  tao_stake            double precision,
  validator_permit     bool,
  active               bool,
  last_update          bigint,
  block_at_registration bigint
);
SELECT create_hypertable('neuron_snapshot','ts', chunk_time_interval => interval '1 day', if_not_exists => TRUE);
CREATE INDEX IF NOT EXISTS ix_neuron_snap_net_uid_ts ON neuron_snapshot (netuid, uid, ts DESC);
CREATE INDEX IF NOT EXISTS ix_neuron_snap_coldkey_ts ON neuron_snapshot (coldkey, ts DESC);

-- ---------- current neuron state ----------
CREATE TABLE IF NOT EXISTS neuron_live (
  netuid               int NOT NULL,
  uid                  int NOT NULL,
  hotkey               text,
  coldkey              text,
  emission             double precision,
  emission_pct         double precision,
  incentive            double precision,
  dividends            double precision,
  consensus            double precision,
  stake                double precision,
  alpha_stake          double precision,
  tao_stake            double precision,
  validator_permit     bool,
  active               bool,
  last_update          bigint,
  block_at_registration bigint,
  rank_in_subnet       int,
  updated_at           timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (netuid, uid)
);
CREATE INDEX IF NOT EXISTS ix_neuron_live_coldkey ON neuron_live (coldkey);
CREATE INDEX IF NOT EXISTS ix_neuron_live_hotkey  ON neuron_live (hotkey);
CREATE INDEX IF NOT EXISTS ix_neuron_live_em      ON neuron_live (netuid, emission DESC);

-- ---------- user-owned metadata (requirement 3) ----------
CREATE TABLE IF NOT EXISTS subnet_meta (
  netuid        int PRIMARY KEY,
  dashboard_url text,
  github_repo   text,
  docs_url      text,
  discord       text,
  website       text,
  twitter       text,
  notes         text,
  tags          text[] DEFAULT '{}',
  watch         bool DEFAULT false,
  our_uids      text[] DEFAULT '{}',
  extra         jsonb DEFAULT '{}'::jsonb,
  updated_at    timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS coldkey_label (
  coldkey    text PRIMARY KEY,
  label      text,
  notes      text,
  is_ours    bool DEFAULT false,
  updated_at timestamptz NOT NULL DEFAULT now()
);

-- ---------- network-wide series ----------
CREATE TABLE IF NOT EXISTS network_snapshot (
  ts             timestamptz NOT NULL PRIMARY KEY,
  block          bigint,
  tao_usd        double precision,
  total_subnets  int,
  total_neurons  int,
  total_coldkeys int,
  total_stake_tao double precision
);

-- ---------- alpha trading foundation (requirement 6) ----------
-- Real price/pool data now; execution layer lands later against these tables.
CREATE TABLE IF NOT EXISTS watchlist (
  id         serial PRIMARY KEY,
  netuid     int NOT NULL,
  note       text,
  created_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS price_alert (
  id         serial PRIMARY KEY,
  netuid     int NOT NULL,
  direction  text NOT NULL CHECK (direction IN ('above','below')),
  threshold  double precision NOT NULL,
  active     bool NOT NULL DEFAULT true,
  fired_at   timestamptz,
  created_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS paper_position (
  id          serial PRIMARY KEY,
  netuid      int NOT NULL,
  side        text NOT NULL CHECK (side IN ('long','short')),
  alpha_qty   double precision NOT NULL,
  entry_price double precision NOT NULL,
  entry_ts    timestamptz NOT NULL DEFAULT now(),
  exit_price  double precision,
  exit_ts     timestamptz,
  note        text
);
