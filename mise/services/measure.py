"""One measurement type for the whole app: a quantity in a unit, how it
reads ("1½ tbsp"), and what it converts to. The browser side
(static/measure.js) uses the same unit table, sent as JSON, so every
quantity on every page offers the same unit dropdown."""

from __future__ import annotations

import html
import json
from dataclasses import dataclass

# unit -> (dimension, size in the base unit: ml for volume, g for mass).
UNITS: dict[str, tuple[str, float]] = {
    "tsp": ("volume", 4.92892),
    "tbsp": ("volume", 14.7868),
    "fl oz": ("volume", 29.5735),
    "cup": ("volume", 236.588),
    "ml": ("volume", 1.0),
    "l": ("volume", 1000.0),
    "g": ("mass", 1.0),
    "kg": ("mass", 1000.0),
    "oz": ("mass", 28.3495),
    "lb": ("mass", 453.592),
}
# Order offered in the dropdown, per dimension.
CHOICES: dict[str, list[str]] = {
    "volume": ["tsp", "tbsp", "cup", "fl oz", "ml", "l"],
    "mass": ["g", "kg", "oz", "lb"],
}
_ALIASES = {
    "teaspoon": "tsp", "teaspoons": "tsp", "tablespoon": "tbsp", "tablespoons": "tbsp",
    "cups": "cup", "fl_oz": "fl oz", "floz": "fl oz", "fluid ounce": "fl oz",
    "milliliter": "ml", "millilitre": "ml", "mL": "ml", "liter": "l", "litre": "l", "L": "l",
    "gram": "g", "grams": "g", "kilogram": "kg", "ounce": "oz", "ounces": "oz",
    "lbs": "lb", "pound": "lb", "pounds": "lb",
}
_GLYPHS = ((0.125, "⅛"), (0.25, "¼"), (1 / 3, "⅓"), (0.375, "⅜"), (0.5, "½"),
           (0.625, "⅝"), (2 / 3, "⅔"), (0.75, "¾"), (0.875, "⅞"))


def normalize_unit(unit: str | None) -> str:
    u = (unit or "").strip()
    if u == "ea":  # counts are stored as "ea" but read better bare
        return ""
    return _ALIASES.get(u, _ALIASES.get(u.lower(), u.lower() if u.lower() in UNITS else u))


def fmt_number(q: float | int | None) -> str:
    """Fractions for kitchen sizes (¼, ⅓, ⅞…), whole numbers bare,
    otherwise a short decimal."""
    if q is None:
        return ""
    qf = float(q)
    if abs(qf - round(qf)) < 0.01:
        return str(int(round(qf)))
    whole = int(qf)
    frac = qf - whole
    if qf < 20:
        for f, g in _GLYPHS:
            if abs(frac - f) < 0.02:
                return f"{whole or ''}{g}"
    if qf >= 100:
        return str(int(round(qf)))
    if qf >= 10:
        return f"{qf:.1f}".rstrip("0").rstrip(".")
    return f"{qf:.2f}".rstrip("0").rstrip(".")


@dataclass(frozen=True)
class Measure:
    qty: float
    unit: str = ""

    @classmethod
    def of(cls, qty: float | int | None, unit: str | None) -> Measure:
        return cls(float(qty or 0), normalize_unit(unit))

    @property
    def dimension(self) -> str | None:
        d = UNITS.get(self.unit)
        return d[0] if d else None

    @property
    def convertible(self) -> bool:
        return self.dimension is not None and self.qty > 0

    def to(self, unit: str) -> Measure:
        unit = normalize_unit(unit)
        src, dst = UNITS.get(self.unit), UNITS.get(unit)
        if not src or not dst or src[0] != dst[0]:
            raise ValueError(f"can't convert {self.unit!r} to {unit!r}")
        return Measure(self.qty * src[1] / dst[1], unit)

    def alternatives(self) -> list[Measure]:
        dim = self.dimension
        if dim is None or self.qty <= 0:
            return []
        return [self.to(u) for u in CHOICES[dim] if u != self.unit]

    def __add__(self, other: Measure) -> Measure:
        if other.unit == self.unit:
            return Measure(self.qty + other.qty, self.unit)
        return Measure(self.qty + other.to(self.unit).qty, self.unit)

    def text(self) -> str:
        return f"{fmt_number(self.qty)} {self.unit}".strip()

    def __str__(self) -> str:
        return self.text()

    def html(self, cls: str = "", text: str | None = None) -> str:
        """A span the browser can rescale (serving stepper) and convert
        (tap for the unit dropdown). `text` overrides what's shown first."""
        tap = " measure-tap" if self.convertible else ""
        return (f"<span class='measure{tap}{' ' + cls if cls else ''}' data-qty='{self.qty:g}' "
                f"data-unit='{html.escape(self.unit, quote=True)}'>{html.escape(text or self.text())}</span>")


def combine(parts: list[Measure]) -> list[Measure]:
    """Sum parts that share a dimension ("2 tbsp + 1 tbsp" -> "3 tbsp";
    "1 cup + 2 tbsp" -> cups). Different dimensions stay separate."""
    out: list[Measure] = []
    for p in parts:
        for i, o in enumerate(out):
            if o.unit == p.unit or (o.dimension and o.dimension == p.dimension):
                out[i] = o + p
                break
        else:
            out.append(p)
    return out


def units_json() -> str:
    return json.dumps({"units": UNITS, "choices": CHOICES})
