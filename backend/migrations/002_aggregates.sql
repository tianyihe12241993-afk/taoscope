-- Continuous aggregates + compression. Statements run individually (CAGGs cannot run in a txn).
CREATE MATERIALIZED VIEW IF NOT EXISTS alpha_candle_1m
WITH (timescaledb.continuous) AS
SELECT time_bucket('1 minute', ts) AS bucket,
       netuid,
       first(price, ts) AS open,
       max(price)       AS high,
       min(price)       AS low,
       last(price, ts)  AS close,
       last(tao_in, ts) AS tao_in,
       last(alpha_in, ts) AS alpha_in,
       last(alpha_out, ts) AS alpha_out,
       last(market_cap_tao, ts) AS market_cap_tao,
       last(subnet_volume, ts) AS subnet_volume,
       last(tao_usd, ts) AS tao_usd
FROM subnet_snapshot
GROUP BY bucket, netuid
WITH NO DATA;

SELECT add_continuous_aggregate_policy('alpha_candle_1m',
  start_offset => INTERVAL '6 hours', end_offset => INTERVAL '1 minute',
  schedule_interval => INTERVAL '1 minute', if_not_exists => TRUE);

CREATE MATERIALIZED VIEW IF NOT EXISTS alpha_candle_1h
WITH (timescaledb.continuous) AS
SELECT time_bucket('1 hour', bucket) AS bucket,
       netuid,
       first(open, bucket) AS open,
       max(high)           AS high,
       min(low)            AS low,
       last(close, bucket) AS close,
       last(tao_in, bucket) AS tao_in,
       last(alpha_in, bucket) AS alpha_in,
       last(market_cap_tao, bucket) AS market_cap_tao,
       last(tao_usd, bucket) AS tao_usd
FROM alpha_candle_1m
GROUP BY 1, 2
WITH NO DATA;

SELECT add_continuous_aggregate_policy('alpha_candle_1h',
  start_offset => INTERVAL '7 days', end_offset => INTERVAL '1 hour',
  schedule_interval => INTERVAL '10 minutes', if_not_exists => TRUE);

ALTER TABLE neuron_snapshot SET (timescaledb.compress, timescaledb.compress_segmentby = 'netuid');

SELECT add_compression_policy('neuron_snapshot', INTERVAL '2 days', if_not_exists => TRUE);

ALTER TABLE subnet_snapshot SET (timescaledb.compress, timescaledb.compress_segmentby = 'netuid');

SELECT add_compression_policy('subnet_snapshot', INTERVAL '14 days', if_not_exists => TRUE);
