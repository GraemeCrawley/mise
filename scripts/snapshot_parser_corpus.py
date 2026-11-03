#!/usr/bin/env python3
"""Capture every (source_line → parsed output) pair from the live DB
into tests/data/parser_corpus.json. The regression test reads this
fixture and asserts parse_line still produces the same key fields
for each source.

Refresh after intentional parser changes:
  docker exec mise uv run python /app/scripts/snapshot_parser_corpus.py

The snapshot stores deterministic fields only — qty, unit, optional,
parse_failed, canonical_name. It SKIPS the LLM-derived display_name
since that's non-deterministic across imports.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlmodel import Session, select

from mise.db import engine
from mise.models import Ingredient


def main() -> int:
    """Snapshot from a CLEAN parse — re-run parse_line on each unique
    source_line and capture its output. This bypasses the live DB's
    LLM-verify overlay so the regression test compares like-with-like."""
    from sqlalchemy.pool import StaticPool
    from sqlmodel import SQLModel, create_engine

    from mise.services.ingredient_parser import parse_line

    # Collect unique source lines from the live DB.
    seen: list[str] = []
    with Session(engine()) as live:
        added: set[str] = set()
        for ing in live.exec(select(Ingredient)).all():
            src = ing.source_line
            if not src or src in added:
                continue
            added.add(src)
            seen.append(src)

    # Re-parse each in an isolated in-memory session so aliases /
    # parser cache don't bleed in.
    test_eng = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool,
    )
    SQLModel.metadata.create_all(test_eng)

    rows: list[dict] = []
    with Session(test_eng) as db:
        for src in seen:
            try:
                out = parse_line(src, db)
            except Exception as e:
                print(f"  parse error on {src!r}: {e}", file=sys.stderr)
                continue
            if not out:
                continue
            pi = out[0]
            rows.append({
                "source_line": src,
                "qty": pi.qty,
                "unit": pi.unit,
                "optional": pi.optional,
                "parse_failed": pi.parse_failed,
                "category": pi.category,
                "canonical_name": pi.canonical_name,
            })

    rows.sort(key=lambda r: r["source_line"])

    out = Path(__file__).parent.parent / "tests" / "data" / "parser_corpus.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(rows, indent=2, ensure_ascii=False) + "\n")
    print(f"wrote {len(rows)} unique source lines to {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
