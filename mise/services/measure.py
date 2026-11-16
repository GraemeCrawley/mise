"""One measurement type for the whole app: a quantity in a unit, how it
reads ("1½ tbsp"), and what it converts to. The browser side
(static/measure.js) uses the same unit table, sent as JSON, so every
quantity on every page offers the same unit dropdown."""

from __future__ import annotations

import html
import json
import re
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
# Grams per US cup, for converting between volume and weight. Sources:
# King Arthur ingredient weight chart and USDA FoodData Central. The
# longest name that appears in the ingredient wins ("coconut milk" beats
# "milk", "brown sugar" beats "sugar"). Unknown ingredients use water's
# weight (1 g per ml) as a rough stand-in; the app marks these with "~".
GRAMS_PER_CUP: dict[str, float] = {
    "water": 237, "milk": 242, "buttermilk": 242, "cream": 238, "half and half": 242,
    "sour cream": 230, "yogurt": 245, "greek yogurt": 227, "condensed milk": 306,
    "evaporated milk": 252, "coconut milk": 226, "coconut cream": 240,
    "broth": 240, "stock": 240, "wine": 236, "juice": 244, "vinegar": 239,
    "oil": 216, "butter": 227, "ghee": 205, "honey": 340, "maple syrup": 312,
    "molasses": 337, "corn syrup": 328, "agave": 330, "soy sauce": 255, "tamari": 255,
    "fish sauce": 290, "mirin": 260, "ketchup": 270, "mayonnaise": 220,
    "peanut butter": 258, "tahini": 240, "tomato sauce": 245, "crushed tomatoes": 242,
    "tomato paste": 262, "passata": 245, "vanilla": 208, "egg white": 243,
    "flour": 120, "whole wheat flour": 113, "almond flour": 96, "cornstarch": 128,
    "cornmeal": 138, "sugar": 198, "brown sugar": 213, "powdered sugar": 113,
    "icing sugar": 113, "cocoa": 84, "oats": 89, "rice": 185, "quinoa": 170,
    "couscous": 173, "lentils": 192, "salt": 288, "kosher salt": 240,
    "baking soda": 230, "baking powder": 192, "coconut": 85, "chocolate chips": 170,
    "chocolate": 170, "almonds": 143, "walnuts": 117, "pecans": 109, "peanuts": 146,
    "cashews": 137, "pine nuts": 135, "sesame seeds": 144, "chia seeds": 170,
    "breadcrumbs": 112, "panko": 50, "parmesan": 100, "cheese": 113, "raisins": 145,
    "peas": 134, "corn": 154, "chickpeas": 164, "beans": 177,
    # Longer names that would otherwise match a shorter, wrong entry.
    "coconut oil": 218, "cream cheese": 232, "green beans": 110, "ice cream": 132,
}

WATER = 1.0  # g per ml

_GLYPHS = ((0.125, "⅛"), (0.25, "¼"), (1 / 3, "⅓"), (0.375, "⅜"), (0.5, "½"),
           (0.625, "⅝"), (2 / 3, "⅔"), (0.75, "¾"), (0.875, "⅞"))


def normalize_unit(unit: str | None) -> str:
    u = (unit or "").strip()
    if u == "ea":  # counts are stored as "ea" but read better bare
        return ""
    return _ALIASES.get(u, _ALIASES.get(u.lower(), u.lower() if u.lower() in UNITS else u))


def density_for(name: str | None) -> float | None:
    """Grams per millilitre for an ingredient name, or None if unknown."""
    if not name:
        return None
    n = " " + re.sub(r"[^a-z ]+", " ", name.lower().replace("-", " ")) + " "
    best = None
    for key in GRAMS_PER_CUP:
        if re.search(rf" {re.escape(key)}(e?s)? ", n) and (best is None or len(key) > len(best)):
            best = key
    return GRAMS_PER_CUP[best] / UNITS["cup"][1] if best else None


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
    density: float | None = None  # g per ml; lets volume and weight convert

    @classmethod
    def of(cls, qty: float | int | None, unit: str | None, ingredient: str | None = None) -> Measure:
        u = normalize_unit(unit)
        density = (density_for(ingredient) or WATER) if u in UNITS else None
        return cls(float(qty or 0), u, density)

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
        if not src or not dst:
            raise ValueError(f"can't convert {self.unit!r} to {unit!r}")
        base = self.qty * src[1]  # ml or g
        if src[0] != dst[0]:
            if not self.density:
                raise ValueError(f"can't convert {self.unit!r} to {unit!r} without a density")
            base = base * self.density if src[0] == "volume" else base / self.density
        return Measure(base / dst[1], unit, self.density)

    def alternatives(self) -> list[Measure]:
        dim = self.dimension
        if dim is None or self.qty <= 0:
            return []
        units = CHOICES[dim] + (CHOICES["mass" if dim == "volume" else "volume"] if self.density else [])
        return [self.to(u) for u in units if u != self.unit]

    def __add__(self, other: Measure) -> Measure:
        if other.unit == self.unit:
            return Measure(self.qty + other.qty, self.unit, self.density)
        return Measure(self.qty + other.to(self.unit).qty, self.unit, self.density)

    def text(self) -> str:
        return f"{fmt_number(self.qty)} {self.unit}".strip()

    def __str__(self) -> str:
        return self.text()

    def html(self, cls: str = "", text: str | None = None) -> str:
        """A span the browser can rescale (serving stepper) and convert
        (tap for the unit dropdown). `text` overrides what's shown first."""
        tap = " measure-tap" if self.convertible else ""
        return (f"<span class='measure{tap}{' ' + cls if cls else ''}' data-qty='{self.qty:g}' "
                f"data-unit='{html.escape(self.unit, quote=True)}'"
                + (f" data-density='{self.density:.4g}'" if self.density else "")
                + f">{html.escape(text or self.text())}</span>")


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
