"""SN100's watchfile is written by another program — every shape must be survivable.

The outage this covers: on 2026-08-21 12:03 the miner tooling started writing a
single submission OBJECT where the adapter expected a LIST of them. `for r in
rows` iterated the dict's KEYS, `r.get(...)` hit a str, and the AttributeError
escaped `seed_watchlist`'s json-only try/except. Because _sync_watchlist runs
FIRST in a tick, the whole subnet went dark — no board, no crown, no repo watch
— for 3.4 days, behind the log line

    SN100 poll failed (378): 'str' object has no attribute 'get'

which names neither file nor function nor line.
"""
import asyncio
import json
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.comp.adapters.sn100 import SN100      # noqa: E402
from app.comp import poller                    # noqa: E402

SUB = "ca349e1a4914388756aa752005ac405d01264d6844fedc40e5466ec941e91fbf"

# Every one of these is a shape the file has been, or trivially could be.
CASES = {
    # what broke it: one object, id under `id`, label under `name`
    "single object (the 2026-08-21 format)": (
        {"id": SUB, "name": "sn100-42", "uid": 162, "candidate": "G"},
        [{"ref": SUB, "label": "sn100-42", "uid": 162}]),
    "list of objects, original key names": (
        [{"submission_id": SUB, "label": "a", "uid": 1}],
        [{"ref": SUB, "label": "a", "uid": 1}]),
    "list of objects, new key names": (
        [{"id": SUB, "name": "sn100-42", "uid": 162}],
        [{"ref": SUB, "label": "sn100-42", "uid": 162}]),
    "mixed list with junk in it": (
        [SUB, None, 7, {"id": SUB, "name": "ok", "uid": 3}],
        [{"ref": SUB, "label": "ok", "uid": 3}]),
    "list of bare id strings (no usable id)": ([SUB, SUB], []),
    "empty list": ([], []),
    "object with no id at all": ({"name": "x", "uid": 1}, []),
    "a bare string": ("nope", []),
    "a number": (42, []),
}


async def main() -> int:
    ad = SN100()
    with tempfile.TemporaryDirectory() as tmp:
        wf = Path(tmp) / "live_submissions.json"
        ad.watchfile = wf

        # Absent file is normal and must be silent.
        assert await ad.seed_watchlist() == []
        print(f"{'absent file':<42} -> []            OK")

        # Unparseable file must not raise either.
        wf.write_text("{not json")
        assert await ad.seed_watchlist() == []
        print(f"{'unparseable json':<42} -> []            OK")

        for label, (payload, want) in CASES.items():
            wf.write_text(json.dumps(payload))
            got = await ad.seed_watchlist()
            assert got == want, f"{label}: got {got!r}, want {want!r}"
            print(f"{label:<42} -> {len(got)} row(s)      OK")

        # --- the isolation: a watchlist that explodes must cost only itself ---
        added = []

        class Boom(SN100):
            async def seed_watchlist(self):
                raise RuntimeError("watchfile is a tarball today")

        async def fake_add(netuid, ref, label="", uid=None):
            added.append(ref)

        real_add = poller.store.add_watch
        poller.store.add_watch = fake_add
        try:
            await poller._sync_watchlist(Boom())      # must NOT raise
            print(f"\n{'seed_watchlist raising':<42} -> tick survives  OK")

            class Junk(SN100):
                async def seed_watchlist(self):
                    return [{"label": "no ref here"}, "a string",
                            {"ref": SUB, "label": "good"}]

            await poller._sync_watchlist(Junk())
            assert added == [SUB], added
            print(f"{'rows with no ref':<42} -> skipped, rest kept OK")
        finally:
            poller.store.add_watch = real_add

    print("\nALL SN100 WATCHFILE CHECKS PASSED")
    return 0


raise SystemExit(asyncio.run(main()))
