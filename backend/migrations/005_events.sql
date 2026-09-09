-- Network events worth being told about.
CREATE TABLE IF NOT EXISTS chain_event (
  id        bigserial PRIMARY KEY,
  ts        timestamptz NOT NULL DEFAULT now(),
  kind      text NOT NULL,
  netuid    int,
  severity  text NOT NULL DEFAULT 'info',
  title     text NOT NULL,
  body      text,
  detail    jsonb NOT NULL DEFAULT '{}'::jsonb,
  user_id   int REFERENCES app_user(id) ON DELETE CASCADE   -- null = network-wide
);
CREATE INDEX IF NOT EXISTS ix_event_ts ON chain_event (ts DESC);
CREATE INDEX IF NOT EXISTS ix_event_kind_netuid_ts ON chain_event (kind, netuid, ts DESC);

-- Per-chat notification preferences.
ALTER TABLE telegram_link ADD COLUMN IF NOT EXISTS prefs jsonb NOT NULL DEFAULT
  '{"new_subnet":true,"king_change":true,"registration":true,"my_miners":true,"emission_move":false,"price_alert":true}'::jsonb;
