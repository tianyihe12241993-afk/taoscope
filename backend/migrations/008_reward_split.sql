-- Split the daily emission by who actually receives it. A miner cares about the
-- miner half, not the combined figure: on SN98 the headline looked healthy while
-- miners received exactly zero.
ALTER TABLE subnet_live ADD COLUMN IF NOT EXISTS miner_alpha_per_day     double precision;
ALTER TABLE subnet_live ADD COLUMN IF NOT EXISTS validator_alpha_per_day double precision;
ALTER TABLE subnet_live ADD COLUMN IF NOT EXISTS emitted_alpha_per_day   double precision;
