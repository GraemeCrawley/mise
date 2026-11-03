"""Regression test — every source_line ever imported should still parse
to the same (qty, unit, optional, canonical_name, category) it did at
snapshot time. Catches silent regressions when the parser is changed.

Snapshot lives at tests/data/parser_corpus.json. Regenerate after an
intentional change with:
    docker exec mise uv run python /app/scripts/snapshot_parser_corpus.py

Skips display_name comparison — that's LLM-derived and non-deterministic
across runs. canonical_name is the deterministic parser key.

Test runs through full parse_line (real strangetom). Big test (~340
cases) but each parametrised case is fast once the CRF is loaded.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from sqlmodel import Session

from mise.services.ingredient_parser import parse_line


_CORPUS_PATH = Path(__file__).parent / "data" / "parser_corpus.json"
_CORPUS = json.loads(_CORPUS_PATH.read_text(encoding="utf-8")) if _CORPUS_PATH.exists() else []


@pytest.mark.parametrize(
    "case",
    _CORPUS,
    ids=[c["source_line"][:60] for c in _CORPUS],
)
def test_parse_line_matches_snapshot(case: dict, session: Session) -> None:
    """parse_line should produce the same key fields as the snapshot.
    If this fails after an intentional change, re-snapshot and review
    the diff — that's the cleanup gate."""
    out = parse_line(case["source_line"], session)
    assert out, f"no output for {case['source_line']!r}"

    # Aggregate fields across all returned PIs (compound lines like
    # "Salt and pepper to taste" yield multiple). Match against the
    # FIRST one that matches the snapshot's canonical_name.
    pi = next(
        (p for p in out if p.canonical_name == case["canonical_name"]),
        out[0],
    )

    # qty + unit: allow small float wobble (LLM-driven category upgrade
    # can swap g↔lb etc.; we compare in the test as exact since the
    # snapshot captures post-preference values).
    assert pi.qty == case["qty"], (
        f"{case['source_line']!r}: qty drift "
        f"({case['qty']} → {pi.qty})"
    )
    assert pi.unit == case["unit"], (
        f"{case['source_line']!r}: unit drift "
        f"({case['unit']} → {pi.unit})"
    )
    assert pi.optional == case["optional"], (
        f"{case['source_line']!r}: optional drift "
        f"({case['optional']} → {pi.optional})"
    )
    assert pi.parse_failed == case["parse_failed"], (
        f"{case['source_line']!r}: parse_failed drift "
        f"({case['parse_failed']} → {pi.parse_failed})"
    )
    assert pi.canonical_name == case["canonical_name"], (
        f"{case['source_line']!r}: canonical_name drift "
        f"({case['canonical_name']!r} → {pi.canonical_name!r})"
    )
    assert pi.category == case["category"], (
        f"{case['source_line']!r}: category drift "
        f"({case['category']!r} → {pi.category!r})"
    )
