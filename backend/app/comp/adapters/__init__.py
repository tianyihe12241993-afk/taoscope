"""Adapter registry.

Only adapters listed in ADAPTERS are polled. Everything else in this directory
is a scaffold waiting for its snapshot() to be pointed at a real dashboard --
registering one before that would poll the template's placeholder URL every few
minutes and log a failure each time.

Live: SN15 SN49 SN62 SN67 SN91 SN98 SN100 SN114.

Every other subnet one of our coldkeys holds a UID on gets a ChainAdapter at
runtime (see refresh() below): the same topic, commands and generic alerts,
from our own chain tables. Writing a real adapter replaces it.

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

# Chain-only adapters, added at runtime for every subnet that has our UIDs or a
# bound topic but no adapter above. See ../chain_adapter.py.
CHAIN: dict[int, SubnetAdapter] = {}


def get(netuid: int) -> SubnetAdapter | None:
    return ADAPTERS.get(netuid) or CHAIN.get(netuid)


def all_adapters() -> list[SubnetAdapter]:
    return sorted([*ADAPTERS.values(), *CHAIN.values()], key=lambda a: a.netuid)


def ensure_chain(netuid: int, label: str) -> SubnetAdapter:
    """The adapter for a subnet, creating a chain-only one if none exists."""
    ad = get(netuid)
    if ad is None:
        from ..chain_adapter import ChainAdapter
        ad = CHAIN[netuid] = ChainAdapter(netuid, label)
    return ad


async def refresh() -> list[SubnetAdapter]:
    """Add a chain adapter for every held or bound subnet that lacks one.

    Only ever adds. A subnet we were deregistered from keeps its adapter for the
    life of the process, so its topic still answers and still gets alerts --
    absence from one sweep is not a reason to stop tracking. Returns the new ones."""
    from .. import store
    held = await store.held_subnets()
    wanted = dict(held)
    for n in await store.bound_netuids():
        if n not in wanted:
            wanted[n] = await store.subnet_name(n) or f"subnet-{n}"
    new = []
    for netuid, name in sorted(wanted.items()):
        if netuid >= 0 and get(netuid) is None:
            new.append(ensure_chain(netuid, name))
    return new
