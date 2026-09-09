"""Exercise the SN62 diff paths fabricate.perturb cannot reach.

perturb() deliberately skips lists, so scored_hk / earning_hk / evaluating /
repo_hot / ours never move in smoke_comp step 6. Those are five of the adapter's
nine event kinds, including every one that renders a list into a message.
"""
import asyncio, html.parser, os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from app.comp.adapters.sn62 import SN62

ALLOWED = {"b","i","u","s","a","code","pre","em","strong","tg-spoiler","blockquote","br"}
class C(html.parser.HTMLParser):
    def __init__(self): super().__init__(); self.stack=[]; self.bad=[]
    def handle_starttag(self,t,a):
        if t not in ALLOWED: self.bad.append(f"<{t}>")
        elif t!="br": self.stack.append(t)
    def handle_endtag(self,t):
        if t in ALLOWED and t!="br":
            if not self.stack or self.stack.pop()!=t: self.bad.append(f"</{t}>")

def check(text):
    c=C(); c.feed(text or ""); return c.bad + ([f"unclosed {c.stack}"] if c.stack else [])

BASE = {
    "comp_id": 27, "top_score": 0.68, "top_cost": 0.1234, "perf_threshold": 0.03,
    "cost_threshold": 0.06, "bar_score": 0.7004, "bar_cost": 0.116,
    "n_validator_problems": 50, "time_multiplier": 1.42, "tm_mark": 0.0,
    "last_approval": "2026-08-21T06:54:43Z", "n_approved": 7,
    "scored_hk": ["5AAA", "5BBB"], "earning_hk": ["5AAA", "5BBB"],
    "n_earning": 2, "weights": [{"hk": "5CCC", "w": 0.4}],
    "upload_alpha": 2.0637, "upload_ref": 2.0637,
    "evaluating": [], "n_evaluating": 0,
    "board": [{"hk": "5CCC", "name": "rival<X>", "score": 0.62, "cost": 0.11}],
    "ours": {"5FooBar…quux": {"name": "ours-v1", "ver": 3, "stage": "screening_2",
                              "score": None, "cost": None, "banked": None}},
}

def case(name, mutate):
    new = {k: (v.copy() if isinstance(v, (list, dict)) else v) for k, v in BASE.items()}
    mutate(new)
    evs = SN62().diff(BASE, new)
    print(f"\n--- {name}: {len(evs)} event(s)")
    for e in evs:
        bad = check(e.title) + check(e.body)
        print(f"    [{e.severity:8s}] {e.kind:12s} {e.icon} {e.title}")
        for line in (e.body or "").splitlines():
            print(f"        | {line}")
        assert not bad, bad
    return evs

def m_threat(n):
    n["evaluating"] = [{"name": "rival<X>", "hk": "5CCC", "ver": 2}]
    n["n_evaluating"] = 1
def m_field(n):
    n["scored_hk"] = ["5AAA", "5BBB", "5CCC"]
def m_weights(n):
    n["earning_hk"] = ["5AAA", "5CCC"]; n["n_earning"] = 2
def m_stall(n):
    n["tm_mark"] = 3.0; n["time_multiplier"] = 3.02
    n["last_approval"] = "2026-08-19T06:54:43Z"
def m_repo(n):
    n["repo_range"] = "9397a57e3d...abcdef0123"
    n["repo_commits"] = 4; n["repo_n_files"] = 9
    n["repo_hot"] = [{"path": "utils/incentives.py", "n": 1,
                      "why": "the reward formula — qualify thresholds, decay, time multiplier"}]
def m_repo_cold(n):
    n["repo_range"] = "9397a57e3d...abcdef0123"
    n["repo_commits"] = 1; n["repo_n_files"] = 1; n["repo_hot"] = []
def m_ours_fail(n):
    n["ours"] = {"5FooBar…quux": {"name": "ours-v1", "ver": 3,
                                  "stage": "failed_screening_1", "score": None,
                                  "cost": None, "banked": None}}
def m_ours_dq(n):
    n["ours"] = {"5FooBar…quux": {"name": "ours-v1", "ver": 3,
                                  "stage": "didnt_qualify", "score": 0.62,
                                  "cost": 0.14, "banked": None}}
def m_ours_win(n):
    n["ours"] = {"5FooBar…quux": {"name": "ours-v1", "ver": 3, "stage": "approved",
                                  "score": 0.72, "cost": 0.11, "banked": 8.4}}

case("rival reaches validator stage", m_threat)
case("new scored agents", m_field)
case("earning set changes", m_weights)
case("stall crosses 3.0x", m_stall)
evs = case("commit touches the rules", m_repo)
assert len(evs) == 1 and evs[0].kind == "repo_impact", "rules commit went unexplained"
assert not case("ordinary commit (must be silent)", m_repo_cold)
# The same range twice must not re-fire, and a cold snapshot must stay silent.
same = dict(BASE); m_repo(same)
assert not SN62().diff(same, same), "repeated repo_range re-fired"
assert not SN62().diff({}, same), "cold start fired a repo event"
case("our agent fails screener 1", m_ours_fail)
case("our agent scores but misses the bar", m_ours_dq)
case("our agent is approved", m_ours_win)

# The upload quote drifts continuously; only a banded move may speak.
def m_price_drift(n): n["upload_alpha"] = 2.0102          # -2.6%, same ref
def m_price_jump(n): n["upload_alpha"] = 2.48; n["upload_ref"] = 2.48
assert not case("upload quote drifts 2.6% (must be silent)", m_price_drift)
assert case("upload quote jumps a band", m_price_jump)

# An outage of the board-derived sources must stay silent.
outage = {k: v for k, v in BASE.items()
          if k not in ("scored_hk", "board", "evaluating", "n_evaluating",
                       "earning_hk", "weights", "n_earning")}
assert not SN62().diff(BASE, outage), "outage produced events"
print("\nboard/weights outage -> 0 events  OK")
print("\nALL SN62 PATH CHECKS PASSED")
