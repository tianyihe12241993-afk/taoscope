"""Render every SOMA-specific alert, without Telegram and without a chat.

    docker compose run --rm -T backend python devtools/preview_sn114.py

`preview_alerts.py` fabricates an SN100-shaped "before" (it sets
`phase="running"`, an SN100 phase), so on SN114 it only ever exercises the
unsent transition. This drives the transitions that actually matter here --
the upload door, our own entries walking the three gates, a dry OpenRouter key,
and the source-review ban -- using numbers taken from the real comp-112 result
so the copy is read against figures that occurred.

Nothing is fetched, nothing is sent, nothing is written.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.comp.adapters.sn114 import SN114            # noqa: E402
from app.comp.base import SEVERITY                   # noqa: E402
from app.comp.router import format_event             # noqa: E402

AD = SN114()

BASE = {
    "comp_id": 145, "comp_name": "CoT-Compression-8", "comp_state": "upload",
    "upload_start": "2026-08-20T14:30:00Z", "upload_end": "2026-08-25T14:30:00Z",
    "eval_start": "2026-08-25T14:30:00Z", "eval_end": "2026-09-03T14:30:00Z",
    "n_field": 0, "n_scored": 0, "n_banned": 0, "n_ranked": 0,
    "board_hk": [], "banned_hk": [], "board": [],
    "n_validators": 3, "n_validators_working": 3,
    "n_ours": 9, "n_unsent": 9, "unsent": ["sn114-4", "sn114-8", "sn114-9"],
    "ours": {},
}

HK4 = "5HBmDWAccYJ7Eekk6PXXoAUbGErGpyN1sRamSPEPYBBoLqeF"
HKK = "5Ccgc6RverWMrrabWdw2nUwcWRsCafr4PwVGpFo3nAvqbnzQ"
HKB = "5FgWvBnidasnaqJDVfJkccVUMNpRXA57nfo1pLoFSLw3bJkg"


def S(**kw):
    s = {k: (v.copy() if isinstance(v, (dict, list)) else v)
         for k, v in BASE.items()}
    s.update(kw)
    return s


def ours(**kw):
    e = {"hk": HK4, "uid": 101, "name": "sn114-4", "phase": "unsent"}
    e.update(kw)
    return {"sn114-4": e}


# (headline, old, new) -- each pair isolates one transition.
SCENARIOS = [
    ("the upload window tightening, while hotkeys are unspent",
     S(upload_end="2026-08-23T14:30:00Z"),
     S(upload_end="2026-08-21T12:30:00Z")),

    ("the upload door shutting with hotkeys unspent",
     S(comp_state="upload"),
     S(comp_state="evaluation", n_unsent=3)),

    ("a brand new competition opening",
     S(comp_id=112, comp_name="CoT-Compression-7", comp_state="finished"),
     S()),

    ("our entry walking the three gates",
     S(ours=ours(phase="screen1")),
     S(ours=ours(phase="passed1", s1_passed=True, s1_score=0.2603,
                 s1_vfy=0.3707, s1_exp=0.2274, s1_edt=0.3647,
                 runs_s1=150, done_s1=150, statused_s1=100, failed_s1=0))),

    ("our entry entering full evaluation -- the moment money starts burning",
     S(ours=ours(phase="screen2", s1_passed=True, s1_score=0.2603, s2_score=0.1387)),
     S(ours=ours(phase="eval", s1_passed=True, s1_score=0.2603, s2_score=0.1387,
                 runs_eval=2910, done_eval=120, statused_eval=80, failed_eval=0))),

    ("the OpenRouter key going dry mid-evaluation (comp 112's real figures)",
     S(ours=ours(phase="eval", runs_eval=2910, done_eval=1200,
                 statused_eval=1200, failed_eval=0)),
     S(ours=ours(phase="eval", runs_eval=2910, done_eval=1411,
                 statused_eval=1940, failed_eval=998))),

    ("our entry finishing, against the clean bar",
     S(king_hk=HKK, king_score=0.1396, n_ranked=88, n_banned=16,
       ours=ours(phase="eval", runs_eval=2910, done_eval=2900,
                 statused_eval=1940, failed_eval=0)),
     S(king_hk=HKK, king_score=0.1396, n_ranked=88, n_banned=16,
       ours=ours(phase="scored", total=-0.4453, rank=51,
                 runs_eval=2910, done_eval=2910,
                 statused_eval=1940, failed_eval=0))),

    ("our entry failing stage 1",
     S(ours=ours(phase="screen1")),
     S(ours=ours(phase="failed1", s1_passed=False, s1_score=0.2418))),

    ("OUR hotkey banned by the source review",
     S(banned_hk=[], ours=ours(phase="eval", runs_eval=2910, done_eval=800)),
     S(banned_hk=[HK4], n_banned=1, ours=ours(phase="banned", status="failed review"))),

    ("rivals banned -- the real top of the board moves",
     S(banned_hk=[], king_hk=HKK, king_score=0.1396, n_ranked=88),
     S(banned_hk=[HKB], n_banned=16, banned_top=0.2095,
       king_hk=HKK, king_score=0.1396, n_ranked=88)),

    ("a new leader on the clean board",
     S(king_hk=HKB, king_score=0.1311, n_ranked=88, n_banned=16),
     S(king_hk=HKK, king_score=0.1396, n_ranked=88, n_banned=16)),

    ("hotkeys uploading to the open competition",
     S(board_hk=[], n_field=0),
     S(board_hk=[HKK, HKB], n_field=2)),

    ("every validator down",
     S(n_validators_working=3),
     S(n_validators_working=0)),

    # -- the platform repo, graded by what the commit touched -----------------
    ("SOMA/main: a scoring commit (comp 112's real hotfix range)",
     S(soma_sha="1df35ac706"),
     S(soma_sha="ec05da4e9a", soma_prev="1df35ac706", soma_class="money",
       soma_subject="hotfix(scoring): exclude comp 112 stage 2 from final score",
       soma_n_commits=12,
       soma_files=["mcp_platform/app/services/incentive_calculator.py",
                   "mcp_platform/app/api/routes/scoring.py",
                   "mcp_platform/app/services/swebench_orchestrator.py",
                   "docs/miner/scoring.md"],
       soma_reasons=["the score formula, benchmark weights and which stages count",
                     "stage-2 advancement and run dispatch"],
       soma_commits=["hotfix(scoring): exclude comp 112 stage 2 from final score",
                     "perf(orchestrator): scale swe dispatch and trim log noise"],
       soma_flags=["a rule keyed to ONE competition id",
                   "a hand-written hotkey override list",
                   "which stages count toward the final score"],
       soma_compare="https://github.com/DendriteHQ/SOMA/compare/1df35ac706...ec05da4e9a")),

    ("SOMA/main: scoring moves AFTER 5 of our hotkeys already uploaded",
     S(soma_sha="8a3fa4d073", n_unsent=4, since_n=9),
     S(soma_sha="ec05da4e9a", soma_prev="8a3fa4d073", soma_class="money",
       soma_subject="hotfix(scoring): exclude comp 112 stage 2 from final score",
       soma_n_commits=1, n_unsent=4,
       soma_files=["mcp_platform/app/services/incentive_calculator.py"],
       soma_reasons=["the score formula, benchmark weights and which stages count"],
       soma_commits=["hotfix(scoring): exclude comp 112 stage 2 from final score"],
       soma_flags=["a rule keyed to ONE competition id"],
       round_sha="5296534af7", since_n=13, since_class="money",
       soma_compare="https://github.com/DendriteHQ/SOMA/compare/8a3fa4d073...ec05da4e9a")),

    ("SOMA/main: a frontend commit -- must NOT buzz",
     S(soma_sha="8a3fa4d073"),
     S(soma_sha="ec05da4e9a", soma_prev="8a3fa4d073", soma_class="other",
       soma_subject="hotfix(frontend): serve cached aggregates for past competitions",
       soma_n_commits=1, soma_files=[], soma_reasons=[],
       soma_commits=["hotfix(frontend): serve cached aggregates for past competitions"],
       soma_flags=[],
       soma_compare="https://github.com/DendriteHQ/SOMA/compare/8a3fa4d073...ec05da4e9a")),

    ("SOMA/main: the run harness moving",
     S(soma_sha="aaaaaaaaaa"),
     S(soma_sha="bbbbbbbbbb", soma_prev="aaaaaaaaaa", soma_class="harness",
       soma_subject="perf(orchestrator): scale swe dispatch",
       soma_n_commits=2,
       soma_files=["mcp_platform/app/services/sandbox/remote_compact_bench_manager.py"],
       soma_reasons=["how a run actually executes — this moves step counts, "
                     "and steps are the score"],
       soma_commits=["perf(orchestrator): scale swe dispatch"], soma_flags=[],
       soma_compare="https://github.com/DendriteHQ/SOMA/compare/aaaa...bbbb")),

    # -- how many are actually competing --------------------------------------
    ("the field thinning as stage 1 eliminates entries",
     S(n_active=400, n_field=458, n_screening=380, n_qualified=20,
       n_scored=0, n_out=58),
     S(n_active=307, n_field=458, n_screening=0, n_qualified=307,
       n_scored=88, n_out=63)),

    ("the field moving by less than a quarter -- must NOT alert",
     S(n_active=300, n_field=458),
     S(n_active=310, n_field=458)),

    ("stage 2 starting to bind (a thin field would let everyone through)",
     S(n_passed1=52, advance_cut=52, cut_binds=False),
     S(n_passed1=395, advance_cut=79, cut_binds=True)),

    ("a thin field -- stage 2 stops filtering anyone",
     S(n_passed1=395, advance_cut=79, cut_binds=True),
     S(n_passed1=44, advance_cut=44, cut_binds=False)),

    ("the upload deadline being EXTENDED mid-round (comp 145 did this)",
     S(upload_end="2026-08-25T14:30:00Z"),
     S(upload_end="2026-08-27T14:30:00Z", n_unsent=8)),

    ("the upload deadline being SHORTENED -- an emergency",
     S(upload_end="2026-08-27T14:30:00Z"),
     S(upload_end="2026-08-22T14:30:00Z", n_unsent=8)),

    # -- registration ---------------------------------------------------------
    ("our hotkeys evicted from a full subnet, mid upload window",
     S(dereg_ours=[], n_reg_ours=9, n_ours=9, reg_cost_tao=0.05,
       ours=ours(phase="unsent")),
     S(dereg_ours=["sn114-4", "sn114-102"], n_reg_ours=7, n_ours=9,
       reg_cost_tao=0.05, ours=ours(phase="unsent"))),
]


# (headline, old, new) pairs that MUST produce nothing. Each one is a bug that
# actually reached the chat, or the same bug class one field over.
SILENT = [
    ("first sight of soma_sha — the 'None → ec05da4e9a' triple-alert",
     {k: v for k, v in S().items() if k != "soma_sha"},
     S(soma_sha="ec05da4e9a", soma_class="other",
       soma_subject="hotfix(frontend): serve cached aggregates")),

    ("a graded sha that has not moved",
     S(soma_sha="ec05da4e9a", soma_prev="1df35ac706", soma_class="money"),
     S(soma_sha="ec05da4e9a", soma_prev="1df35ac706", soma_class="money")),

    ("first sight of dereg_ours, with one already deregistered",
     {k: v for k, v in S().items() if k != "dereg_ours"},
     S(dereg_ours=["sn114-4"], n_reg_ours=8, n_ours=9)),

    ("first sight of banned_hk on a board that already has bans",
     {k: v for k, v in S().items() if k != "banned_hk"},
     S(banned_hk=[HKB], n_banned=16)),

    ("first sight of board_hk on a populated field",
     {k: v for k, v in S().items() if k != "board_hk"},
     S(board_hk=[HKK, HKB], n_field=2)),

    ("GitHub unreachable — soma_sha absent from the new snapshot",
     S(soma_sha="ec05da4e9a", soma_prev="1df35ac706"),
     {k: v for k, v in S().items() if k != "soma_sha"}),

    ("the metagraph sweep has not run — dereg_ours absent",
     S(dereg_ours=[], n_reg_ours=9),
     {k: v for k, v in S().items() if k != "dereg_ours"}),
]


def main() -> int:
    total = 0
    print("=" * 68)
    print("  MUST BE SILENT")
    print("=" * 68)
    bad = 0
    for headline, old, new in SILENT:
        evs = AD.diff(old, new)
        mark = "ok  " if not evs else "FAIL"
        if evs:
            bad += 1
        print(f"  [{mark}] {headline}")
        for ev in evs:
            print(f"          !! {ev.kind}: {ev.title}")
    if bad:
        print(f"\n  {bad} silence check(s) FAILED — those would reach the chat.")
        return 1
    print(f"\n  all {len(SILENT)} silent.\n")
    for headline, old, new in SCENARIOS:
        evs = AD.diff(old, new)
        print("\n" + "=" * 68)
        print(f"  {headline}")
        print("=" * 68)
        if not evs:
            print("  (no alert)")
            continue
        for ev in evs:
            total += 1
            _, loud = SEVERITY.get(ev.severity, ("•", False))
            print(f"\n  {ev.kind}  [{ev.severity}]  "
                  f"{'🔔 BUZZES' if loud else '🔕 silent'}   "
                  f"dedup={ev.dedup_key}")
            print("  " + "-" * 66)
            for line in format_event({
                "severity": ev.severity, "icon": ev.icon,
                "title": ev.title, "body": ev.body,
            }).splitlines():
                print("  " + line)
    print(f"\n{total} alert(s) across {len(SCENARIOS)} scenarios.")
    print("Cold start / no change / outage are covered by smoke_comp.py 114.")
    return 0


raise SystemExit(main())
