-- Q&A usage log: gives per-question cost visibility and backs the daily cap.
CREATE TABLE IF NOT EXISTS ai_usage (
  id            bigserial PRIMARY KEY,
  ts            timestamptz NOT NULL DEFAULT now(),
  chat_id       bigint,
  user_id       int REFERENCES app_user(id) ON DELETE SET NULL,
  question      text,
  answer        text,
  input_tokens  int,
  output_tokens int,
  cache_read_tokens int,
  cost_usd      double precision,
  tool_calls    int,
  ms            int,
  error         text
);
CREATE INDEX IF NOT EXISTS ix_ai_usage_ts ON ai_usage (ts DESC);
CREATE INDEX IF NOT EXISTS ix_ai_usage_chat_ts ON ai_usage (chat_id, ts DESC);
