#!/usr/bin/env python3
"""Scaffold a new subnet adapter from the template.

    python devtools/new_adapter.py 85 vidaio "Vidaio"

Writes app/comp/adapters/sn85.py and tells you the two edits left to make.
Refuses to overwrite an existing adapter.
"""
import pathlib
import re
import sys

HERE = pathlib.Path(__file__).resolve().parent.parent
ADAPTERS = HERE / "app" / "comp" / "adapters"


def main() -> int:
    if len(sys.argv) != 4:
        print(__doc__)
        return 2
    netuid, slug, label = int(sys.argv[1]), sys.argv[2], sys.argv[3]
    if not re.fullmatch(r"[a-z0-9_]+", slug):
        print(f"slug must be lowercase a-z0-9_ (got {slug!r})")
        return 2

    dest = ADAPTERS / f"sn{netuid}.py"
    if dest.exists():
        print(f"{dest} already exists — edit it instead.")
        return 1

    src = (ADAPTERS / "_template.py").read_text()
    body = src.partition('"""')[2].partition('"""')[2].lstrip("\n")
    header = (f'"""SN{netuid} {label}.\n\n'
              f'Scaffolded from _template.py. Fill in snapshot() against the real\n'
              f'dashboard, then decide in diff() what is worth a message.\n'
              f'Validate with:  python devtools/smoke_comp.py {netuid}\n'
              f'"""\n')
    out = (header + body
           .replace("class SNXX(", f"class SN{netuid}(")
           .replace("    netuid = 0", f"    netuid = {netuid}")
           .replace('    slug = "template"', f'    slug = "{slug}"')
           .replace('    label = "Template Subnet"', f'    label = "{label}"'))
    dest.write_text(out)

    print(f"created {dest.relative_to(HERE)}\n")
    print("Next:")
    print(f"  1. Fill in snapshot() -- point it at SN{netuid}'s dashboard API.")
    print(f"  2. Register it in app/comp/adapters/__init__.py:")
    print(f"       from .sn{netuid} import SN{netuid}")
    print(f"       ADAPTERS = {{a.netuid: a for a in (SN100(), SN{netuid}())}}")
    print(f"  3. docker compose build backend")
    print(f"  4. python devtools/smoke_comp.py {netuid}     # no Telegram, no chat")
    print(f"  5. docker compose up -d backend")
    print(f"  6. In the group:  /setup   (or /bind {netuid} inside a topic)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
