"""Corpus tests — every line that landed in the unparsed pile during
the 2026-05-30 import, with the expected parser behaviour. Driving
improvements to ingredient_parser.py + helpers.

Each case is one of four classes:

  A. Parseable — strangetom + our pipeline should extract qty/unit/name.
  B. Section header — should be detected by is_section_header.
  C. Name-only — no qty, plausible ingredient name. Should mark optional
     with the name preserved (rather than going to parse_failed=True).
  D. Out of scope — foreign language, gibberish; we expect parse_failed=True.

Tests use the real strangetom (no stub). The module-level CRF load is
amortised across the corpus; individual cases are fast.
"""

from __future__ import annotations

import pytest
from sqlmodel import Session

from mise.services.ingredient_parser import (
    is_section_header,
    parse_line,
)


# --- Class A: should parse cleanly ---------------------------------------
#
# (source_line, expected_qty, expected_unit, name_substring_in_display)

CLASS_A = [
    # Trivial counts
    ("1 cinnamon stick",          1.0, "ea",    "cinnamon"),
    ("2 cinnamon sticks",         2.0, "ea",    "cinnamon"),
    ("1 Cinnamon Stick",          1.0, "ea",    "cinnamon"),
    ("4 black peppercorns",       4.0, "ea",    "peppercorn"),
    ("1 large french baguette",   1.0, "ea",    "baguette"),
    ("1 jalapeño, seeds removed, finely chopped", 1.0, "ea", "jalape"),

    # Standard tsp/tbsp/cup quantities
    ("1/2 tsp fine sea salt",     0.5, "tsp",   "salt"),
    ("1 tsp fine sea salt",       1.0, "tsp",   "salt"),
    ("1/2 tsp ground pepper",     0.5, "tsp",   "pepper"),
    ("1 teaspoon whole peppercorn", 1.0, "tsp", "peppercorn"),
    ("2 tsp dried mixed herbs",   2.0, "tsp",   "herb"),
    ("1 teaspoon dried rose petals (optional)", 1.0, "tsp", "rose"),
    ("2 Tbsp gochujang",          2.0, "tbsp",  "gochujang"),
    ("1 tbsp. taco seasoning",    1.0, "tbsp",  "taco"),     # trailing period
    ("2 tbsp creamed horseradish spread", 2.0, "tbsp", "horseradish"),
    ("1 tablespoon chopped fresh sage", 1.0, "tbsp", "sage"),
    ("1 teaspoon grated fresh ginger",  1.0, "tsp",  "ginger"),
    ("1 tsp instant yeast",       1.0, "tsp",   "yeast"),
    ("1 tsp. kosher salt, plus more", 1.0, "tsp", "salt"),

    # Cups
    ("1 cup corn kernels, drained", 1.0, "cup", "corn"),
    ("1/4 cup mayo",              0.25, "cup",  "mayo"),
    ("2 cups cooked white or brown rice, cooled", 2.0, "cup", "rice"),

    # Mixed fractions
    ("1 ½ teaspoons sambal oelek", 1.5, "tsp",  "sambal"),
    ("1 1/2 tsp smoked paprika",   1.5, "tsp",  "paprika"),
    ("1 ½ teaspoons smoked paprika*", 1.5, "tsp", "paprika"),  # trailing *

    # Leading fraction with no unit — strangetom misses it, the
    # _extract_leading_qty fallback recovers count + unit=ea.
    ("½ red onion, finely sliced", 0.5, "ea", "onion"),
    ("1/4 sour cream",             0.25, "ea", "sour cream"),

    # Range — take the first number
    ("1-2 green cardamoms",        1.0, "ea",   "cardamom"),

    # NOTE: "1 inch ginger" / "1 inch cinnamon" not in Class A —
    # strangetom doesn't recognise "inch" as a unit, so qty is dropped.
    # The ingredient still appears on the card (as optional cinnamon /
    # ginger) but without the inch count. Known limitation; would
    # require a pre-strangetom regex for length units.

    # Compound spice powders
    ("1 tbsp chili/cayenne powder", 1.0, "tbsp", "chili"),
    ("3 tsp ground coriander",      3.0, "tsp",  "coriander"),
    ("1/2 tsp coriander powder",    0.5, "tsp",  "coriander"),
    ("1 tsp kasuri methi",          1.0, "tsp",  "kasuri"),
]


# --- Class B: section headers --------------------------------------------

CLASS_B_HEADERS = [
    "Garnish",
    "Gravy",
    "To Saute & Puree",
    "Yogurt Sauce",
    "MEAT AND MEAT SAUCE",                  # caught by existing CAPS rule
    "VEGETABLES AND OTHERS",
    "BIBIMBAP SAUCE",
    "DRINK",
    "GARNISH",
    "optional garnishes: citrus slices (orange, lemon and/or lime), extra cinnamon sticks, extra star anise",
    "toppings: chopped fresh chives or green onions, freshly-cracked black pepper",
    "garnishes (avocado, tortilla chips, etc)",
]


