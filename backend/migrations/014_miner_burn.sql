-- Share of miner emission the chain withheld from miners last tempo: incentive
-- routed to the subnet owner's hotkeys is recycled or burned, never paid.
--
-- miner_burned is the chain's OWN record (SubtensorModule::MinerBurned, written
-- in distribute_dividends_and_incentives), 0..1. owner_incentive_share is our
-- independent reconstruction from the metagraph: the incentive held by UIDs
-- whose coldkey is the owner coldkey, or whose hotkey is SubnetOwnerHotkey --
-- the same set get_owner_hotkeys() builds. The two agree on 123 of 128 subnets
-- (2026-09-14); keeping both is how the UI flags the ones where they do not.
ALTER TABLE subnet_live ADD COLUMN IF NOT EXISTS miner_burned          double precision;
ALTER TABLE subnet_live ADD COLUMN IF NOT EXISTS owner_incentive_share double precision;
