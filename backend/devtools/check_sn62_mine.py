"""Prove /mine is driven by the chain, and that its three states stay distinct.

`ours` now comes from: our coldkeys -> the hotkeys they registered on netuid 62
-> the agents those hotkeys uploaded. Three situations must never be blurred,
because each carries a different decision:

    registered + idle       capacity we hold and are not using
    submitted + finished    a burned upload attempt
    submitted + NOT registered   scores normally and EARNS NOTHING

and a fourth that must never be inferred: an unreadable metagraph is not the
same as owning nothing.
"""
import html.parser
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.comp.adapters.sn62 import SN62      # noqa: E402

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


def legal(text):
    c = Check(); c.feed(text or "")
    return c.bad + ([f"unclosed {c.stack}"] if c.stack else [])


BAR = {"comp_id": 27, "top_score": 0.68, "top_cost": 0.1234,
       "perf_threshold": 0.03, "cost_threshold": 0.06, "bar_score": 0.7004,
       "bar_cost": 0.116, "n_validator_problems": 50, "time_multiplier": 1.44,
       "last_approval": "2026-08-21T06:54:43Z"}


def agent(name, stage, hk, **kw):
    d = {"name": name, "hk": hk, "ver": 1, "agent_id": f"id-{name}",
         "stage": stage, "phase": 1, "registered": True, "uid": 7,
         "score": None, "cost": None, "banked": None, "coldkey": "5CK"}
    d.update(kw)
    return d


def show(label, state):
    out = SN62().render_me(state)
    bad = legal(out)
    print(f"\n--- {label}\n{out}")
    assert not bad, bad
    return out


# 1. No coldkey configured at all -> say how to fix it, do not pretend.
out = show("no coldkey configured", dict(BAR, n_coldkeys=0, ours={}))
assert "SN62_COLDKEYS" in out and "my_coldkey" in out

# 2. Registered but nothing uploaded -> idle capacity is the headline.
out = show("registered, nothing uploaded",
           dict(BAR, n_coldkeys=1, n_registered=23, n_idle=23, ours={}))
assert "23" in out and "idle" in out

# 3. Live and dead separated, idle counted.
out = show("mixed", dict(
    BAR, n_coldkeys=1, n_registered=23, n_idle=20,
    ours={"a": agent("Pinkkk", "failed_pre_screening", "5AAA"),
          "b": agent("Xing", "screening_2", "5BBB"),
          "c": agent("Won", "approved", "5CCC", score=0.72, cost=0.11,
                     banked=8.4)}))
assert out.index("IN FLIGHT") < out.index("FINISHED"), "dead listed before live"
assert "20 registered hotkey(s) with nothing" in out
assert "banked 8.400" in out

# 4. THE quiet failure: an agent whose hotkey is no longer registered.
out = show("agent on a deregistered hotkey", dict(
    BAR, n_coldkeys=1, n_registered=22, n_idle=22,
    ours={"a": agent("Ghost", "finished", "5DEAD", registered=False,
                     uid=None, score=0.70, cost=0.10)}))
assert "NOT registered" in out and "earns nothing" in out

# 5. An unreadable metagraph must not read as "nothing registered".
out = show("metagraph unavailable", dict(
    BAR, n_coldkeys=1,
    ours={"a": agent("Xing", "screening_2", "5BBB", registered=None, uid=None)}))
assert "metagraph unavailable" in out
assert "NOT registered" not in out, "unknown rendered as deregistered"

# --- the alerts ---------------------------------------------------------------
BASE = dict(BAR, n_coldkeys=1, n_registered=23, n_idle=22,
            our_hotkeys=["5AAA", "5BBB"],
            ours={"a": agent("Xing", "screening_2", "5BBB")})

# A dereg must fire; registering more must not.
gone = dict(BASE, n_registered=22, our_hotkeys=["5BBB"])
evs = SN62().diff(BASE, gone)
assert [e.kind for e in evs] == ["dereg"], [e.kind for e in evs]
assert evs[0].severity == "warn" and "earns nothing" in evs[0].body
print("\ndereg fires, warn                        OK")
assert not [e for e in SN62().diff(BASE, dict(BASE, n_registered=24,
                                              our_hotkeys=["5AAA", "5BBB", "5CCC"]))
            if e.kind == "dereg"]
print("registering more is silent               OK")

# A new upload fires -- but only once we already had a picture.
more = dict(BASE, ours=dict(BASE["ours"],
                            b=agent("Fresh", "pre_screening", "5AAA")))
evs = [e for e in SN62().diff(BASE, more) if e.kind == "our_run"]
assert len(evs) == 1 and "Upload accepted" in evs[0].title, evs
print("new upload fires                         OK")

cold = dict(BASE, ours={})
evs = [e for e in SN62().diff(cold, BASE) if e.kind == "our_run"]
assert not evs, "back catalogue replayed on the first populated poll"
print("first populated poll is a baseline       OK")

# An outage of the chain-derived keys must stay silent.
assert not SN62().diff(BASE, {k: v for k, v in BASE.items()
                              if k not in ("n_registered", "our_hotkeys")})
print("metagraph outage is silent               OK")

print("\nALL SN62 MINE CHECKS PASSED")
