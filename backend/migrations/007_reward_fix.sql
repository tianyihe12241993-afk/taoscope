-- "Reward / day" previously used emission_share x network TAO emission, which measures
-- TAO injected into the subnet's pool — about 6.4x LESS than what UIDs actually earn.
-- Store the alpha genuinely distributed to UIDs per day so the figure is derived from
-- real emissions rather than a pool-injection proxy.
ALTER TABLE subnet_live ADD COLUMN IF NOT EXISTS participant_alpha_per_day double precision;
