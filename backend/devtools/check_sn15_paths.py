"""Exercise the SN15 paths live data and fabricate.perturb() cannot reach.

Race 133 was RUNNING when this adapter was built, so the completed-race board,
every deadline mark and the elimination alert were all unreachable from a live
snapshot. perturb() additionally skips lists, which is the board and the window.
"""
import html.parser
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.comp.adapters.sn15 import SN15      # noqa: E402

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


CROWN = {"king_id": "k1", "king_name": "Uak", "king_score": 0.4731,
         "threshold": 0.4826, "margin": 0.0095, "days_as_top": 0.73,
         "tao_day": 115.1, "usd_day": 27646.0}

QUALIFYING = dict(CROWN, race_no=134, race_id="r134", phase="qualifying",
                  suite_id=3, suite_ver=3, qual_gate=0.55, n_qualifiers=120,
                  n_field=120, sealed=True, close_mark=360, mins_to_close=400.0,
                  n_coldkeys=1, n_registered=1, our_hotkeys=["5OURS"], n_ever=1,
                  n_idle=1, ours={})


def evs(a, b):
    out = SN15().diff(a, b)
    for e in out:
        bad = legal(e.title) + legal(e.body)
        assert not bad, (e.title, bad)
    return out


def kinds(a, b):
    return [e.kind for e in evs(a, b)]


# --- 1. the deadline ladder ---------------------------------------------------
seq = [(400.0, 360), (200.0, 180), (50.0, 60), (25.0, 30), (10.0, 15)]
prev = QUALIFYING
fired = []
for mins, mark in seq[1:]:
    nxt = dict(prev, mins_to_close=mins, close_mark=mark)
    got = [e for e in evs(prev, nxt) if e.kind == "window_closing"]
    assert len(got) == 1, (mark, got)
    fired.append((mark, got[0].severity))
    prev = nxt
print("deadline ladder fires once per mark  ", fired)
assert [m for m, _ in fired] == [180, 60, 30, 15]
assert [s for _, s in fired] == ["warn", "warn", "critical", "critical"]

# ASCENDING marks: after downtime, a poller resuming at t-10m must announce the
# URGENT mark, never "closes in 6h" at the most urgent moment there is.
gap = evs(QUALIFYING, dict(QUALIFYING, mins_to_close=10.0, close_mark=15))
w = [e for e in gap if e.kind == "window_closing"][0]
assert "10m" in w.title, w.title
assert w.severity == "critical"
print("after downtime fires the URGENT mark  ", w.title)

# A widening deadline (a new window opening) must be silent.
assert not [e for e in evs(dict(QUALIFYING, close_mark=15, mins_to_close=10.0),
                           dict(QUALIFYING, close_mark=360, mins_to_close=400.0))
            if e.kind == "window_closing"]
print("a widening deadline is silent         OK")

# Severity drops when we already have an entry in: same clock, different decision.
mine_in = dict(QUALIFYING, close_mark=30, mins_to_close=25.0,
               ours={"a": {"hk": "5OURS", "name": "ours", "ver": 1,
                           "in_race": True, "phase": "in today's race",
                           "registered": True}})
w = [e for e in evs(QUALIFYING, mine_in) if e.kind == "window_closing"][0]
assert w.severity == "info", w.severity
print("entry in -> quiet at the same mark    OK")

# --- 2. the crown -------------------------------------------------------------
# challenge_threshold decays continuously; on its own it must NEVER speak.
assert not kinds(QUALIFYING, dict(QUALIFYING, threshold=0.4799, margin=0.0068,
                                  days_as_top=1.4))
print("threshold decay alone is silent       OK")
assert kinds(QUALIFYING, dict(QUALIFYING, king_id="k2", king_name="Rival",
                              king_score=0.49)) == ["king_change"]
print("crown change fires critical           OK")

