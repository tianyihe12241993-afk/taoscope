"""One definition of "validator" vs "miner" for every query.

A validator is a UID that receives dividends. `validator_permit` alone is not
it: small subnets hand permits to the top-K by stake, and 250+ permit holders
network-wide earn incentive with zero dividends — they are miners. No UID
without a permit has dividends (checked over all ~30k neurons), so the permit
check below only guards against a chain quirk.

A validator can also earn incentive (42 UIDs do); it stays a validator, and
`also_mines` flags it. A coldkey is 'validator', 'miner' or 'both' by the roles
of its hotkeys on that subnet.

All fragments expect neuron_live aliased as `n`.
"""

IS_VALIDATOR = "(COALESCE(n.validator_permit, false) AND COALESCE(n.dividends, 0) > 0)"
ROLE = f"CASE WHEN {IS_VALIDATOR} THEN 'validator' ELSE 'miner' END"
ALSO_MINES = f"({IS_VALIDATOR} AND COALESCE(n.incentive, 0) > 0)"
