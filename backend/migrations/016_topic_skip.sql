-- Subnets a chat has opted OUT of getting a topic for.
--
-- Topics are created automatically for every subnet our coldkeys hold a UID
-- on. Without a record of "the operator removed this one", a deliberately
-- deleted or /unbind-ed topic would be recreated on the next sync, and there
-- would be no way to keep it gone. /setup and /bind <netuid> clear the row.
CREATE TABLE IF NOT EXISTS comp_topic_skip (
  chat_id  bigint NOT NULL,
  netuid   int NOT NULL,
  reason   text NOT NULL DEFAULT '',
  at       timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (chat_id, netuid)
);
