#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12,<3.13"
# dependencies = [
#   "anthropic>=0.34,<1",
#   "openpyxl>=3.1,<4",
#   "pydantic>=2.7,<3",
#   "sqlmodel>=0.0.22,<1",
# ]
# ///

"""Real-LLM smoke test for the ingredient parser.

NOT a unit test. Run manually before building anything on top of the
parser. Calls the real Anthropic API against ~7 representative lines
pulled from the legacy xlsx so we know:

  1. Claude Haiku returns valid tool-use output for our actual data
  2. Cost per line is in the expected ballpark
  3. Output quality is good enough to import without per-line review

Usage:
    export ANTHROPIC_API_KEY=$(grep ANTHROPIC_API_KEY ~/projects/infrastructure/.env | cut -d= -f2)
    ./scripts/smoke_parser.py
"""

from __future__ import annotations

import os
import sys
import time

# Make `mise.*` importable when run from the project root
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlmodel import Session, SQLModel, create_engine

from mise.services.ingredient_parser import (
    DEFAULT_MODEL,
    parse_line,
)

CASES = [
    # (label, raw line)
    ("simple weight + prep",         "2 lbs chuck beef, cut into bite-size cubes"),
    ("count + prep",                 "6 garlic cloves, chopped"),
    ("simple spice",                 "1 tablespoon paprika"),
    ("to-taste compound",            "Salt and pepper to taste"),
    ("fraction quantity",            "1/2 pound potatoes"),
    ("capitalisation variant",       "2 Tablespoon olive oil"),
    ("metric + smart-punct chunks",  "180g bacon"),
    ("long with alternatives",       "2.5 lb beef chuck, boneless short rib or any other slow cooking beef, cut into 2″ chunks"),
]


def main() -> int:
    if not os.environ.get("ANTHROPIC_API_KEY"):
        print("FATAL: ANTHROPIC_API_KEY not set.", file=sys.stderr)
        return 2

    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
    SQLModel.metadata.create_all(engine)

    print(f"model: {DEFAULT_MODEL}\n")
    started = time.monotonic()
    with Session(engine) as db:
        for label, raw in CASES:
            t = time.monotonic()
            try:
                p = parse_line(raw, db)
            except Exception as e:
                print(f"  ✗ {label!s}\n     raw={raw!r}\n     ERROR: {e}\n")
                continue
            dur_ms = (time.monotonic() - t) * 1000
            extras = []
            if p.prep_note:        extras.append(f"prep={p.prep_note!r}")
            if p.size_descriptor:  extras.append(f"size={p.size_descriptor!r}")
            if p.package_qty:      extras.append(f"pkg={p.package_qty} {p.package_unit}")
            if p.original_qty is not None and p.original_unit:
                extras.append(f"orig={p.original_qty} {p.original_unit}")
            if p.optional:         extras.append("OPTIONAL")
            print(f"  ✓ {label}  ({dur_ms:.0f}ms)")
            print(f"     raw:  {raw!r}")
            print(f"     out:  qty={p.qty} {p.unit}  name={p.name!r}  cat={p.category}")
            if extras:
                print(f"           {' · '.join(extras)}")
            print()
    total = time.monotonic() - started
    print(f"total: {total:.1f}s for {len(CASES)} lines  →  ~{total/len(CASES):.1f}s/line")
    return 0


if __name__ == "__main__":
    sys.exit(main())
