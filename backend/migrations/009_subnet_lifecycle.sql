-- A subnet is REGISTERED when its netuid appears, but only starts paying when the
-- owner makes the start call. Verified equivalent to the chain's is_subnet_active:
-- alpha_out_emission > 0, with zero mismatches across all 128 subnets — so it is
-- derived every fast tick instead of 128 RPC calls taking ~26s.
ALTER TABLE subnet_live ADD COLUMN IF NOT EXISTS is_active bool;
ALTER TABLE subnet_live ADD COLUMN IF NOT EXISTS started_at timestamptz;
