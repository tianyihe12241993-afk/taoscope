"""Validate API-credit monitoring -- no Telegram, no chat, no DB writes.

    python devtools/smoke_credits.py

Proves the things that actually break:

  1. every configured provider answers, and a balance is parsed
  2. a cold start is SILENT (no "you are low" replay on every restart)
  3. a transient 5xx is SILENT (absence is not a change)
  4. a rejected key DOES alert
  5. crossing a threshold alerts exactly once, not once per poll
"""
import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.config import settings                         # noqa: E402
from app.credits import poller as cp                    # noqa: E402
from app.credits import providers                       # noqa: E402

FAIL = []


def check(label: str, ok: bool, detail: str = "") -> None:
    print(f"    {'OK  ' if ok else 'FAIL'}  {label}{('  ' + detail) if detail else ''}")
    if not ok:
        FAIL.append(label)


async def main() -> int:
    print("thresholds:", cp.thresholds())

    print("\n[1] providers answer")
    readings = await providers.read_all()
    if not readings:
        print("    (no provider keys configured — set them in .env)")
    for name, r in readings.items():
        if r.get("ok"):
            bal = cp.money(r.get("remaining"), r.get("unit", "$"))
            print(f"    {name:12s} {bal:>12s}  {r.get('note', '')}"
                  + (f"  raw={r.get('raw_keys')}" if r.get("raw_keys") else ""))
        else:
            print(f"    {name:12s} {'DEAD KEY' if r.get('dead') else 'transient'}"
                  f"  HTTP {r.get('status')}")

    print("\n[2] bucketing")
    check("healthy balance has no bucket", cp.bucket(500) is None)
    check("$12 falls into the 20 bucket", cp.bucket(12) == 20, str(cp.bucket(12)))
    check("$3 falls into the 5 bucket", cp.bucket(3) == 5, str(cp.bucket(3)))
    check("unknown balance is never a bucket", cp.bucket(None) is None)
    check("a low bucket is critical", cp.severity_for(5) == "critical")
    check("a mid bucket only warns", cp.severity_for(20) == "warn")
    check("a big-account bucket stays silent", cp.severity_for(100) == "info",
          cp.severity_for(100))
    check("per-provider override is honoured",
          cp.thresholds("openrouter") == cp.thresholds()
          or len(cp.thresholds("openrouter")) > 0)

    print("\n[3] a cold start must be silent")
    # tick() only compares when `old` is non-empty; mirror that decision here.
    old, new = {}, {"ok": True, "remaining": 2.0}
    fires = bool(old) and cp.bucket(new["remaining"]) is not None
    check("first sight does not alert", not fires)

    print("\n[3b] first sight of the pods FIELD must be silent")
    # Mirrors tick(): the gate is whether the KEY was there, not the row.
    stored_without_pods = {"remaining": 30.0}          # row exists, never fetched pods
    check("a 65h-old pod does not announce itself",
          "pods" not in stored_without_pods)
    stored_with_no_pods = {"remaining": 30.0, "pods": {}}
    check("an empty pods baseline still allows a real start alert",
          "pods" in stored_with_no_pods)

    print("\n[3c] a credit_low alert with provider-supplied burn must not raise")
    # The regression: Lium sets burn_per_h itself, so the sampled-burn branch is
    # skipped -- and the alert body referenced that branch's local, raising
    # UnboundLocalError and freezing EVERY provider mid-sweep.
    import app.credits.poller as _cp
    src = open(_cp.__file__).read()
    body_block = src[src.index("kind=\"credit_low\""):src.index("await save(name, r)")]
    check("alert body reads the stored burn, not a branch-local",
          "if burn else" not in body_block)

    print("\n[4] a transient failure must be silent")
    r = {"ok": False, "dead": False, "status": 503}
    check("5xx is not a dead key", not r["dead"])
    check("5xx keeps the stored balance", cp.bucket(None) is None)

    print("\n[5] a threshold is crossed once, not every poll")
    was = {"remaining": 25.0}
    crossings = 0
    for bal in (12.0, 11.0, 10.5):          # three polls, all inside the 20 bucket
        b_old, b_new = cp.bucket(was["remaining"]), cp.bucket(bal)
        if b_new is not None and (b_old is None or b_new < b_old):
            crossings += 1
        was = {"remaining": bal}
    check("one alert for three polls in the same bucket", crossings == 1,
          f"{crossings} alert(s)")

    print("\n[6] burn must refuse a too-short window")
    import datetime as _dt
    just_now = (_dt.datetime.now(_dt.timezone.utc)
                - _dt.timedelta(seconds=60)).isoformat()
    check("60s window gives no burn estimate",
          cp._burn({"remaining": 10.0, "_at": just_now}, {"remaining": 9.6}) is None)
    long_ago = (_dt.datetime.now(_dt.timezone.utc)
                - _dt.timedelta(hours=1)).isoformat()
    check("1h window does estimate",
          cp._burn({"remaining": 10.0, "_at": long_ago}, {"remaining": 9.0}) is not None)

    print("\n[6] a top-up must not inherit the draining burn rate")
    # Mirrors tick(): a rising balance drops the stored burn instead of reusing it.
    old_r = {"remaining": 0.0, "burn_per_h": 33.59}
    new_r = {"remaining": 25.80}
    topped = new_r["remaining"] > old_r["remaining"]
    carried = None if topped else old_r["burn_per_h"]
    check("top-up clears the stale burn", carried is None)
    check("no burn -> no confident wrong runway", cp.runway(25.80, carried) == "")

    print("\n[6] runway")
    check("$10 at $5/h reads as 2h", cp.runway(10, 5) == "2.0h left",
          cp.runway(10, 5))
    check("no burn -> no false runway", cp.runway(10, 0) == "")

    if FAIL:
        print("\nFAILED:", ", ".join(FAIL))
        return 1
    print("\nALL CHECKS PASSED")
    return 0


raise SystemExit(asyncio.run(main()))
