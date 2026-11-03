"""Single source of cross-unit shopping knowledge.

For a curated set of ingredients, defines the unit you actually BUY in,
how other measures bridge to it, and an optional rollup to a bigger
purchase unit so large counts stay legible (48 cloves -> "~5 heads").

Everything not in the table falls back to natural recipe units (handled
by the aggregator, not here). Factors are sourced in comments; keep the
table small and add entries only as real cases surface.
"""
from __future__ import annotations

from pint import UnitRegistry

_ureg = UnitRegistry()

# canonical -> {
#   "buy_unit": str,
#   "bridges": {from_unit: how_many_buy_units_per_one_from_unit},
#   "rollup": (pkg_unit, buy_units_per_pkg, threshold) | None,
# }
# Bridges only list the count-style conversions Pint can't do; pure
# within-dimension conversions (tbsp->cup) are handled via Pint.
BUY_UNITS: dict[str, dict] = {
    # 1 clove minced ~= 1 tsp; 1 cup = 48 tsp -> 48 cloves; ~10 cloves/head.
    "garlic": {"buy_unit": "clove",
               "bridges": {"clove": 1.0, "cup": 48.0, "tbsp": 3.0, "tsp": 1.0},
               "rollup": ("head", 10.0, 20.0)},
    # 1 medium onion ~= 1 cup chopped ~= 150 g.
    "onion":  {"buy_unit": "ea",
               "bridges": {"ea": 1.0, "cup": 1.0, "g": 1 / 150.0, "kg": 1000 / 150.0},
               "rollup": None},
    # sold by weight; 1 block ~= 400 g.
    "tofu":   {"buy_unit": "g",
               "bridges": {"g": 1.0, "kg": 1000.0, "block": 400.0, "ea": 400.0},
               "rollup": None},
    # juiced by the fruit; 1 lemon ~= 3 tbsp juice, 1 lime ~= 2 tbsp.
    "lemon":  {"buy_unit": "ea",
               "bridges": {"ea": 1.0, "tbsp": 1 / 3.0, "cup": 16 / 3.0},
               "rollup": None},
    "lime":   {"buy_unit": "ea",
               "bridges": {"ea": 1.0, "tbsp": 1 / 2.0, "cup": 16 / 2.0},
               "rollup": None},
}


def has_buy_unit(canonical: str) -> bool:
    return canonical in BUY_UNITS


def to_buy_unit(canonical: str, qty: float, unit: str) -> float | None:
    """Return qty expressed in the ingredient's buy unit, or None if the
    unit can't be bridged (caller falls back to natural units)."""
    entry = BUY_UNITS.get(canonical)
    if entry is None:
        return None
    unit = unit.lower()
    bridges = entry["bridges"]
    if unit in bridges:
        return qty * bridges[unit]
    # Try a pure Pint conversion into any bridged unit (e.g. ml -> tsp).
    # INVARIANT: a given ingredient's bridge factors must be mutually
    # consistent (e.g. cup=48, tbsp=3, tsp=1 all agree that 1 cup=48 tsp).
    # The loop returns the first Pint-convertible bridge, so an inconsistent
    # bridge added later would be silently picked by insertion order.
    for from_unit, factor in bridges.items():
        try:
            converted = (qty * _ureg(unit)).to(from_unit).magnitude
            return converted * factor
        except Exception:
            continue
    return None


def rollup(canonical: str, qty: float, unit: str) -> tuple[float, str]:
    """Promote a large buy-unit count to its purchase package when over
    threshold; otherwise return unchanged."""
    entry = BUY_UNITS.get(canonical)
    if entry is None or entry["rollup"] is None:
        return qty, unit
    pkg_unit, per, threshold = entry["rollup"]
    if unit == entry["buy_unit"] and qty >= threshold:
        return round(qty / per, 1), pkg_unit
    return qty, unit
