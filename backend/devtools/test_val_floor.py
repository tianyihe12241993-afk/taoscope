"""SN67 must not report routine validator flapping -- only a quorum collapse.

Regression test for a monitor that had trained its reader to ignore it: the
healthy quorum oscillated 4<->5 roughly every 85 minutes, and because the old
dedup_key was the NEW COUNT, the two directions carried different keys and each
one's 1h cooldown never saw its own repeat. Nothing was ever suppressed.

    219 `validators` events in seven days -- 72% of every SN67 event ever
    recorded, ~30/day, half of them `warn` and therefore buzzing a phone,
    for a number that healed itself within the hour.

The rule the old alert broke: a field earns a place in a monitor only if a
change in it triggers an action. A validator we do not run, flapping back on its
own, triggers nothing. A COLLAPSE does -- each healthy validator is one judge
draw and a task score is their median.

Pure function test. No API, no database, no Telegram: diff() is called directly
on synthetic snapshots.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.comp.adapters.sn67 import SN67, VAL_FLOOR      # noqa: E402

AD = SN67()


def snap(quorum, **kw):
    """A snapshot carrying the fields the validators branch reads.

    `quorum=None` means the key is ABSENT -- a failed fetch, which must never
    read as a change in either direction.
    """
    s = {"val_unhealthy": 3, "val_unknown": 2, "batch_id": "b"}
    if quorum is not None:
        s["val_quorum"] = quorum
    s.update(kw)
    return s


def kinds(old, new):
    return [(e.kind, e.severity) for e in AD.diff(old, new) if e.kind == "validators"]


CASES = [
    # (label,                              old,  new,  expected)
    ("the flap that caused this: 5 -> 4",     5,    4,  []),
    ("and back: 4 -> 5",                      4,    5,  []),
    ("6 -> 3, still above the floor",         6,    3,  []),
    ("no change at all",                      5,    5,  []),
    ("collapse 5 -> 2",                       5,    2,  [("validators", "critical")]),
    ("collapse 4 -> 1",                       4,    1,  [("validators", "critical")]),
    ("TOTAL collapse 5 -> 0",                 5,    0,  [("validators", "critical")]),
    ("recovery 0 -> 5",                       0,    5,  [("validators", "good")]),
    ("recovery 2 -> 3, one over the floor",   2,    3,  [("validators", "good")]),
    ("2 -> 1, both under the floor",          2,    1,  []),
    ("fetch failed: quorum absent in new",    5, None,  []),
    ("first sight: quorum absent in old",  None,    4,  []),
]


def main() -> int:
    print(f"VAL_FLOOR = {VAL_FLOOR}\n")
    failures = 0
    for label, o, n, want in CASES:
        got = kinds(snap(o), snap(n))
        ok = got == want
        failures += not ok
        print(f"  {'OK ' if ok else 'BAD'} {label:38s} "
              f"want {str(want or 'silent'):32s} -> {got or 'silent'}")

    # The two framework invariants smoke_comp also checks, restated here so a
    # change to this branch cannot break them unnoticed.
    print()
    for label, old, new in [("restart replays nothing", {}, snap(1)),
                            ("identical snapshots", snap(4), snap(4)),
                            ("outage is not a change", snap(4), snap(None))]:
        got = AD.diff(old, new)
        ok = not got
        failures += not ok
        print(f"  {'OK ' if ok else 'BAD'} {label:38s} want silent"
              f"{'':21s} -> {[e.kind for e in got] or 'silent'}")

    if failures:
        print(f"\n{failures} CASE(S) FAILED")
        return 1
    print("\nONLY A COLLAPSE SPEAKS")
    return 0


raise SystemExit(main())
