"""Fabricate a plausible "before" state for any adapter shape.

Shared by smoke_comp.py (step 6: real changes DO fire) and preview_alerts.py.

Both tools used to mutate a fixed list of field names borrowed from the
template -- `king_hk`, `comp_id`, `pin`, `board_hk`. An adapter that names its
state differently was therefore never exercised at all: SN98 diffs `king_uid`
and `last_round`, and SN49 keys everything per tournament under a nested dict,
so both tools reported "nothing fires" on adapters whose diff() was correct.

So instead of guessing an adapter's vocabulary, perturb whatever it actually
stored. Values keep their TYPE, so no diff() can crash on a shape it would
never see in production.
"""
from __future__ import annotations


def bump(v):
    """A different value of the SAME type, or None to leave it alone."""
    if isinstance(v, bool):
        return not v
    if isinstance(v, str):
        return "WAS-" + v[:12] if v else None
    if isinstance(v, (int, float)):
        return type(v)(0 if v else 1)
    return None


def perturb(state: dict, _depth: int = 6) -> None:
    """Mutate every scalar the adapter stored, in place, at every nesting level.

    Recursive, not one-level: SN49 keys its own hotkeys' standing at
    tours -> <tournament> -> mine -> <hotkey>, four dicts down, and a
    single-level walk left every "our agent" alert untested while still
    reporting ALL CHECKS PASSED. The depth budget must exceed the deepest
    adapter's nesting -- too small and it silently no-ops at the bottom.

    Lists are skipped: the callers already mutate the two that matter, and
    rewriting a board wholesale fabricates a hundred fake entrants. `_`-prefixed
    keys are framework view data (`_chain`, `_repos`), not adapter state.
    """
    if _depth <= 0:
        return
    for key, value in list(state.items()):
        if key.startswith("_"):
            continue
        if isinstance(value, dict):
            perturb(value, _depth - 1)
            continue
        bumped = bump(value)
        if bumped is not None:
            state[key] = bumped
