-- Phase 2: Google auth + allowlist, sessions/audit, portfolio, saved filters, telegram.

-- ---------- auth hardening ----------
ALTER TABLE app_user ADD COLUMN IF NOT EXISTS google_sub   text UNIQUE;
ALTER TABLE app_user ADD COLUMN IF NOT EXISTS display_name text;
ALTER TABLE app_user ADD COLUMN IF NOT EXISTS avatar_url   text;
ALTER TABLE app_user ADD COLUMN IF NOT EXISTS last_login   timestamptz;
ALTER TABLE app_user ADD COLUMN IF NOT EXISTS disabled     bool NOT NULL DEFAULT false;
ALTER TABLE app_user ALTER COLUMN password_hash DROP NOT NULL;

-- Closed by default: only these emails may sign in with Google.
CREATE TABLE IF NOT EXISTS email_allowlist (
  email      text PRIMARY KEY,
  role       text NOT NULL DEFAULT 'member',
  note       text,
  added_by   text,
  created_at timestamptz NOT NULL DEFAULT now()
);

-- Server-side sessions so a login can actually be revoked.
CREATE TABLE IF NOT EXISTS user_session (
  jti        text PRIMARY KEY,
  user_id    int NOT NULL REFERENCES app_user(id) ON DELETE CASCADE,
  issued_at  timestamptz NOT NULL DEFAULT now(),
  expires_at timestamptz NOT NULL,
  revoked_at timestamptz,
  ip         text,
  user_agent text
);
CREATE INDEX IF NOT EXISTS ix_session_user ON user_session (user_id);

CREATE TABLE IF NOT EXISTS audit_log (
  id         bigserial PRIMARY KEY,
  ts         timestamptz NOT NULL DEFAULT now(),
  actor      text,
  action     text NOT NULL,
  detail     jsonb DEFAULT '{}'::jsonb,
  ip         text
);
CREATE INDEX IF NOT EXISTS ix_audit_ts ON audit_log (ts DESC);

-- Brute-force throttle, kept in the DB so it survives a restart.
CREATE TABLE IF NOT EXISTS login_attempt (
  id      bigserial PRIMARY KEY,
  ts      timestamptz NOT NULL DEFAULT now(),
  ip      text,
  email   text,
  success bool NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_attempt_ip_ts ON login_attempt (ip, ts DESC);

-- ---------- requirement 3: wallet connect (schema now, signing later) ----------
CREATE TABLE IF NOT EXISTS wallet_link (
  id          serial PRIMARY KEY,
  user_id     int NOT NULL REFERENCES app_user(id) ON DELETE CASCADE,
  ss58        text NOT NULL,
  label       text,
  verified_at timestamptz,
  challenge   text,
  created_at  timestamptz NOT NULL DEFAULT now(),
  UNIQUE (user_id, ss58)
);

-- ---------- requirement 5: my coldkeys ----------
CREATE TABLE IF NOT EXISTS my_coldkey (
  id         serial PRIMARY KEY,
  user_id    int NOT NULL REFERENCES app_user(id) ON DELETE CASCADE,
  coldkey    text NOT NULL,
  label      text,
  created_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE (user_id, coldkey)
);
CREATE INDEX IF NOT EXISTS ix_my_coldkey_user ON my_coldkey (user_id);

-- ---------- requirement 2: saved filters ----------
CREATE TABLE IF NOT EXISTS saved_filter (
  id         serial PRIMARY KEY,
  user_id    int NOT NULL REFERENCES app_user(id) ON DELETE CASCADE,
  name       text NOT NULL,
  criteria   jsonb NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE (user_id, name)
);

-- ---------- requirement 6: telegram ----------
CREATE TABLE IF NOT EXISTS telegram_link (
  id         serial PRIMARY KEY,
  user_id    int NOT NULL REFERENCES app_user(id) ON DELETE CASCADE,
  chat_id    bigint UNIQUE,
  link_code  text UNIQUE,
  username   text,
  linked_at  timestamptz,
  created_at timestamptz NOT NULL DEFAULT now()
);

ALTER TABLE price_alert ADD COLUMN IF NOT EXISTS user_id int REFERENCES app_user(id) ON DELETE CASCADE;
ALTER TABLE price_alert ADD COLUMN IF NOT EXISTS kind    text NOT NULL DEFAULT 'price';
ALTER TABLE price_alert ADD COLUMN IF NOT EXISTS notify_telegram bool NOT NULL DEFAULT true;

CREATE UNIQUE INDEX IF NOT EXISTS ux_telegram_user ON telegram_link (user_id);