# --- Class C: name-only lines (no qty; plausible ingredient name) --------

CLASS_C = [
    "lime zest",
    "sliced limes",
    "fresh peppermint",
    "Sesame seeds",
    "Thyme",
    "Freshly ground black pepper",
    "Salt and pepper",
    "Extra virgin olive oil",
    "fine sea salt",
    "Cooking oil",
    "baguette",
    # NOTE: "grated or sliced cheese (such as Gruyere, Asiago, ...)"
    # is a complex compound with parenthetical alternatives and "or"
    # connectives — strangetom returns name="unknown", source fallback
    # keeps it visible on the card.
]


# --- Class D: out-of-scope / unparseable ---------------------------------

CLASS_D = [
    "pâte brisée",                          # French
    "tomates et tomates cerises",
    "miel",
    "vinaigre balsamique",
    "huile d’olive",
    "pignons",
    "basilc",
    "sel",
    "poivre",
    "jaune d’oeuf",
    "burrata",
    "oignons confits",
    "3 to 4 serving portions of steamed rice",
    "Some toasted seasoned seaweed, shredded (long thin cut)",
]


# --- the tests ----------------------------------------------------------

_UNIT_ALIASES = {
    "tsp": ("tsp", "teaspoon", "teaspoons"),
    "tbsp": ("tbsp", "tablespoon", "tablespoons"),
    "cup": ("cup", "cups"),
    "ea": ("ea",),
    "inch": ("inch", "inches", "in"),
}


@pytest.mark.parametrize("source,expected_qty,expected_unit,name_substr", CLASS_A)
def test_class_a_parses_cleanly(
    session: Session, source: str, expected_qty: float,
    expected_unit: str, name_substr: str,
) -> None:
    """Lines with a clear qty + unit + name. We accept either the
    post-category-preference values (qty, unit) OR the preserved
    original_qty/original_unit — apply_category_preference may
    canonicalise to g/ml when the category prefix doesn't match."""
    out = parse_line(source, session)
    assert out, f"parser returned nothing for {source!r}"
    pi = out[0]
    assert not pi.parse_failed, (
        f"unexpected parse_failed for {source!r} "
        f"(qty={pi.qty} unit={pi.unit!r} name={pi.name!r})"
    )
    aliases = _UNIT_ALIASES.get(expected_unit, (expected_unit,))
    matched = (
        pi.qty == expected_qty and pi.unit in aliases
    ) or (
        pi.original_qty == expected_qty
        and (pi.original_unit or "").lower() in aliases
    )
    assert matched, (
        f"{source!r}: expected {expected_qty} {expected_unit}; "
        f"got qty={pi.qty} unit={pi.unit!r} "
        f"(original={pi.original_qty} {pi.original_unit!r})"
    )
    haystack = (pi.name or "").lower()
    assert name_substr.lower() in haystack, (
        f"{source!r}: expected {name_substr!r} in name={pi.name!r}"
    )


@pytest.mark.parametrize("header", CLASS_B_HEADERS)
def test_class_b_section_headers(header: str) -> None:
    assert is_section_header(header), f"not detected as header: {header!r}"


@pytest.mark.parametrize("source", CLASS_C)
def test_class_c_name_only_marks_optional(session: Session, source: str) -> None:
    """A line with a plausible ingredient name but no quantity should
    NOT propagate as parse_failed. Mark optional, preserve the name."""
    out = parse_line(source, session)
    assert out
    pi = out[0]
    assert not pi.parse_failed, (
        f"{source!r}: should not be parse_failed (name-only, mark optional)"
    )
    assert pi.optional, f"{source!r}: should be marked optional"
    # Name should be something recognisable, not "unknown".
    assert pi.name and pi.name != "unknown", (
        f"{source!r}: name was {pi.name!r}"
    )


@pytest.mark.parametrize("source", CLASS_D)
def test_class_d_unparseable_falls_through_with_name(
    session: Session, source: str,
) -> None:
    """Foreign language / unusual phrasing — we don't need full structured
    parse, but the ingredient must still EXIST on the card. Either it's
    flagged parse_failed (preview falls back to source_line), OR it's
    marked optional with a derived name (also visible). Either is fine —
    what's NOT fine is silently dropping the row."""
    out = parse_line(source, session)
    assert out, f"parser returned nothing for {source!r}"
    pi = out[0]
    visible_on_card = (
        pi.parse_failed  # → preview shows source_line as fallback
        or (pi.name and pi.name != "unknown")  # → derived name shown
    )
    assert visible_on_card, (
        f"{source!r}: row would silently disappear from preview "
        f"(parse_failed={pi.parse_failed} name={pi.name!r})"
    )
