"""Print every rendered view of an adapter as a phone would show it.

    python devtools/preview_views.py 62

smoke_comp.py proves a renderer emits HTML Telegram will ACCEPT. That is a
different question from whether it is readable, and the difference is not
theoretical: SN62's board passed the HTML check while printing `$0.123$ smCat`,
because an approval marker had been pushed up against a right-justified cost
column and read as part of the number.

Nothing is fetched twice and nothing is sent.
"""
import asyncio
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import db                                   # noqa: E402
from app.comp import store                           # noqa: E402
from app.comp.adapters import all_adapters, get      # noqa: E402

# Attributes included: a colourable board emits <code class="language-diff">,
# and a bare-tag regex leaves that visible and misaligns every row under it.
TAGS = re.compile(r"</?(?:b|i|u|s|code|pre|em|strong|blockquote|tg-spoiler)(?:\s[^>]*)?>")


def plain(html: str) -> str:
    """Strip the formatting tags so column alignment is visible as sent."""
    out = TAGS.sub("", html or "")
    return (out.replace("&lt;", "<").replace("&gt;", ">").replace("&amp;", "&"))


async def main() -> int:
    if len(sys.argv) != 2 or not sys.argv[1].isdigit():
        print(__doc__)
        print("registered:", ", ".join(f"SN{a.netuid}" for a in all_adapters()))
        return 2
    ad = get(int(sys.argv[1]))
    if ad is None:
        print(f"No adapter registered for SN{sys.argv[1]}.")
        return 1

    await db.connect()
    await store.register_subnet(ad.netuid, ad.slug, ad.label)
    s = await ad.snapshot()
    # The poller merges these in for free; without them /info loses its tail.
    s.setdefault("_repos", [{"repo": r.partition("@")[0], "sha": "0" * 10,
                             "subject": ""} for r in ad.repos])

    for name, fn in (("/state", ad.render_state), ("/info", ad.render_info),
                     ("/board", ad.render_board), ("/mine", ad.render_me),
                     ("/guide", ad.render_guide), ("digest", ad.render_digest)):
        body = plain(fn(s))
        widest = max((len(ln) for ln in body.splitlines()), default=0)
        print(f"\n{'=' * 60}\n{name}   ({len(body)} chars, widest line {widest})\n"
              f"{'=' * 60}\n{body}")
    await db.close()
    return 0


raise SystemExit(asyncio.run(main()))
