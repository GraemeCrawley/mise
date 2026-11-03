#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12,<3.13"
# dependencies = []
# ///

"""Re-run the same 8 smoke cases through the strangetom-backed parser
(after the 2026-05-29 refactor) to confirm parity against the earlier
LLM smoke. Uses the real parser code path (mise.services.ingredient_parser)
not stubs."""

from __future__ import annotations

import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlmodel import Session, SQLModel, create_engine

from mise.services.ingredient_parser import parse_line

CASES = [
    "2 lbs chuck beef, cut into bite-size cubes",
    "6 garlic cloves, chopped",
    "1 tablespoon paprika",
    "Salt and pepper to taste",
    "1/2 pound potatoes",
    "2 Tablespoon olive oil",
    "180g bacon",
    "2.5 lb beef chuck, boneless short rib or any other slow cooking beef, cut into 2″ chunks",
]


def main() -> int:
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
    SQLModel.metadata.create_all(engine)

    print("parser: strangetom + Pint (deterministic)\n")
    total = 0.0
    with Session(engine) as db:
        for raw in CASES:
            t = time.monotonic()
            try:
                results = parse_line(raw, db)
            except Exception as e:
                print(f"  ✗ {raw!r}\n     ERROR: {e}\n")
                continue
            ms = (time.monotonic() - t) * 1000
            total += ms
            print(f"  raw: {raw!r}  ({ms:.0f}ms)  → {len(results)} ingredient(s)")
            for r in results:
                extras = []
                if r.original_qty is not None:
                    extras.append(f"orig={r.original_qty} {r.original_unit}")
                if r.prep_note:
                    extras.append(f"prep={r.prep_note!r}")
                if r.size_descriptor:
                    extras.append(f"size={r.size_descriptor!r}")
                if r.optional:
                    extras.append("OPTIONAL")
                if r.confidence is not None:
                    extras.append(f"conf={r.confidence:.3f}")
                print(f"     • {r.name!r}: {r.qty} {r.unit}  cat={r.category}")
                if extras:
                    print(f"       {' · '.join(extras)}")
            print()
    print(f"total: {total:.0f}ms for {len(CASES)} lines  →  ~{total/len(CASES):.0f}ms/line")
    return 0


if __name__ == "__main__":
    sys.exit(main())
