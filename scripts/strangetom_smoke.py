#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12,<3.13"
# dependencies = [
#   "ingredient-parser-nlp>=2.7,<3",
#   "pint>=0.24,<1",
# ]
# ///

"""Real-strangetom smoke against the same 8 lines as scripts/smoke_parser.py.

The goal is parity check: confirm strangetom (deterministic, ~10ms/line,
free) handles our actual data well enough to make it the primary extractor.
LLM verification would only run on first-seen ingredients afterwards.

Compares against the LLM-smoke output we already have:

  | line                                 | LLM output (canonical)         |
  |--------------------------------------|--------------------------------|
  | 2 lbs chuck beef, cut into …         | qty=907g name='chuck beef'     |
  | 6 garlic cloves, chopped             | qty=6 ea name='garlic cloves'  |
  | 1 tablespoon paprika                 | qty=15ml name='paprika'        |
  | Salt and pepper to taste             | qty=0 ea name='salt' OPTIONAL  |
  | 1/2 pound potatoes                   | qty=227g name='potatoes'       |
  | 2 Tablespoon olive oil               | qty=30ml name='olive oil'      |
  | 180g bacon                           | qty=180g name='bacon'          |
  | 2.5 lb beef chuck, … 2″ chunks       | qty=1134g name='beef chuck'    |

We're looking for: matches on qty + unit (after Pint canonicalisation),
matches on name, useful values in foundation_foods.
"""

from __future__ import annotations

import sys
import time

from ingredient_parser import parse_ingredient
from pint import UnitRegistry

ureg = UnitRegistry()

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


def to_canonical(qty_str: str, unit_str: str) -> tuple[float | None, str | None]:
    """Try to convert (qty, unit) to canonical g/ml/ea via Pint.
    Returns (None, None) on parse failure — caller decides what to do."""
    try:
        q = float(qty_str)
    except (ValueError, TypeError):
        return None, None
    try:
        u = ureg.parse_expression(unit_str)
        # u may be a unit (not a quantity); turn into a quantity of 1
        if hasattr(u, "units"):
            unit = u.units
        else:
            unit = u
        quantity = q * unit
        # Try mass → g, then volume → ml
        try:
            converted = quantity.to("gram")
            return converted.magnitude, "g"
        except Exception:
            pass
        try:
            converted = quantity.to("millilitre")
            return converted.magnitude, "ml"
        except Exception:
            pass
        # Count or package — return as-is
        return q, unit_str
    except Exception:
        return q, unit_str


def main() -> int:
    print(f"library: ingredient-parser-nlp; canonical-unit conversion via pint\n")
    total = 0.0
    for raw in CASES:
        t = time.monotonic()
        try:
            parsed = parse_ingredient(raw, foundation_foods=True)
        except Exception as e:
            print(f"  ✗ raw={raw!r}\n     ERROR: {e}\n")
            continue
        dur_ms = (time.monotonic() - t) * 1000
        total += dur_ms

        # Pull the structured fields, defensive about None
        name_text = getattr(parsed.name, "text", None) if parsed.name else None
        name_conf = getattr(parsed.name, "confidence", None) if parsed.name else None
        prep_text = getattr(parsed.preparation, "text", None) if parsed.preparation else None
        size_text = getattr(parsed.size, "text", None) if parsed.size else None
        comment_text = getattr(parsed.comment, "text", None) if parsed.comment else None
        foundation = (
            [(getattr(f, "text", None), getattr(f, "confidence", None))
             for f in parsed.foundation_foods]
            if getattr(parsed, "foundation_foods", None) else []
        )

        # amount is a list of IngredientAmount objects
        amounts = []
        for a in (parsed.amount or []):
            q = getattr(a, "quantity", None)
            u = getattr(a, "unit", None)
            text = getattr(a, "text", None)
            amounts.append((q, u, text, getattr(a, "confidence", None)))

        print(f"  raw:  {raw!r}  ({dur_ms:.1f}ms)")
        print(f"     name:        {name_text!r}  (conf={name_conf})")
        for q, u, t_, c in amounts:
            qty_c, unit_c = to_canonical(q, str(u)) if (q is not None and u is not None) else (None, None)
            print(f"     amount:      qty={q!r} unit={str(u)!r} text={t_!r} conf={c}")
            print(f"                  → canonical: qty={qty_c} unit={unit_c}")
        if prep_text:    print(f"     prep:        {prep_text!r}")
        if size_text:    print(f"     size:        {size_text!r}")
        if comment_text: print(f"     comment:     {comment_text!r}")
        if foundation:   print(f"     foundation:  {foundation}")
        print()

    avg = total / max(len(CASES), 1)
    print(f"total: {total:.1f}ms for {len(CASES)} lines  →  ~{avg:.1f}ms/line")
    return 0


if __name__ == "__main__":
    sys.exit(main())
