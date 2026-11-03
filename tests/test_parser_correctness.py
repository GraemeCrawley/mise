"""Correctness suite — every unique source line has a hand-annotated
expected output in tests/data/parser_expected.py. Compares the
parser's actual output to my judgment of what the line SHOULD parse
to. Failures here = real parser bugs (vs the regression suite which
only catches drift from current behaviour).

Entries with None as the expected value are skipped (genuinely
ambiguous lines).
"""

from __future__ import annotations

import pytest
from sqlmodel import Session

from mise.services.ingredient_parser import parse_line
from tests.data.parser_expected import EXPECTED


_UNIT_ALIASES = {
    "tsp":   {"tsp", "teaspoon", "teaspoons"},
    "tbsp":  {"tbsp", "tablespoon", "tablespoons"},
    "cup":   {"cup", "cups"},
    "lb":    {"lb", "lbs", "pound", "pounds"},
    "kg":    {"kg", "kilogram", "kilograms"},
    "g":     {"g", "gram", "grams"},
    "oz":    {"oz", "ounce", "ounces"},
    "ml":    {"ml", "milliliter", "millilitre", "milliliters", "millilitres"},
    "L":     {"l", "litre", "liter"},
    "ea":    {"ea", "", "piece", "each"},
    "inch":  {"inch", "inches", "in"},
    "clove": {"clove", "cloves"},
    "sprig": {"sprig", "sprigs"},
    "stalk": {"stalk", "stalks"},
    "head":  {"head", "heads"},
    "can":   {"can", "cans"},
    "slice": {"slice", "slices"},
    "scoop": {"scoop", "scoops"},
    "bottle":{"bottle", "bottles"},
    "package":{"package", "packages", "pkg"},
}


# Filter out entries with None (skip-ambiguous)
_CASES = [(src, exp) for src, exp in EXPECTED.items() if exp is not None]


@pytest.mark.parametrize(
    "source_line,expected",
    _CASES,
    ids=[s[:60] for s, _ in _CASES],
)
def test_parses_to_expected(
    source_line: str, expected: tuple, session: Session,
) -> None:
    exp_qty, exp_unit, exp_name_substr, exp_category, exp_optional, exp_parse_failed = expected

    out = parse_line(source_line, session)
    assert out, f"parser returned nothing for {source_line!r}"

    # When the source produces multiple PIs (compound "X and Y"),
    # find the one whose canonical/name matches our expected substring.
    pi = next(
        (p for p in out if exp_name_substr in (p.name or "").lower()),
        out[0],
    )

    # qty/unit (skip if expected_qty is None — meaning no specific qty)
    if exp_qty is not None:
        # Tolerance 0.01 to absorb 2dp rounding (0.125 → 0.12 etc).
        assert abs(pi.qty - exp_qty) <= 0.01 or (
            pi.original_qty is not None and abs(pi.original_qty - exp_qty) <= 0.01
        ), (
            f"{source_line!r}: expected qty={exp_qty}, got {pi.qty} "
            f"(original={pi.original_qty})"
        )
    if exp_unit is not None:
        # Accept any equivalent form of the expected unit.
        aliases = _UNIT_ALIASES.get(exp_unit, (exp_unit,))
        observed = (pi.unit, (pi.original_unit or "").lower())
        assert any(o in aliases for o in observed), (
            f"{source_line!r}: expected unit={exp_unit!r}, got {pi.unit!r} "
            f"(original={pi.original_unit!r})"
        )

    # name substring check (loose match — humanise is non-deterministic)
    name_haystack = (pi.name or "").lower()
    assert exp_name_substr in name_haystack, (
        f"{source_line!r}: expected name to contain {exp_name_substr!r}, "
        f"got name={pi.name!r}"
    )

    # Category check is intentionally omitted — category gets upgraded
    # by the LLM verify pass at import time (Other → Beverages etc.),
    # but this test runs parse_line in isolation (no verify call). The
    # category expectation lives in the EXPECTED fixture for documentation;
    # checking it here would mostly flag "verify hasn't run yet" rather
    # than parser bugs.

    # optional + parse_failed
    assert pi.optional == exp_optional, (
        f"{source_line!r}: expected optional={exp_optional}, "
        f"got {pi.optional}"
    )
    assert pi.parse_failed == exp_parse_failed, (
        f"{source_line!r}: expected parse_failed={exp_parse_failed}, "
        f"got {pi.parse_failed}"
    )
