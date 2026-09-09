"""Print SN98's /board as a phone would show it, live and in four shapes the
live board does not currently contain.

    docker run --rm \
      -v "$PWD/backend/app/comp/adapters/sn98.py:/srv/app/comp/adapters/sn98.py:ro" \
      -v "$PWD/backend/devtools:/srv/devtools:ro" -v /home/dev/work:/work:ro \
      taoscope-backend python devtools/preview_sn98_board.py

The colour lives in the FIRST CHARACTER of each row (see base.diff_block), so
this is not a "does it look nice" check. It asserts that column 0 carries the
right marker on the right rows, that the numbers still sit under their own
headers now that a marker column exists, and that the tag never touches the
last number -- the `$0.123$` collision devtools/preview_views.py was written
to catch, which the old trailing `*` had in exactly the same place.

Nothing is fetched twice, nothing is written and nothing is sent.
"""
import asyncio
import copy
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.comp.adapters.sn98 import SN98                      # noqa: E402

TAGS = re.compile(r"</?(?:b|i|u|s|code|pre|em|strong|blockquote)(?:\s[^>]*)?>")


def plain(html: str) -> str:
    out = TAGS.sub("", html or "")
    return out.replace("&lt;", "<").replace("&gt;", ">").replace("&amp;", "&")


def rows_of(body: str) -> list[str]:
    """The board block: the header, then every line carrying a marker column.

    There is NO blank line after the block -- the tail lines are joined
    straight onto it -- so the terminator has to be the row shape itself.
    Stopping on a blank line swept the bar line, the legend and the
    still-evaluating note into the rows and failed four checks that were
    actually passing.
    """
    lines = body.splitlines()
    i = next(i for i, l in enumerate(lines) if l.strip().startswith("#"))
    out = [lines[i]]
    for l in lines[i + 1:]:
        if l[:2] not in ("+ ", "- ", "  "):
            break
        out.append(l)
    return out


def show(name: str, body: str) -> None:
    w = max((len(l) for l in body.splitlines()), default=0)
    print(f"\n{'=' * 66}\n{name}   (widest line {w})\n{'=' * 66}\n{body}")


def check(ok: bool, msg: str) -> bool:
    print(("  PASS  " if ok else "  FAIL  ") + msg)
    return ok


async def main() -> int:
    ad = SN98()
    s = await ad.snapshot()
    if not s.get("board"):
        print("no published board in the live snapshot — cannot preview")
        return 1

    live = plain(ad.render_board(s))
    show("/board  (live)", live)

    cut = plain(ad.render_board(s, limit=2))
    show("/board 2  (our row falls BELOW the cut)", cut)

    # We hold the crown: the row must be BOTH green and tagged K. The old
    # renderer picked one marker and lost the other.
    crown = copy.deepcopy(s)
    mine = {o["hk"] for o in (crown.get("ours") or {}).values() if o.get("hk")}
    for r in crown["board"]:
        r["king"] = r.get("hk") in mine
    crowned = plain(ad.render_board(crown))
    show("/board  (synthetic: OUR row is the champion)", crowned)

    none = copy.deepcopy(s)
    none["ours"] = {}
    show("/board  (synthetic: none of ours placed)", plain(ad.render_board(none)))

    noval = copy.deepcopy(s)
    noval.pop("validators", None)
    show("/board  (synthetic: validators fetch failed)",
         plain(ad.render_board(noval)))

    print(f"\n{'=' * 66}\nassertions\n{'=' * 66}")
    ok = True
    n_ours = len(s.get("ours") or {})
    body = rows_of(live)
    head, data = body[0], body[1:]

    ok &= check(n_ours > 0, f"the live board contains {n_ours} of our rows to colour")
    ok &= check(all(r[0] in "+- " for r in data),
                "every row starts with a marker character (+ / - / space)")
    ok &= check(sum(r[0] == "+" for r in data) == n_ours,
                f"exactly {n_ours} row(s) marked + (green = ours)")
    ok &= check(sum(r[0] == "-" for r in data) <= 1,
                "at most one row marked - (red = champion)")
    ok &= check(head.startswith("  ") and all(r[1] == " " for r in data),
                "the marker has a column of its own — header offset to match")

    # The header's own column positions must still land on the numbers.
    hi = head.index("uid")
    ok &= check(all(r[hi:hi + 3].strip().isdigit() or r[hi:hi + 3].strip() in ("—", "?")
                    for r in data),
                "the uid column sits under the 'uid' header")

    # A tag must never abut the last number.
    tagged = [r for r in data if re.search(r"\d\s{0,1}[A-Za-z]", r)]
    ok &= check(not tagged,
                "no tag is jammed against a number (needs >=2 spaces)")

    cutrows = rows_of(cut)[1:]
    ok &= check(sum(r[0] == "+" for r in cutrows) == n_ours,
                f"/board 2 still shows all {n_ours} of our rows, below a '...' break")
    ok &= check(any(r.strip() == "..." for r in cutrows),
                "the break between the top N and our rows is marked")

    crows = rows_of(crowned)[1:]
    ours_k = [r for r in crows if r[0] == "+" and r.rstrip().endswith("K")
              or (r[0] == "+" and " K" in r)]
    ok &= check(len(ours_k) == n_ours,
                "when we are the champion the row is green AND still tagged K")

    print("\n" + ("all checks passed" if ok else "FAILURES ABOVE"))
    return 0 if ok else 1


raise SystemExit(asyncio.run(main()))
