"""Prove /board marks our rows, escapes miner-controlled names, and never hides us.

Three things smoke_comp.py cannot check, because it renders the LIVE snapshot
and we currently hold no SN62 agent:
  1. our rows carry the `+` prefix a highlighting client paints green
  2. an agent name is esc()'d -- names are free text chosen by other miners, and
     a stray `<` inside a <pre> makes Telegram reject the whole message
  3. an agent still in the pipeline, which has no score and so no ladder row,
     is still reported instead of silently vanishing from "show me my agents"
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


STATE = {
    "comp_id": 27, "n_agents": 170, "n_scored": 46,
    "top_score": 0.68, "top_cost": 0.1234, "perf_threshold": 0.03,
    "cost_threshold": 0.06, "bar_score": 0.7004, "bar_cost": 0.116,
    "n_validator_problems": 50, "time_multiplier": 1.44,
    "last_approval": "2026-08-21T06:54:43Z",
    "board": [
        {"rank": 1, "id": "id-king", "hk": "5KING", "name": "smCat",
         "score": 0.68, "cost": 0.1234, "approved": True},
        {"rank": 2, "id": "id-evil", "hk": "5EVIL", "name": "<b>pwn</b>&co",
         "score": 0.60, "cost": 0.1274, "approved": True},
        {"rank": 3, "id": "id-mine-v1", "hk": "5OURS", "name": "ours-v1",
         "score": 0.58, "cost": 0.1332, "approved": False},
        {"rank": 4, "id": "id-other", "hk": "5OTHR", "name": "basil",
         "score": 0.58, "cost": 0.1332, "approved": False},
    ],
    # Tracked at v2 (a newer agent_id) while v1 is what sits on the ladder.
    "ours": {"5OURS…keyA": {"agent_id": "id-mine-v2", "hk": "5OURS",
                            "name": "ours-v2", "ver": 2, "stage": "screening_2",
                            "score": None, "cost": None}},
}

out = SN62().render_board(STATE)
print(out.replace("\\n", "\n"), "\n")

bad = legal(out)
assert not bad, bad
print("HTML legal                              OK")

assert '<pre><code class="language-diff">' in out, "board is not a colourable block"
print("rendered as a language-diff block       OK")

body = out.split('language-diff">')[1].split("</code>")[0].splitlines()
mine = [ln for ln in body if ln.startswith("+")]
assert len(mine) == 1 and "ours-v1" in mine[0], mine
print("our row prefixed '+' (green)            OK")
assert all(ln.startswith(" ") for ln in body if "ours-v1" not in ln), \
    "a row that is not ours was marked"
print("no rival row marked                     OK")

assert "<b>pwn</b>" not in out and "&lt;b&gt;pwn&lt;/b&gt;&amp;co" in out
print("miner-controlled name escaped           OK")

# Ours is on the ladder as v1, so it must NOT also be reported as unplaced.
assert "screener 2 running" not in out, "matched agent reported as missing too"
print("matched by hotkey across versions       OK")

# Now the same agent with nothing of ours on the ladder at all.
solo = dict(STATE, board=[r for r in STATE["board"] if r["hk"] != "5OURS"])
out2 = SN62().render_board(solo)
assert not legal(out2)
assert "ours-v2" in out2 and "screener 2 running" in out2, out2
print("unscored agent still reported           OK")

# An unscored set must say so -- AND still show ours, which is in the pipeline
# at exactly that moment. An early return here would hide it behind "no board".
early = SN62().render_board(dict(STATE, board=[
    {"rank": None, "id": "x", "hk": "5X", "name": "n", "score": None,
     "cost": None, "approved": False}]))
assert not legal(early)
assert "has been scored yet" in early, early
assert "ours-v2" in early and "screener 2 running" in early, early
print("unscored set -> honest, ours still shown OK")
print("\nALL SN62 BOARD CHECKS PASSED")
