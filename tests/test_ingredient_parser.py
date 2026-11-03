"""Tests for the ingredient parser. Strangetom is stubbed via the
`strangetom=` keyword arg — real CRF inference happens in
scripts/strangetom_smoke.py, not in unit tests.

Coverage:
  - normalize() / cache_key() pure + stable
  - Section detection heuristic
  - Unit canonicalisation (Pint conversion + rounding)
  - Category prefix lookup
  - parse_line: list-output shape, cache hit/miss, version invalidation
  - parse_blob: section routing, per-line failure isolation
"""

from __future__ import annotations

from dataclasses import dataclass, field
from fractions import Fraction

from sqlmodel import Session

from mise.models import ParserCache
from mise.services.ingredient_parser import (
    ParserError,
    cache_key,
    is_section_header,
    normalize,
    parse_blob,
    parse_line,
    resolve_category,
    split_blob_into_sections,
    to_canonical,
)

# --- normalisation / cache key ------------------------------------------


def test_normalize_strips_and_collapses_whitespace() -> None:
    assert normalize("  2  tbsp   olive   oil  ") == "2 tbsp olive oil"
    assert normalize("\n2 tbsp\tolive oil\n") == "2 tbsp olive oil"


def test_cache_key_stable_for_equivalent_inputs() -> None:
    assert cache_key("  2 tbsp olive oil ") == cache_key("2 tbsp olive oil")


def test_cache_key_differs_when_content_differs() -> None:
    assert cache_key("2 tbsp olive oil") != cache_key("3 tbsp olive oil")


# --- section detection --------------------------------------------------


def test_is_section_header_short_label_with_colon() -> None:
    assert is_section_header("Marinade:")
    assert is_section_header("For the sauce:")
    assert is_section_header("Garnish:")


def test_is_section_header_rejects_quantity_lines() -> None:
    assert not is_section_header("1 tsp salt: optional")
    assert not is_section_header("2 tbsp olive oil:")


def test_is_section_header_rejects_long_sentences() -> None:
    assert not is_section_header("Combine all the dry ingredients in a bowl and then:")


def test_split_blob_with_no_sections() -> None:
    blob = "2 tbsp olive oil\n1 yellow onion, diced"
    sections = split_blob_into_sections(blob)
    assert sections == [(None, ["2 tbsp olive oil", "1 yellow onion, diced"])]


def test_split_blob_with_explicit_sections() -> None:
    blob = "Marinade:\n2 tbsp soy sauce\n\nFor the sauce:\n1/2 cup mayo\n"
    sections = split_blob_into_sections(blob)
    assert [s[0] for s in sections] == ["Marinade", "For the sauce"]


# --- unit canonicalisation ---------------------------------------------


def test_to_canonical_weight_conversion() -> None:
    # Natural unit: lb stays lb, no conversion to g.
    qty, unit, orig_q, orig_u = to_canonical(2.0, "lb")
    assert qty == 2.0
    assert unit == "lb"
    assert orig_q is None
    assert orig_u is None


def test_to_canonical_volume_conversion() -> None:
    # Natural unit: tablespoon stays as tbsp (abbreviated), no ml conversion.
    qty, unit, orig_q, orig_u = to_canonical(1.0, "tablespoon")
    assert qty == 1.0
    assert unit == "tbsp"
    assert orig_q is None
    assert orig_u is None


def test_to_canonical_no_conversion_when_already_canonical() -> None:
    qty, unit, orig_q, orig_u = to_canonical(180.0, "gram")
    assert qty == 180
    assert unit == "g"
    assert orig_q is None
    assert orig_u is None


def test_to_canonical_handles_plural_caps_input() -> None:
    # "Tablespoons" is normalised → "tbsp"; qty preserved as-is.
    qty, unit, orig_q, orig_u = to_canonical(2.0, "Tablespoons")
    assert qty == 2.0
    assert unit == "tbsp"


