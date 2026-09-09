-- Chain hyperparameters use u64 max (18446744073709551615) as an "unbounded/disabled"
-- sentinel, which does not fit int32. Widen the honest ones to bigint; the sentinel
-- itself is stored as NULL by the transform layer rather than clamped to a wrong number.
ALTER TABLE subnet_live ALTER COLUMN tempo                  TYPE bigint;
ALTER TABLE subnet_live ALTER COLUMN blocks_since_last_step TYPE bigint;
ALTER TABLE subnet_live ALTER COLUMN num_uids               TYPE bigint;
ALTER TABLE subnet_live ALTER COLUMN max_uids               TYPE bigint;
ALTER TABLE subnet_live ALTER COLUMN active_uids            TYPE bigint;
ALTER TABLE subnet_live ALTER COLUMN validator_count        TYPE bigint;
ALTER TABLE subnet_live ALTER COLUMN unique_coldkeys        TYPE bigint;
ALTER TABLE subnet_live ALTER COLUMN top_coldkey_hotkeys    TYPE bigint;
ALTER TABLE subnet_live ALTER COLUMN immunity_period        TYPE bigint;
ALTER TABLE subnet_live ALTER COLUMN max_validators         TYPE bigint;
ALTER TABLE subnet_live ALTER COLUMN activity_cutoff        TYPE bigint;
ALTER TABLE subnet_live ALTER COLUMN min_allowed_weights    TYPE bigint;
ALTER TABLE subnet_live ALTER COLUMN weights_rate_limit     TYPE bigint;
ALTER TABLE subnet_live ALTER COLUMN weights_version        TYPE bigint;
ALTER TABLE subnet_live ALTER COLUMN commit_reveal_period   TYPE bigint;
