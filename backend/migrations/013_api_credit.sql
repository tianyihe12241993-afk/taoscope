-- Balance / usage per API provider (lium, openrouter, parallel, chutes, vercel).
-- One row per provider; `data` is the last good reading, so a failed poll can
-- leave the previous number in place instead of erasing it.
CREATE TABLE IF NOT EXISTS api_credit (
    provider    text PRIMARY KEY,
    data        jsonb NOT NULL DEFAULT '{}'::jsonb,
    updated_at  timestamptz NOT NULL DEFAULT now(),
    polls       bigint NOT NULL DEFAULT 0,
    last_error  text
);