# --- 3. a race completing -----------------------------------------------------
BOARD = [
    {"rank": 1, "name": "Uak", "hk": "5KING", "ver": 2, "raw": 0.50,
     "weighted": 0.4731, "cut": False, "seeds": 0},
    {"rank": 2, "name": "Bolt<1>", "hk": "5BOLT", "ver": 1, "raw": 0.4889,
     "weighted": 0.4700, "cut": True, "seeds": 0},
    {"rank": 3, "name": "ours", "hk": "5OURS", "ver": 1, "raw": 0.4600,
     "weighted": 0.4500, "cut": False, "seeds": 2},
]
DONE = dict(QUALIFYING, phase="complete", sealed=False, board=BOARD,
            winner="Uak", winner_score=0.5, anchor=0.4385, n_eliminated=221,
            n_field=342,
            ours={"a": {"hk": "5OURS", "name": "ours", "ver": 1, "rank": 3,
                        "raw": 0.46, "weighted": 0.45, "seeds": 2,
                        "in_race": True, "registered": True,
                        "phase": "placed #3 of 342"}})
racing = dict(QUALIFYING, phase="racing", scored_count=340,
              ours=dict(DONE["ours"], a=dict(DONE["ours"]["a"], rank=None,
                                             raw=None, weighted=None,
                                             phase="in today's race")))
got = evs(racing, DONE)
assert "race_done" in [e.kind for e in got], [e.kind for e in got]
done_ev = [e for e in got if e.kind == "race_done"][0]
assert "anchor" in done_ev.body and "0.4385" in done_ev.body
assert "#3" in done_ev.body
print("race complete carries anchor + our place OK")

# --- 4. elimination is its own alert ------------------------------------------
cut = dict(DONE, ours={"a": dict(DONE["ours"]["a"],
                                 cut_at="2026-08-25T04:00:00",
                                 phase="eliminated — cut on raw score")})
got = [e for e in evs(DONE, cut) if e.kind == "eliminated"]
assert len(got) == 1 and got[0].severity == "warn", got
assert "RAW" in got[0].body, got[0].body
print("elimination fires warn, names RAW     OK")

# --- 5. renderers -------------------------------------------------------------
for label, state in (("sealed", racing), ("complete", DONE),
                     ("qualifying", QUALIFYING)):
    for name, fn in (("state", SN15().render_state), ("info", SN15().render_info),
                     ("board", SN15().render_board), ("mine", SN15().render_me),
                     ("digest", SN15().render_digest)):
        bad = legal(fn(state))
        assert not bad, (label, name, bad)
print("all renderers legal in all 3 phases   OK")

b = SN15().render_board(DONE)
print("\n--- /board, race complete ---\n" + b)
assert 'class="language-diff"' in b
body = b.split('language-diff">')[1].split("</code>")[0].splitlines()
mine = [ln for ln in body if ln.startswith("+")]
assert len(mine) == 1 and "ours" in mine[0], mine
assert "&lt;1&gt;" in b, "agent name not escaped"
bolt = [ln for ln in body if "Bolt" in ln][0]
assert bolt.rstrip().endswith(" x"), f"cut marker collides with the name: {bolt!r}"
print("\nours coloured, name escaped, cut marked OK")

# A race we did not enter must not buzz; one we did must.
solo = dict(DONE, ours={})
solo_race = dict(racing, ours={})
ev = [e for e in evs(solo_race, solo) if e.kind == "race_done"][0]
assert ev.severity == "info", ev.severity
ev = [e for e in evs(racing, DONE) if e.kind == "race_done"][0]
assert ev.severity == "warn", ev.severity
print("race_done buzzes only when we entered  OK")

assert "sealed" in SN15().render_board(racing)
print("mid-race board says sealed             OK")

# --- 6. outages ---------------------------------------------------------------
for drop in (("close_mark", "mins_to_close"), ("king_id", "king_score"),
             ("n_registered", "our_hotkeys"), ("phase",), ("suite_id", "suite_ver")):
    thin = {k: v for k, v in DONE.items() if k not in drop}
    assert not evs(DONE, thin), f"outage of {drop} produced events"
print("every source outage is silent          OK")

print("\nALL SN15 PATH CHECKS PASSED")
