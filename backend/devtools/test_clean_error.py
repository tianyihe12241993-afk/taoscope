"""clean_error against the exact strings the SN100 platform produced."""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from app.comp.base import clean_error, sci

CASES = [
    # truncated JSON: the closing quote is already gone upstream
    ('measure: provision: lium api: lium api: POST /executors/'
     'e5ec0ec7-9026-4d80-bc12-6c5e2ccda73b/rent -> 400 Bad Request: '
     '{"success":false,"message":"Another rental is already in progress on this node. Tr',
     "Another rental is already in progress"),
    # diagnosis followed by a harness log
    ('measure: exec: lium exec: harness failed (code 1): [harness] prism harness '
     'parent starting (recipe 2.1.0)\n[harness] downloading https://huggingface.co/x',
     "harness failed (code 1)"),
    # already a clean sentence: must survive untouched
    ('B200s are currently out of capacity on Lium; this job is queued until an '
     'offer appears.',
     "B200s are currently out of capacity"),
    ("", ""),
    (None, ""),
]

ok = True
for raw, must_contain in CASES:
    got = clean_error(raw)
    good = must_contain in got and len(got) <= 111 and "\n" not in got
    ok &= good
    print(f"  {'OK ' if good else 'BAD'} {got!r}")

assert sci(3e18) == "3.0e18", sci(3e18)
assert sci(1000000000) == "1.0e9", sci(1000000000)
assert sci(4.0) == "4", sci(4.0)
print(f"  OK  sci: {sci(3e18)} {sci(0.15)} {sci(4.0)}")
assert ok, "clean_error regressed"
print("\nCLEAN_ERROR OK")