def test_to_canonical_count_units() -> None:
    qty, unit, orig_q, orig_u = to_canonical(6.0, "")
    assert qty == 6
    assert unit == "ea"
    assert orig_q is None


def test_to_canonical_package_units_kept_verbatim() -> None:
    qty, unit, orig_q, orig_u = to_canonical(2.0, "can")
    assert qty == 2
    assert unit == "can"
    assert orig_q is None


def test_to_canonical_rounds_canonical_qty() -> None:
    # Natural unit: 0.5 lb stays 0.5 lb (no g conversion).
    qty, unit, _, _ = to_canonical(0.5, "lb")
    assert qty == 0.5
    assert unit == "lb"
    assert isinstance(qty, float)


def test_to_canonical_rounds_original_qty_to_2dp() -> None:
    # No conversion happens; qty itself rounds to 2dp via _round_original.
    qty, unit, orig_q, _ = to_canonical(0.3333, "lb")
    assert qty == 0.33  # 2dp rounding
    assert orig_q is None  # no conversion, so no original_*


# --- category resolution -----------------------------------------------


def test_resolve_category_prefix_match() -> None:
    assert resolve_category("Beef, steak, chuck", None) == "Meat"
    assert resolve_category("Garlic, raw", None) == "Produce"
    assert resolve_category("Olive oil", None) == "Pantry"
    assert resolve_category("Salt, table, iodized", None) == "Spices"


def test_resolve_category_falls_back_to_name_when_no_foundation() -> None:
    assert resolve_category(None, "Garlic, raw") == "Produce"


def test_resolve_category_unknown_returns_other() -> None:
    assert resolve_category("Quinoa-and-kale fusion thing", None) == "Pasta/Rice"  # 'Quinoa' prefix
    assert resolve_category("Mystery ingredient", None) == "Other"


# --- parse_line tests (strangetom stubbed) -----------------------------


@dataclass
class FakeStrangetomField:
    text: str | None = None
    confidence: float = 1.0


@dataclass
class FakeAmount:
    quantity: Fraction
    unit: str
    text: str = ""
    confidence: float = 1.0


@dataclass
class FakeStrangetomResult:
    name: FakeStrangetomField | None = None
    amount: list[FakeAmount] = None
    preparation: FakeStrangetomField | None = None
    size: FakeStrangetomField | None = None
    comment: FakeStrangetomField | None = None
    foundation_foods: list[FakeStrangetomField] = None

    def __post_init__(self) -> None:
        if self.amount is None:
            self.amount = []
        if self.foundation_foods is None:
            self.foundation_foods = []


def _stub_strangetom_olive_oil(raw: str) -> FakeStrangetomResult:
    """Stub: pretends strangetom returned a parse for olive oil."""
    return FakeStrangetomResult(
        amount=[FakeAmount(quantity=Fraction(2, 1), unit="tablespoon", text="2 tablespoon", confidence=0.99)],
        foundation_foods=[FakeStrangetomField(text="Olive oil", confidence=1.0)],
    )


def test_parse_line_returns_list(session: Session) -> None:
    """Even single-ingredient lines return a list — caller iterates."""
    parsed = parse_line("2 tablespoon olive oil", session, strangetom=_stub_strangetom_olive_oil)
    assert isinstance(parsed, list)
    assert len(parsed) == 1


def test_parse_line_single_ingredient(session: Session) -> None:
    parsed = parse_line("2 tablespoon olive oil", session, strangetom=_stub_strangetom_olive_oil)[0]
    # apply_category_preference restores 2 tbsp from Pantry-preferred list
    # instead of leaving the canonical 30 ml — tbsp is shopping-natural.
    assert parsed.qty == 2
    assert parsed.unit == "tbsp"
    assert parsed.name == "olive oil"
    assert parsed.category == "Pantry"


