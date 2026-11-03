"""Shopping list aggregator.

Consumes already-parsed Ingredient rows; doesn't depend on the parser
or LLM. Pure function:

    aggregate(recipe_ids, db) -> list[ShoppingItem]

Grouping rule: by canonical_name AND unit-dimension (mass / volume /
count). The same canonical in different dimensions (e.g. "1 cup garlic"
+ "3 cloves garlic") yields TWO entries because you can't sum them
without a density table.

Skips: optional ingredients, parse failures (display_name='unknown').

Output sorted by category (following household.grocery_categories
order) then by display_name.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from typing import Literal

from pint import UnitRegistry
from sqlmodel import Session, select

from mise.models import (
    CookingSession,
    Household,
    Ingredient,
    IngredientSection,
    Recipe,
)
from mise.services.buy_units import BUY_UNITS, has_buy_unit, rollup, to_buy_unit

# Reuse Pint for unit aggregation. New registry so we don't share state
# with the parser's instance (unit definitions same; cleaner isolation).
_ureg = UnitRegistry()

Dimension = Literal["mass", "volume", "count", "package", "length"]

# Units that Pint converts cleanly within a single dimension.
_MASS_UNITS = {"g", "gram", "kg", "kilogram", "oz", "ounce", "lb", "pound"}
_VOLUME_UNITS = {"ml", "L", "litre", "liter", "cup", "tbsp", "tablespoon", "tsp", "teaspoon", "floz"}
# Pint also knows these but we treat them as count-style: no inter-
# conversion possible without a density / per-item knowledge.
_COUNT_UNITS = {"ea", "piece", "slice", "scoop", "pinch", "dash"}
# `pod` (garlic), `rib` (celery) are recipe synonyms for clove / stalk;
# keep them in the same dimension so 1 pod garlic + 4 clove garlic merge.
_PACKAGE_UNITS = {"can", "jar", "bottle", "bunch", "head", "box", "bag", "package", "pkg", "clove", "sprig", "stalk", "stick", "pod", "rib"}
_LENGTH_UNITS = {"inch", "inches", "in"}


def _dimension(unit: str) -> Dimension:
    u = unit.lower().strip()
    if u in _MASS_UNITS:
        return "mass"
    if u in _VOLUME_UNITS:
        return "volume"
    if u in _LENGTH_UNITS:
        return "length"
    if u in _PACKAGE_UNITS:
        return "package"
    return "count"


_GLYPHS = ((0.25, "¼"), (0.5, "½"), (0.75, "¾"), (1 / 3, "⅓"), (2 / 3, "⅔"), (0.125, "⅛"))


def _fmt(q: float) -> str:
    q = round(q, 2)
    if abs(q - round(q)) < 0.05:
        return str(int(round(q)))
    whole, frac = int(q), q - int(q)
    for f, g in _GLYPHS:
        if abs(frac - f) < 0.015:
            return (str(whole) if whole else "") + g
    return str(q)


def _part(q: float, u: str, mixed: bool = False) -> str:
    # Counts are stored as "ea": show "2" alone, "2 whole + 1 cup" mixed.
    if u == "ea":
        return f"{_fmt(q)} whole" if mixed else _fmt(q)
    return f"{_fmt(q)} {u}"


def _display_line(canonical: str, occ: list[tuple[float, str]]) -> str:
    """Compute the display string for one shopping line.

    Rule 1: buy-unit table hit → convert all occurrences, sum, rollup.
    Rule 2: same dimension (mass or volume) → sum in largest recipe unit.
    Rule 3: mixed/incompatible dimensions → show PARTS joined by ' + '.
    """
    # Rule 1: buy-unit table hit → convert all, sum, rollup.
    if has_buy_unit(canonical):
        total = 0.0
        ok = True
        for q, u in occ:
            v = to_buy_unit(canonical, q, u)
            if v is None:
                ok = False
                break
            total += v
        if ok:
            buy_unit = BUY_UNITS[canonical]["buy_unit"]
            q2, u2 = rollup(canonical, total, buy_unit)
            prefix = "~" if u2 != buy_unit else ""
            return prefix + _part(q2, u2)
    # Rules 2 & 3: group by dimension; sum compatible, show parts otherwise.
    dims: dict[str, list[tuple[float, str]]] = {}
    for q, u in occ:
        dims.setdefault(_dimension(u), []).append((q, u))
    parts: list[tuple[float, str]] = []
    for dim, lst in dims.items():
        if dim in ("mass", "volume"):
            target = max(
                (u for _, u in lst),
                key=lambda u: _ureg(u).to_base_units().magnitude,
            )
            tot = 0.0
            for q, u in lst:
                try:
                    tot += (q * _ureg(u)).to(target).magnitude
                except Exception:
                    tot += q
            parts.append((tot, target))
        else:
            byu: dict[str, float] = {}
            for q, u in lst:
                byu[u] = byu.get(u, 0.0) + q
            parts += [(q, u) for u, q in byu.items()]
    return " + ".join(_part(q, u, len(parts) > 1) for q, u in parts)


# Known canonical synonyms that the LLM verify pass produced
# inconsistently across recipes. Mapping to a "preferred" canonical so
# shopping aggregation groups them. The right long-term answer is to
# canonicalize harder in llm_verify, but maintaining a small alias map
# here is cheaper than re-running verify against the whole library.
_CANONICAL_ALIASES: dict[str, str] = {
    "whole_cloves": "cloves",
    "fine_sea_salt": "salt",
    "kosher_salt": "salt",
    "sea_salt": "salt",
    "table_salt": "salt",
    # LLM canonical-drift cases surfaced by a full-library
    # aggregation test. These collapse near-duplicate names into one.
    "green_cardamom_pods": "green_cardamom",
    "fresh_lemon_juice": "lemon_juice",
    "yukon_gold_russet_potatoes": "potatoes",
    "russet_potatoes": "potatoes",
    "yukon_gold_potatoes": "potatoes",
    "cinnamon_bark": "cinnamon_stick",
    # Bare 'cinnamon' canonical with display 'cinnamon stick' is the
    # stick form. Safe to alias even though 'cinnamon' could also mean
    # ground: ground cinnamon arrives in a different dimension
    # (tsp/g/ml) so the (canonical, dim) grouping keeps them separate.
    "cinnamon": "cinnamon_stick",
}


# Plural-ending tokens that should NOT have their trailing 's' stripped.
# "molasses" → "molasse" is wrong; "chickpeas" → "chickpea" is fine.
# Keep this list small and only add real conflicts as they surface.
_KEEP_PLURAL = {
    "molasses",
    "couscous",
    "asparagus",
    "hummus",
    "watercress",
}


def _normalize_canonical(c: str) -> str:
    """Make the aggregation key tolerant of small canonicalization
    drift. Two passes, in order — alias first so the strip operates on
    the canonical noun, not the synonym:
      1. Explicit alias map (whole_cloves → cloves, kosher_salt → salt).
      2. Strip a trailing 's' on the LAST token so 'cinnamon_sticks'
         matches 'cinnamon_stick' AND 'cloves' matches the just-aliased
         'cloves'. Skips known mass nouns to avoid 'molasses → molasse'
         etc."""
    if c in _CANONICAL_ALIASES:
        c = _CANONICAL_ALIASES[c]
    parts = c.split("_")
    last = parts[-1]
    if (
        last not in _KEEP_PLURAL
        and len(last) > 3
        and last.endswith("s")
        and not last.endswith(("ss", "us", "is"))
    ):
        # English -oes plural: potatoes → potato, tomatoes → tomato.
        # Without this, a generic 'strip s' produces 'potatoe' which
        # doesn't match the singular form anyone else writes.
        if last.endswith("oes") and len(last) > 4:
            parts[-1] = last[:-2]
        else:
            parts[-1] = last[:-1]
        return "_".join(parts)
    return c


@dataclass
class SourceRow:
    """One ingredient row that contributed to an aggregated ShoppingItem.
    Preserves the ORIGINAL qty + unit (pre-conversion) so the user can
    verify the math — e.g. 4 lb yellow onion → 12 ea via 150g/onion."""
    qty: float
    unit: str
    recipe_name: str
    recipe_id: int | None = None
    ingredient_id: int | None = None


@dataclass
class ShoppingItem:
    canonical_name: str
    display_name: str
    category: str
    qty: float
    unit: str
    display_qty: str = ""  # rendered qty+unit string; multi-part for rule 3
    source_count: int = 0  # how many recipe rows contributed
    optional: bool = False  # all contributing rows were optional
    sources: list[SourceRow] = field(default_factory=list)


def aggregate_for_sessions(session_ids: list[int], db: Session) -> list[ShoppingItem]:
    """Variant that aggregates over planned cooking sessions, scaling
    each recipe by the sum of its sessions' batch_multipliers.
    Closes household Tier 1 #2 (per-slot serving overrides flow into the
    shopping list — no silent double-shop). Multiple sessions of the
    same recipe at different scales are additively combined."""
    if not session_ids:
        return []
    sessions = db.exec(
        select(CookingSession).where(CookingSession.id.in_(session_ids))
    ).all()
    multipliers: dict[int, float] = defaultdict(float)
    for s in sessions:
        multipliers[s.recipe_id] += (s.batch_multiplier or 1.0)
    return aggregate(list(multipliers.keys()), db, multipliers=dict(multipliers))


def aggregate(
    recipe_ids: list[int],
    db: Session,
    multipliers: dict[int, float] | None = None,
) -> list[ShoppingItem]:
    if not recipe_ids:
        return []

    # Fetch every ingredient row across the selected recipes, joined
    # with recipe name so sub-line attribution works without a second
    # query per row.
    joined = db.exec(
        select(Ingredient, Recipe)
        .join(IngredientSection, Ingredient.section_id == IngredientSection.id)
        .join(Recipe, IngredientSection.recipe_id == Recipe.id)
        .where(IngredientSection.recipe_id.in_(recipe_ids))
    ).all()

    # Group by (canonical_name, dimension). Same canonical in two
    # different dimensions (1 cup garlic vs 3 cloves garlic) yields two
    # entries unless buy_units bridges them in _display_line.
    # canonical_name is run through _normalize_canonical first so
    # whole_cloves/cloves and cinnamon_stick/cinnamon_sticks merge.
    #
    # Each entry is (qty, unit, display_name, source_row). The SourceRow
    # preserves the ORIGINAL qty + unit for the math sub-line.
    @dataclass
    class _Entry:
        qty: float
        unit: str  # effective; used for aggregation math
        display_name: str
        source: SourceRow

    # Per-recipe multiplier — defaults to 1.0; aggregate_for_sessions
    # passes the sum of batch_multipliers so per-slot scaling flows
    # through. Each ingredient row is scaled before grouping so the
    # cross-dimension bridges still operate on accurate magnitudes.
    mult_map = multipliers or {}

    # Group by canonical ONLY (not canonical+dimension): _display_line owns
    # all dimension logic — rule 1 combines a buy-unit ingredient across
    # dimensions (garlic cloves + cups -> one line), rules 2/3 sum compatible
    # units or show parts. Keying by dimension here would split those apart.
    groups: dict[str, list[_Entry]] = defaultdict(list)
    for ing, rec in joined:
        if ing.display_name == "unknown" or ing.canonical_name == "unknown":
            continue
        if ing.optional:
            continue
        if ing.qty == 0:
            continue
        mult = mult_map.get(rec.id, 1.0) if rec.id is not None else 1.0
        scaled_qty = ing.qty * mult
        key = _normalize_canonical(ing.canonical_name)
        groups[key].append(_Entry(
            qty=scaled_qty,
            unit=ing.unit,
            display_name=ing.display_name,
            source=SourceRow(
                qty=scaled_qty, unit=ing.unit,
                recipe_name=rec.name_english or rec.name_original,
                recipe_id=rec.id,
                ingredient_id=ing.id,
            ),
        ))

    # Per-group: category lookup needs the original Ingredient.
    # We attach it via a side-table on Ingredient.canonical_name
    # since it's the same across the group's rows.
    canonical_to_category: dict[str, str] = {}
    for ing, _ in joined:
        canonical_to_category.setdefault(
            _normalize_canonical(ing.canonical_name), ing.category,
        )

    items: list[ShoppingItem] = []
    for canonical, entries in groups.items():
        occ = [(e.qty, e.unit) for e in entries]
        display_qty = _display_line(canonical, occ)
        qty = sum(e.qty for e in entries)
        unit = entries[0].unit

        name_counts: dict[str, int] = defaultdict(int)
        for e in entries:
            name_counts[e.display_name] += 1
        display_name = sorted(name_counts.items(), key=lambda x: (-x[1], x[0]))[0][0]

        items.append(ShoppingItem(
            canonical_name=canonical,
            display_name=display_name,
            category=canonical_to_category.get(canonical, "Other"),
            qty=qty,
            unit=unit,
            display_qty=display_qty,
            source_count=len(entries),
            sources=[e.source for e in entries],
        ))

    # De-duplicate any same (canonical, unit) entries (shouldn't normally
    # occur with dimension-keyed grouping, but acts as a safety net).
    seen: dict[tuple[str, str], ShoppingItem] = {}
    for it in items:
        key = (it.canonical_name, it.unit)
        if key in seen:
            prev = seen[key]
            seen[key] = ShoppingItem(
                canonical_name=prev.canonical_name,
                display_name=prev.display_name,
                category=prev.category,
                qty=round(prev.qty + it.qty, 2),
                unit=prev.unit,
                display_qty=prev.display_qty,  # keep first
                source_count=prev.source_count + it.source_count,
                sources=prev.sources + it.sources,
            )
        else:
            seen[key] = it
    items = list(seen.values())

    # Sort: by category (household order), then primary noun (last word
    # of display_name) so "yellow onion" + "red onion" cluster together,
    # then full display_name as tiebreaker.
    household = db.exec(select(Household)).first()
    cat_order = household.grocery_categories if household else []
    cat_index = {c: i for i, c in enumerate(cat_order)}

    def _sort_key(x: ShoppingItem) -> tuple:
        words = x.display_name.lower().split()
        primary = words[-1] if words else x.display_name.lower()
        return (cat_index.get(x.category, 999), primary, x.display_name.lower())

    items.sort(key=_sort_key)
    return items
