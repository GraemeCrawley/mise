"""Tests for llm_verify. The Anthropic call itself is stubbed via
monkey-patching _llm_verify — real round-trips happen in a manual
smoke script, not in unit tests."""

from __future__ import annotations

from sqlmodel import Session, select

from mise.models import IngredientAlias
from mise.services.ingredient_parser import ParsedIngredient
from mise.services.llm_verify import VerifyOutput, verify_ingredient


def _make_pi(name: str, category: str = "Other", foundation: str | None = None) -> ParsedIngredient:
    return ParsedIngredient(
        qty=1.0,
        unit="ea",
        name=name,
        canonical_name=name.lower().replace(" ", "_"),
        category=category,
        foundation_food=foundation,
    )


def test_off_mode_returns_unchanged(session: Session, monkeypatch) -> None:
    called = {"n": 0}

    def explode(*a, **k):
        called["n"] += 1
        raise AssertionError("LLM should not be called in off mode")

    monkeypatch.setattr("mise.services.llm_verify._llm_verify", explode)
    pi = _make_pi("Peppers, hungarian, raw", category="Produce")
    out = verify_ingredient(pi, session, mode="off")
    assert out == pi
    assert called["n"] == 0


def test_first_seen_skips_well_categorised(session: Session, monkeypatch) -> None:
    """When prefix map already gave a non-Other category and there's no
    auto-source alias for this foundation_food, first-seen mode skips
    the LLM call entirely."""
    monkeypatch.setattr("mise.services.llm_verify._llm_verify", lambda **k: VerifyOutput(canonical_name="X", category="X", display_name_pretty="x"))

    pi = _make_pi("Olive oil", category="Pantry", foundation="Olive oil")
    out = verify_ingredient(pi, session, mode="first-seen")
    # Should not change anything — first-seen + good guess = no LLM
    assert out.category == "Pantry"
    assert out.canonical_name == pi.canonical_name


def test_first_seen_fires_when_category_is_other(session: Session, monkeypatch) -> None:
    calls = []

    def stub(**k):
        calls.append(k)
        return VerifyOutput(canonical_name="paprika", category="Spices", display_name_pretty="paprika")

    monkeypatch.setattr("mise.services.llm_verify._llm_verify", stub)

    pi = _make_pi(
        "Peppers, hungarian, raw",
        category="Produce",  # prefix map's wrong answer
        foundation="Peppers, hungarian, raw",
    )
    # Force first-seen by lying that category is Other
    pi = pi.model_copy(update={"category": "Other"})
    out = verify_ingredient(pi, session, mode="first-seen")
    assert len(calls) == 1
    assert out.canonical_name == "paprika"
    assert out.category == "Spices"

    # Cached
    alias = session.get(IngredientAlias, "peppers, hungarian, raw")
    assert alias is not None
    assert alias.source == "llm"
    assert alias.canonical_name == "paprika"
    assert alias.category == "Spices"


def test_existing_llm_alias_short_circuits(session: Session, monkeypatch) -> None:
    """If we've already verified this foundation_food (source='llm'),
    return the cached values without calling the LLM."""
    session.add(
        IngredientAlias(
            display_name="peppers, hungarian, raw",
            canonical_name="paprika",
            category="Spices",
            confidence=0.9,
            source="llm",
        )
    )
    session.commit()

    explode_calls = {"n": 0}

    def explode(**k):
        explode_calls["n"] += 1
        raise AssertionError("Should not call LLM when alias is already source=llm")

    monkeypatch.setattr("mise.services.llm_verify._llm_verify", explode)

    pi = _make_pi(
        "Peppers, hungarian, raw", category="Other", foundation="Peppers, hungarian, raw"
    )
    out = verify_ingredient(pi, session, mode="every")  # even "every" should respect cache
    assert explode_calls["n"] == 0
    assert out.canonical_name == "paprika"
    assert out.category == "Spices"


def test_manual_alias_overrides_llm(session: Session, monkeypatch) -> None:
    """source='manual' (user-pinned) wins over any LLM output."""
    session.add(
        IngredientAlias(
            display_name="bacon",
            canonical_name="bacon",
            category="Meat",
            confidence=1.0,
            source="manual",
        )
    )
    session.commit()

    monkeypatch.setattr(
        "mise.services.llm_verify._llm_verify",
        lambda **k: VerifyOutput(canonical_name="WRONG", category="Other", display_name_pretty="wrong"),
    )

    pi = _make_pi("Beef, bacon, cooked", category="Other", foundation="bacon")
    out = verify_ingredient(pi, session, mode="every")
    assert out.canonical_name == "bacon"
    assert out.category == "Meat"


def test_llm_returns_none_falls_back_to_deterministic(session: Session, monkeypatch) -> None:
    monkeypatch.setattr("mise.services.llm_verify._llm_verify", lambda **k: None)
    pi = _make_pi("Something weird", category="Other", foundation="Something weird")
    out = verify_ingredient(pi, session, mode="first-seen")
    assert out == pi
    # No alias written when verify fails
    assert session.exec(select(IngredientAlias)).all() == []


def test_grounded_in_source_accepts_partial_overlap() -> None:
    """'flour' → 'all_purpose_flour' is a legit refinement (one word
    overlap is enough)."""
    from mise.services.llm_verify import _llm_output_grounded_in_source
    assert _llm_output_grounded_in_source("all_purpose_flour", "2 cups flour")


def test_grounded_in_source_rejects_hallucination() -> None:
    """'vanilla bean' → 'canned_beans' is a hallucination — no word
    in canonical appears in source. Reject."""
    from mise.services.llm_verify import _llm_output_grounded_in_source
    assert not _llm_output_grounded_in_source(
        "canned_beans", "1 Vanilla Bean (Split & Scraped)"
    )


def test_grounded_in_source_accepts_modifier_kept() -> None:
    """'kidney bean' → 'kidney_bean' grounds on both words."""
    from mise.services.llm_verify import _llm_output_grounded_in_source
    assert _llm_output_grounded_in_source("kidney_bean", "1 cup kidney beans")


def test_grounded_in_source_trusts_when_no_source() -> None:
    """No source_line → no signal → trust the LLM."""
    from mise.services.llm_verify import _llm_output_grounded_in_source
    assert _llm_output_grounded_in_source("anything_at_all", None)


def test_grounded_in_source_strips_accents() -> None:
    """'jalapeño' source grounds 'jalapeno' canonical — accent-stripping
    is a legit canonicalization, not a hallucination."""
    from mise.services.llm_verify import _llm_output_grounded_in_source
    assert _llm_output_grounded_in_source(
        "jalapeno", "1 jalapeño, seeds removed, finely chopped"
    )
    assert _llm_output_grounded_in_source("pate_brisee", "pâte brisée")
