"""Tests for canonical_resolver.

Phase 0 cut: deterministic only (no LLM). Coverage:
  - snake_case generation correctness
  - Empty / "unknown" inputs short-circuit without cache writes
  - First call writes IngredientAlias with source="auto"
  - Subsequent calls hit the cache (no second write)
  - Manual override (source="manual") survives a resolve call
"""

from __future__ import annotations

from sqlmodel import Session, select

from mise.models import IngredientAlias
from mise.services.canonical_resolver import resolve, snake_case

# --- snake_case pure function -----------------------------------------


def test_snake_case_basic() -> None:
    assert snake_case("Yellow Onion") == "yellow_onion"
    assert snake_case("Olive oil") == "olive_oil"


def test_snake_case_fdc_format() -> None:
    assert snake_case("Beef, steak, chuck") == "beef_steak_chuck"
    assert snake_case("Salt, table, iodized") == "salt_table_iodized"
    assert snake_case("Garlic, raw") == "garlic_raw"


def test_snake_case_collapses_runs() -> None:
    assert snake_case("Extra  virgin    olive   oil") == "extra_virgin_olive_oil"


def test_snake_case_strips_punctuation() -> None:
    assert snake_case("ground (black) pepper!!!") == "ground_black_pepper"


def test_snake_case_empty_returns_unknown() -> None:
    assert snake_case("") == "unknown"
    assert snake_case("   ") == "unknown"
    assert snake_case("!!!") == "unknown"


# --- resolve() integration with the alias cache -----------------------


def test_resolve_empty_short_circuits_without_writing(session: Session) -> None:
    canonical, conf, source = resolve("", session)
    assert canonical == "unknown"
    assert conf == 0.0
    assert source == "default"
    # No row should be inserted for empty input
    rows = session.exec(select(IngredientAlias)).all()
    assert rows == []


def test_resolve_first_call_writes_alias(session: Session) -> None:
    canonical, conf, source = resolve("Beef, steak, chuck", session)
    assert canonical == "beef_steak_chuck"
    assert conf == 0.5
    assert source == "auto"

    alias = session.get(IngredientAlias, "beef, steak, chuck")
    assert alias is not None
    assert alias.canonical_name == "beef_steak_chuck"
    assert alias.source == "auto"


def test_resolve_second_call_uses_cache(session: Session) -> None:
    resolve("Beef, steak, chuck", session)
    # Second call returns the cached row (not a fresh insert)
    canonical, conf, source = resolve("Beef, steak, chuck", session)
    assert canonical == "beef_steak_chuck"
    # Exactly one alias row exists
    rows = session.exec(select(IngredientAlias)).all()
    assert len(rows) == 1


def test_resolve_is_case_insensitive_on_input(session: Session) -> None:
    resolve("Beef, Steak, Chuck", session)
    canonical, _, _ = resolve("BEEF, STEAK, CHUCK", session)
    assert canonical == "beef_steak_chuck"
    rows = session.exec(select(IngredientAlias)).all()
    assert len(rows) == 1  # second call hit the cache


def test_resolve_respects_manual_override(session: Session) -> None:
    # User has manually pinned 'olive oil' to a non-default canonical
    session.add(
        IngredientAlias(
            display_name="olive oil",
            canonical_name="extra_virgin_olive_oil",
            confidence=1.0,
            source="manual",
        )
    )
    session.commit()

    canonical, conf, source = resolve("Olive oil", session)
    assert canonical == "extra_virgin_olive_oil"
    assert conf == 1.0
    assert source == "manual"
