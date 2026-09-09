"""Adapter registry.

Only adapters listed in ADAPTERS are polled. Everything else in this directory
is a scaffold waiting for its snapshot() to be pointed at a real dashboard --
registering one before that would poll the template's placeholder URL every few
minutes and log a failure each time.

Live: SN15 SN49 SN62 SN67 SN91 SN98 SN100 SN114.

Scaffolded, NOT yet active -- each still points at the template's placeholder
URL, so registering one before its snapshot() is filled in would poll a dead
host every few minutes and log a failure each time:
    sn3  sn38  sn66  sn80  sn85
    sn108 sn118 sn126

Adding one more repo to a live adapter is NOT free: the GitHub watcher is
already over its unauthenticated budget (14 repos, 60/hour per IP -- and a 304
still costs a request, measured). Set TAOSCOPE_GITHUB_TOKEN before widening repo
coverage -- see the README's "Operational notes".

To activate one:
    1. fill in snapshot() / diff() / the renderers   (see docs/ADDING_A_SUBNET.md)
    2. import it and add it to ADAPTERS below
    3. python devtools/smoke_comp.py <netuid>        (no Telegram, nothing posted)
    4. docker compose up -d backend, then /bind <netuid> in its topic
"""
from ..base import SubnetAdapter
from .sn100 import SN100
from .sn15 import SN15
from .sn49 import SN49
from .sn62 import SN62
from .sn91 import SN91
from .sn114 import SN114
from .sn67 import SN67
from .sn98 import SN98

ADAPTERS: dict[int, SubnetAdapter] = {a.netuid: a for a in (SN15(), SN49(), SN62(), SN67(), SN91(), SN98(), SN100(), SN114())}


def get(netuid: int) -> SubnetAdapter | None:
    return ADAPTERS.get(netuid)


def all_adapters() -> list[SubnetAdapter]:
    return sorted(ADAPTERS.values(), key=lambda a: a.netuid)
