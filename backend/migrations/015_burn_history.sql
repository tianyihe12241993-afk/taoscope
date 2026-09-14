-- History of SubtensorModule::MinerBurned, for the Burn trend sparkline.
--
-- Two writers, one series. The neuron sweep appends a row per subnet every
-- 15 minutes (source 'sweep'); devtools/backfill_burn.py reads the same storage
-- at past blocks from an archive node (source 'archive'). Both are the chain's
-- own number, so the sparkline and the Burn column never disagree on source.
--
-- `emitting` exists because the chain stores 0 for a subnet that paid miners
-- nothing (withheld/total falls back to 0 on 0/0). Without it a subnet that
-- stopped emitting would draw as "burn fell to 0%". NULL = unknown (archive
-- rows older than our own snapshots), which the reader treats as emitting.
CREATE TABLE IF NOT EXISTS subnet_burn (
  ts                    timestamptz NOT NULL,
  netuid                int NOT NULL,
  block                 bigint,
  miner_burned          double precision NOT NULL,
  owner_incentive_share double precision,
  emitting              boolean,
  source                text NOT NULL
);
SELECT create_hypertable('subnet_burn', 'ts', chunk_time_interval => interval '7 days', if_not_exists => TRUE);
CREATE UNIQUE INDEX IF NOT EXISTS ux_subnet_burn_netuid_ts ON subnet_burn (netuid, ts);
