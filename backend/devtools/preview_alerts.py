"""Print every alert an adapter can produce, exactly as the router formats it.

    python devtools/preview_alerts.py 100

Takes the live snapshot, fabricates a plausible "before", and renders each
resulting event through the real format_event(). Nothing is sent and nothing is
written. This is the fastest way to see whether an alert reads well before it
fires at 3am.
"""
import asyncio
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import db                                   # noqa: E402
from app.comp import router, store                   # noqa: E402
from app.comp.adapters import all_adapters, get      # noqa: E402
from app.comp.base import SEVERITY                   # noqa: E402
from fabricate import perturb                       # noqa: E402


def as_telegram(html_text: str) -> str:
    """Roughly what the message looks like in the client."""
    t = html_text.replace("<br>", "\n")
    t = re.sub(r"</?(b|strong)>", "", t)
    t = re.sub(r"</?(i|em)>", "", t)
    t = re.sub(r"</?code>", "", t)
    t = re.sub(r"<pre>(.*?)</pre>", r"\1", t, flags=re.S)
    t = re.sub(r'<a href="([^"]+)">([^<]*)</a>', r"\2", t)
    return re.sub(r"<[^>]+>", "", t)


def fabricate(s: dict) -> dict:
    """A 'before' state that triggers as many distinct alerts as possible."""
    old = json.loads(json.dumps(s, default=str))
    old["king_hk"] = "5PREVIOUSKING000000000000000000000000000000000"
    old["king_elo"] = (s.get("king_elo") or 0) - 12_500
    old["pin"] = "automodel@v0.4.9"
    old["pin_hex"] = "000000000000"
    old["shares"] = {"prism": 0.5, "design": 0.5}
    old["burn"] = 0.25
    old["paid"] = {"5OTHERHOTKEY": 1.0}
    old["n_queued"] = (s.get("n_queued") or 0) + 6
    for k in ("prod_pin", "staging_pin"):
        if k in old:
            old[k] = "0000000000"
    if old.get("board_hk"):
        old["board_hk"] = old["board_hk"][1:]
    for o in (old.get("ours") or {}).values():
        o["phase"], o["status"], o["cls"], o["blocked"] = ("running", "running",
                                                           None, None)
    # Adapters that name their state differently, or nest it per round /
    # tournament / entry, are untouched by everything above -- perturb whatever
    # this one actually stored so its own diff() is exercised too.
    perturb(old)
    return old


async def main() -> int:
    if len(sys.argv) != 2 or not sys.argv[1].isdigit():
        print(__doc__)
        print("registered:", ", ".join(f"SN{a.netuid}" for a in all_adapters()))
        return 2
    ad = get(int(sys.argv[1]))
    if ad is None:
        print(f"No adapter for SN{sys.argv[1]}")
        return 1

    await db.connect()
    s = await store.last_state(ad.netuid)
    if not s:
        print("No stored snapshot yet — let the poller run once.")
        return 1

    events = ad.diff(fabricate(s), s)
    # the queue-drained branch only fires from a non-zero start
    drained = dict(s); drained["n_queued"] = 0
    events += [e for e in ad.diff(s, drained) if e.kind == "queue"]

    print(f"SN{ad.netuid} {ad.label} — {len(events)} alert(s)\n")
    for ev in events:
        _, loud = SEVERITY.get(ev.severity, ("•", False))
        row = dict(kind=ev.kind, severity=ev.severity, title=ev.title,
                   body=ev.body, icon=ev.icon)
        print("─" * 58)
        print(f"  {ev.kind}  [{ev.severity}]  "
              f"{'🔔 buzzes' if loud else '🔕 silent'}")
        print("─" * 58)
        print(as_telegram(router.format_event(row)))
        print()
    await db.close()
    return 0


raise SystemExit(asyncio.run(main()))