def test_parse_line_compound_yields_multiple(session: Session) -> None:
    """Salt and pepper to taste → two ParsedIngredients (no quantity present)."""
    def stub(raw: str) -> FakeStrangetomResult:
        return FakeStrangetomResult(
            comment=FakeStrangetomField(text="to taste", confidence=0.99),
            foundation_foods=[
                FakeStrangetomField(text="Salt, table, iodized", confidence=1.0),
                FakeStrangetomField(text="Spices, pepper, black", confidence=1.0),
            ],
        )
    parsed = parse_line("Salt and pepper to taste", session, strangetom=stub)
    assert len(parsed) == 2
    # Name now derives from source line ("Salt and pepper") rather than
    # the FDC foundation_food (which would be "Salt, table, iodized" etc).
    # Both ingredients come from the same source line so name is the
    # same; foundation_food preserves the FDC distinction.
    assert all(p.optional for p in parsed)
    assert all(p.qty == 0 for p in parsed)
    assert parsed[0].foundation_food == "Salt, table, iodized"
    assert parsed[1].foundation_food == "Spices, pepper, black"


def test_parse_line_quantified_alternatives_picks_first(session: Session) -> None:
    """'2.5 lb beef chuck, boneless short rib or any other slow cooking beef'
    → ONE ingredient (the 'or' lists alternatives for the same purchase).
    Strangetom returns multiple foundation_foods; quantified line means
    we take the highest-confidence one (foundations[0])."""
    def stub(raw: str) -> FakeStrangetomResult:
        return FakeStrangetomResult(
            amount=[FakeAmount(quantity=Fraction(5, 2), unit="pound", confidence=0.99)],
            preparation=FakeStrangetomField(text="cut into chunks", confidence=0.99),
            foundation_foods=[
                FakeStrangetomField(text="Beef, steak, chuck", confidence=0.887),
                FakeStrangetomField(text="Beef, chuck, short ribs, boneless", confidence=0.817),
            ],
        )
    parsed = parse_line("2.5 lb beef chuck, or any other", session, strangetom=stub)
    assert len(parsed) == 1
    assert "beef chuck" in parsed[0].name.lower() or parsed[0].name == "Beef, steak, chuck"
    # apply_category_preference keeps lb (Meat's preferred unit) instead
    # of converting to canonical grams.
    assert parsed[0].qty == 2.5
    assert parsed[0].unit == "lb"


def test_parse_line_caches_on_first_call(session: Session) -> None:
    call_count = {"n": 0}

    def counting(raw: str) -> FakeStrangetomResult:
        call_count["n"] += 1
        return _stub_strangetom_olive_oil(raw)

    parse_line("2 tablespoon olive oil", session, strangetom=counting)
    assert call_count["n"] == 1
    # Persisted
    assert session.get(ParserCache, cache_key("2 tablespoon olive oil")) is not None


def test_parse_line_uses_cache_on_second_call(session: Session) -> None:
    raw = "2 tablespoon olive oil"
    # Prime cache
    parse_line(raw, session, strangetom=_stub_strangetom_olive_oil)

    def explode(raw: str) -> FakeStrangetomResult:
        raise AssertionError("Should not be called on cache hit")

    parsed = parse_line(raw, session, strangetom=explode)
    assert parsed[0].name == "olive oil"


def test_parse_line_version_bump_invalidates(session: Session) -> None:
    raw = "2 tablespoon olive oil"
    parse_line(raw, session, strangetom=_stub_strangetom_olive_oil)

    def fresh(raw: str) -> FakeStrangetomResult:
        return FakeStrangetomResult(
            amount=[FakeAmount(quantity=Fraction(2, 1), unit="tablespoon")],
            foundation_foods=[FakeStrangetomField(text="Olive oil (re-parsed)", confidence=1.0)],
        )

    parsed = parse_line(raw, session, strangetom=fresh, parser_version="v3-test")
    # Name now derives from source line — version bump invalidated the
    # ParserCache so we know fresh-parse ran; verify via foundation_food.
    assert parsed[0].foundation_food == "Olive oil (re-parsed)"


