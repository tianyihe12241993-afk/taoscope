-- Coldkeys that actually earn, distinct from coldkeys that merely hold a UID.
-- On a full subnet the two differ by an order of magnitude (SN91: 36 operators,
-- 3 earning), and only the second number describes the real field.
ALTER TABLE subnet_live ADD COLUMN IF NOT EXISTS earning_coldkeys bigint;
-- `IF EXISTS`: this schema has no subnet_history table (the historical
-- table is subnet_snapshot, which never receives this column). Without
-- the guard this statement aborts migrate() and the backend crash-loops
-- on startup with UndefinedTableError.
ALTER TABLE IF EXISTS subnet_history ADD COLUMN IF NOT EXISTS earning_coldkeys bigint;
