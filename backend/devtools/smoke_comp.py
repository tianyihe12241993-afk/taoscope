"""Validate one adapter end to end -- without Telegram and without a chat.

    python devtools/smoke_comp.py 100

Proves the five things that actually break in production:

  1. snapshot() reaches the real API and returns the fields the renderers use
  2. every renderer emits HTML that Telegram will accept
  3. diff({}, snap)  is SILENT      -- a restart must not replay the board
  4. diff(snap, snap) is SILENT     -- no phantom churn
  5. diff(snap, outage) is SILENT   -- absence is not a change

Run this before registering an adapter, and again after any edit to diff().
"""
import asyncio
import html.parser
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import db                                   # noqa: E402
from app.comp import store                           # noqa: E402
from app.comp.adapters import all_adapters, get      # noqa: E402
from fabricate import perturb                       # noqa: E402

# Exactly what Telegram accepts. An unknown tag makes it reject the WHOLE
# message, so a renderer that emits <div> silently sends nothing at all.
ALLOWED = {"b", "i", "u", "s", "a", "code", "pre", "em", "strong", "tg-spoiler",
           "blockquote", "br"}


class Check(html.parser.HTMLParser):
    def __init__(self):
        super().__init__(); self.stack = []; self.bad = []

    def handle_starttag(self, tag, attrs):
        if tag not in ALLOWED:
            self.bad.append(f"<{tag}>")
        elif tag != "br":
            self.stack.append(tag)

    def handle_endtag(self, tag):
        if tag in ALLOWED and tag != "br":
            if not self.stack or self.stack.pop() != tag:
                self.bad.append(f"unbalanced </{tag}>")


def check_html(name, text) -> bool:
    c = Check(); c.feed(text or "")
    problems = c.bad + ([f"unclosed {c.stack}"] if c.stack else [])
    print(f"    {name:10s} {len(text or ''):5d} chars  "
          f"{'OK' if not problems else problems}")
    return not problems


async def main() -> int:
    if len(sys.argv) != 2 or not sys.argv[1].isdigit():
        print(__doc__)
        print("registered:", ", ".join(f"SN{a.netuid}" for a in all_adapters()))
        return 2
    netuid = int(sys.argv[1])
    ad = get(netuid)
    if ad is None:
        print(f"No adapter registered for SN{netuid}. Register it in "
              f"app/comp/adapters/__init__.py first.")
        return 1

    await db.connect()
    await store.register_subnet(ad.netuid, ad.slug, ad.label)
    print(f"SN{ad.netuid} {ad.label}  (poll {ad.poll_seconds}s, "
          f"repos {ad.repos or 'none'})\n")

    print("[1] snapshot")
    s = await ad.snapshot()
    assert s, "snapshot() returned nothing"
    for k, v in sorted(s.items()):
        if k == "ours":
            print(f"    {k:14s} = {len(v or {})} tracked")
            for ref, o in (v or {}).items():
                print(f"       {ref} uid{o.get('uid')} "
                      f"stage={o.get('stage')} rank={o.get('rank')}")
        else:
            print(f"    {k:14s} = {json.dumps(v, default=str)[:88]}")

    print("\n[2] renderers produce Telegram-legal HTML")
    ok = all([check_html("state", ad.render_state(s)),
              check_html("info", ad.render_info(s)),
              check_html("board", ad.render_board(s)),
              check_html("mine", ad.render_me(s)),
              check_html("digest", ad.render_digest(s))])
    assert ok, "a renderer emitted HTML Telegram would reject"

    print("\n[3] cold start must be silent")
    evs = ad.diff({}, s)
    print(f"    {len(evs)} events (expected 0)")
    assert not evs, [e.title for e in evs]

    print("\n[4] no change must be silent")
    evs = ad.diff(s, s)
    print(f"    {len(evs)} events (expected 0)")
    assert not evs, [e.title for e in evs]

    print("\n[5] an API outage must NOT look like a change")
    # Drop the keys a failing endpoint would have omitted.
    drop = {"comp_id", "king_hk", "king_score", "king_elo", "board", "board_hk",
            "n_board", "shares", "burn", "pin", "pin_hex"}
    outage = {k: v for k, v in s.items() if k not in drop}
    evs = ad.diff(s, outage)
    print(f"    {len(evs)} events (expected 0 — absence is not a change)")
    assert not evs, [e.title for e in evs]

    print("\n[6] real changes DO fire")
    old = json.loads(json.dumps(s, default=str))
    for k in ("king_hk", "comp_id", "pin"):
        if k in old:
            old[k] = "SOMETHING-ELSE"
    for k in ("king_elo", "king_score"):
        if k in old:
            old[k] = 1
    if old.get("board_hk"):
        old["board_hk"] = old["board_hk"][1:]
    perturb(old)
    fired = ad.diff(old, s)
    for ev in fired:
        print(f"    [{ev.severity:8s}] {ev.kind:12s} {ev.title[:52]}")
        assert check_html("  body", ev.body or "-")
    assert fired, "diff() fired nothing on a changed board — check changed() usage"

    await db.close()
    print("\nALL CHECKS PASSED")
    return 0


raise SystemExit(asyncio.run(main()))