def test_parse_line_strangetom_failure_raises(session: Session) -> None:
    def boom(raw: str) -> FakeStrangetomResult:
        raise RuntimeError("CRF inference failed")

    try:
        parse_line("anything", session, strangetom=boom)
    except ParserError as e:
        assert "CRF inference failed" in str(e)
    else:
        raise AssertionError("expected ParserError")


# --- parse_blob ---------------------------------------------------------


def test_parse_blob_routes_to_sections(session: Session) -> None:
    blob = "Marinade:\n2 tablespoon soy sauce\nSauce:\n1 cup mayo\n"

    def stub(raw: str) -> FakeStrangetomResult:
        return FakeStrangetomResult(
            amount=[FakeAmount(quantity=Fraction(1, 1), unit="ea")],
            foundation_foods=[FakeStrangetomField(text=raw, confidence=1.0)],
        )

    sections = parse_blob(blob, session, strangetom=stub)
    assert [s.name for s in sections] == ["Marinade", "Sauce"]
    assert len(sections[0].lines) == 1
    # Source-derived name: stripped "2 tablespoon " → "soy sauce".
    assert sections[0].lines[0].name == "soy sauce"


def test_parse_blob_collects_failures(session: Session) -> None:
    blob = "2 tablespoon olive oil\nbroken line\n1 yellow onion"

    def selective(raw: str) -> FakeStrangetomResult:
        if "broken" in raw:
            raise RuntimeError("simulated")
        return FakeStrangetomResult(
            amount=[FakeAmount(quantity=Fraction(1, 1), unit="ea")],
            foundation_foods=[FakeStrangetomField(text="X", confidence=1.0)],
        )

    sections = parse_blob(blob, session, strangetom=selective)
    assert len(sections) == 1
    assert len(sections[0].lines) == 2
    assert len(sections[0].failed) == 1


# --- category-preferred display units -----------------------------------


def test_apply_category_preference_restores_original_when_preferred() -> None:
    from mise.services.ingredient_parser import apply_category_preference

    # 15 ml ← 1 tbsp soy sauce; category Sauces (tbsp preferred) → restore
    qty, unit, oq, ou = apply_category_preference(15.0, "ml", 1.0, "tbsp", "Sauces")
    assert (qty, unit, oq, ou) == (1.0, "tbsp", None, None)


def test_apply_category_preference_normalises_long_form_original() -> None:
    from mise.services.ingredient_parser import apply_category_preference

    # strangetom returns "teaspoon" — should restore as abbreviated "tsp".
    qty, unit, oq, ou = apply_category_preference(1.0, "ml", 0.25, "teaspoon", "Produce")
    assert unit == "tsp"
    assert qty == 0.25
    assert oq is None and ou is None


def test_apply_category_preference_converts_to_default_when_no_original() -> None:
    from mise.services.ingredient_parser import apply_category_preference

    # 454 g → 1 lb is clean; should convert.
    qty, unit, oq, ou = apply_category_preference(454.0, "g", None, None, "Meat")
    assert unit == "lb"
    assert abs(qty - 1.0) <= 0.02
    assert oq is None and ou is None


def test_apply_category_preference_keeps_canonical_when_conversion_isnt_clean() -> None:
    from mise.services.ingredient_parser import apply_category_preference

    # 100 g → 0.22 lb is not clean; should stay as g.
    qty, unit, _, _ = apply_category_preference(100.0, "g", None, None, "Meat")
    assert (qty, unit) == (100.0, "g")


def test_apply_category_preference_keeps_canonical_on_cross_dimension() -> None:
    from mise.services.ingredient_parser import apply_category_preference

    # 5 ml of garlic, no original; Produce default is "ea" — ml→ea is
    # cross-dimension (no density). Rule says keep canonical.
    qty, unit, oq, ou = apply_category_preference(5.0, "ml", None, None, "Produce")
    assert (qty, unit) == (5.0, "ml")


def test_apply_category_preference_other_category_no_change() -> None:
    from mise.services.ingredient_parser import apply_category_preference

    # Empty preferred list → leave everything alone.
    qty, unit, oq, ou = apply_category_preference(100.0, "g", None, None, "Other")
    assert (qty, unit, oq, ou) == (100.0, "g", None, None)


# --- parse-failure guard ------------------------------------------------


def test_parse_failed_when_no_qty_and_not_optional(session: Session) -> None:
    """A USDA reference name in the ingredient column with no qty
    prefix (e.g. 'Turkey, dark meat, meat and skin, raw') gets
    flagged as parse_failed."""
    @dataclass
    class FakeFF:
        text: str = "Turkey, dark meat, meat and skin, raw"
        confidence: float = 0.9

    @dataclass
    class FakeResult:
        amount: list = field(default_factory=list)
        foundation_foods: list = field(default_factory=lambda: [FakeFF()])
        name = None
        preparation = None
        size = None
        comment = None

    out = parse_line("Turkey, dark meat, meat and skin, raw", session, strangetom=lambda raw: FakeResult())
    assert len(out) == 1
    assert out[0].qty == 0
    assert out[0].optional is False
    assert out[0].parse_failed is True


def test_parse_not_failed_when_qty_present(session: Session) -> None:
    @dataclass
    class FakeFF:
        text: str = "Beef, ground, raw"
        confidence: float = 0.9

    @dataclass
    class FakeResult:
        amount: list = field(default_factory=lambda: [FakeAmount(quantity=Fraction(100, 1), unit="g")])
        foundation_foods: list = field(default_factory=lambda: [FakeFF()])
        name = None
        preparation = None
        size = None
        comment = None

    out = parse_line("100 g ground beef", session, strangetom=lambda raw: FakeResult())
    assert out[0].parse_failed is False


def test_parse_not_failed_when_optional(session: Session) -> None:
    """'Salt, to taste' should NOT be flagged — it's a legit optional row."""
    @dataclass
    class FakeFF:
        text: str = "Salt, table, iodized"
        confidence: float = 0.9

    @dataclass
    class FakeComment:
        text: str = "to taste"

    @dataclass
    class FakeResult:
        amount: list = field(default_factory=list)
        foundation_foods: list = field(default_factory=lambda: [FakeFF()])
        name = None
        preparation = None
        size = None
        comment = FakeComment()

    out = parse_line("salt, to taste", session, strangetom=lambda raw: FakeResult())
    assert out[0].optional is True
    assert out[0].parse_failed is False


# --- recipe-clean number gating + apply_category_preference rule 2 ------


def test_is_recipe_clean_accepts_integers_and_common_fractions() -> None:
    from mise.services.ingredient_parser import _is_recipe_clean
    assert _is_recipe_clean(1.0)
    assert _is_recipe_clean(2.5)         # 2½
    assert _is_recipe_clean(0.25)        # ¼
    assert _is_recipe_clean(0.333)       # ⅓
    assert _is_recipe_clean(0.75)        # ¾


def test_is_recipe_clean_rejects_garbage() -> None:
    from mise.services.ingredient_parser import _is_recipe_clean
    assert not _is_recipe_clean(0.22)    # 100 g → 0.22 lb
    assert not _is_recipe_clean(0.06)    # 15 ml → 0.06 cup
    assert not _is_recipe_clean(1.69)    # 400 ml → 1.69 cup


def test_rule2_skipped_when_result_isnt_clean() -> None:
    """100 g of beef shouldn't become 0.22 lb — keep canonical g."""
    from mise.services.ingredient_parser import apply_category_preference
    qty, unit, oq, ou = apply_category_preference(100.0, "g", None, None, "Meat")
    assert (qty, unit) == (100.0, "g")


def test_rule2_applies_when_result_is_clean() -> None:
    """454 g → 1 lb is clean; should convert."""
    from mise.services.ingredient_parser import apply_category_preference
    qty, unit, _, _ = apply_category_preference(454.0, "g", None, None, "Meat")
    assert unit == "lb"
    assert abs(qty - 1.0) <= 0.02
