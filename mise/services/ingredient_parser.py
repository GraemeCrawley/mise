"""Ingredient parser — strangetom + Pint (deterministic) with cache.

The 2026-05-29 refactor: replaced the LLM-per-line pipeline with
ingredient-parser-nlp (CRF-backed extraction) + Pint canonicalisation.
LLM is no longer on the per-line path — it's only invoked as a
first-seen verify check for category / canonical-name disagreements
(separate followup, see TODO at bottom of file).

Why this is structurally better than the old LLM-per-line approach:
  - Deterministic math (Pint can't relabel "2 lbs" as "2 g")
  - 100×-1000× faster (~10ms/line vs ~1.1s/line)
  - Free per-line (LLM only on first-seen unknowns)
  - No prompt-engineering treadmill
  - Compound lines work natively — "salt and pepper to taste" returns
    BOTH ingredients from strangetom's foundation_foods array

Output contract:
  - parse_line(raw) returns LIST[ParsedIngredient]. Compound lines
    yield multiple items; single-ingredient lines yield one.
  - All numeric values are rounded:
      qty (canonical g/ml/ea): integer
      original_qty: 2 decimal places (preserves "1.5 cups" → 1.5)

Failure-mode contract (per docs/ingredient-parsing.md):
  - Per-line parse failures raise ParserError; importer flags via
    needs_review on SyncRun.
  - "to taste" / "as needed" → optional=True, qty=0, unit="ea".
  - Unknown unit → preserved verbatim; surfaced for review.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from fractions import Fraction

from ingredient_parser import parse_ingredient as _strangetom_parse
from pint import UnitRegistry
from pydantic import BaseModel, Field, ValidationError
from sqlmodel import Session

from mise.models import ParserCache
from mise.services.canonical_resolver import resolve as resolve_canonical

log = logging.getLogger("mise.parser")

# Bump on output-schema / extraction-logic change. Cached rows with
# older versions stay (audit) but writes go to new keys until manually
# cleaned. v2 = strangetom-backed.
PARSER_VERSION = "v27"

_ureg = UnitRegistry()

# Container-size fallback: "N (oz|ounce|ounces|g|grams|ml|l) (can|jar|...)"
# patterns strangetom misses. Captures the number + unit word so we can
# stash "14.5 oz" as the row's size_descriptor when the canned-tomato
# style line ("1 14.5-oz. can diced tomatoes") leaves it on the floor.
# (?<!\d) prevents capturing the trailing digit of a multi-digit number.
_CONTAINER_SIZE_RE = re.compile(
    r"""
    (?<!\d)
    \(?                                # optional opening paren
    (\d+(?:\.\d+)?)                    # \1: the size number
    \s*[-\s]?\s*                       # whitespace / hyphen
    (oz|ounce|ounces|g|gram|grams|ml|milliliter|milliliters|millilitre|millilitres|l|liter|liters|litre|litres)
    \.?                                # optional dot abbreviation
    \s*\)?                             # optional closing paren
    \s+
    (?:can|cans|jar|jars|bottle|bottles|box|boxes|bag|bags|pkg|pkgs|package|packages)
    \b
    """,
    re.IGNORECASE | re.VERBOSE,
)

# Category mapping by FDC foundation-food prefix. Most of the legacy
# library's food space is covered by these prefixes; the long tail
# falls into "Other" and gets flagged for review (LLM verify pass in
# the followup commit).
CATEGORY_BY_PREFIX: dict[str, str] = {
    "Beef": "Meat",
    "Pork": "Meat",
    "Chicken": "Meat",
    "Turkey": "Meat",
    "Lamb": "Meat",
    "Fish": "Meat",
    "Shrimp": "Meat",
    "Crab": "Meat",
    "Bacon": "Meat",
    "Sausage": "Meat",

    "Milk": "Dairy",
    "Cheese": "Dairy",
    "Yogurt": "Dairy",
    "Butter": "Dairy",
    "Cream": "Dairy",
    "Egg": "Dairy",

    "Bread": "Bakery",
    "Flour": "Bakery",

    "Rice": "Pasta/Rice",
    "Pasta": "Pasta/Rice",
    "Noodle": "Pasta/Rice",
    "Couscous": "Pasta/Rice",
    "Quinoa": "Pasta/Rice",

    "Salt": "Spices",
    "Sel": "Spices",  # French
    "Pepper": "Spices",  # 'Pepper, black'; produce peppers also start with 'Peppers' (plural)
    "Spices": "Spices",
    "Paprika": "Spices",
    "Cumin": "Spices",
    "Cinnamon": "Spices",
    "Vanilla": "Spices",
    "Rose": "Spices",  # rose petals, rose water — tea/dessert flavouring

    "Oil": "Pantry",
    "Olive oil": "Pantry",
    "Vinegar": "Pantry",
    "Sauce": "Sauces",
    "Soy sauce": "Sauces",
    "Ketchup": "Sauces",
    "Mustard": "Sauces",
    "Mayonnaise": "Sauces",

    "Onion": "Produce",
    "Garlic": "Produce",
    "Tomato": "Produce",
    "Potato": "Produce",
    "Carrot": "Produce",
    "Celery": "Produce",
    "Lettuce": "Produce",
    "Spinach": "Produce",
    "Kale": "Produce",
    "Mushroom": "Produce",
    "Pepper, green": "Produce",
    "Pepper, red": "Produce",
    "Peppers": "Produce",
    "Lemon": "Produce",
    "Lime": "Produce",
    "Apple": "Produce",
    "Banana": "Produce",
    "Broccoli": "Produce",
    "Cabbage": "Produce",
    "Cauliflower": "Produce",
    "Cucumber": "Produce",
    "Zucchini": "Produce",
    "Eggplant": "Produce",
    "Avocado": "Produce",
    "Ginger": "Produce",
    "Cilantro": "Produce",
    "Parsley": "Produce",
    "Basil": "Produce",
    "Mint": "Produce",
    "Plantain": "Produce",

    "Water": "Beverages",
    "Beverages": "Beverages",

    "Honey": "Pantry",
    "Tofu": "Pantry",
    "Seaweed": "Pantry",
    "Nutritional": "Pantry",  # protein powders, dietary supplements
    "Syrups": "Pantry",
    "Peanut butter": "Pantry",
    "Almond": "Pantry",
    "Baobab": "Pantry",
    "Icing": "Bakery",
    "Hemp": "Pantry",  # hemp hearts, hemp seeds — Pantry/health food aisle

    "Frog": "Meat",
    "Meatballs": "Meat",
}


# --- output schema --------------------------------------------------------


class ParsedIngredient(BaseModel):
    """Structured output of a single parsed ingredient. Field semantics
    match docs/ingredient-parsing.md §"The parser"."""

    qty: float = Field(description="Canonical numeric quantity. 0 for 'to taste'. Integer.")
    unit: str = Field(description="Canonical unit: ml | g | ea | can | jar | ...")
    name: str = Field(description="Display name without prep notes (from foundation_food when available).")
    canonical_name: str = Field(description="Aggregation key (snake_case). Set by canonical_resolver.")
    category: str = Field(description="Grocery section: Produce | Dairy | Meat | Pantry | Spices | …")

    prep_note: str | None = None
    size_descriptor: str | None = None  # 'large' / 'medium' / 'small' only
    package_qty: float | None = None
    package_unit: str | None = None
    optional: bool = False
    original_qty: float | None = Field(default=None, description="Pre-conversion qty, rounded to 2dp.")
    original_unit: str | None = None
    foundation_food: str | None = Field(default=None, description="FDC reference (audit; not user-visible).")
    confidence: float | None = Field(default=None, description="strangetom's min confidence across fields.")
    parse_failed: bool = Field(default=False, description="True when the line had no quantity and no 'to taste' marker — almost certainly a USDA reference name pasted as data, not a real ingredient line.")
    display_name_pretty: str | None = Field(default=None, description="Humanised display name from the LLM verify cache, if available. Importer prefers this over `name` (FDC-flavoured).")
    source_line: str | None = Field(default=None, description="Verbatim ingredient line from the source xlsx, before any parsing. Threaded through for display so the user can compare parsed output to the original input.")
    alternatives_note: str | None = Field(default=None, description="LLM-extracted alternative from the source line (e.g. 'or beef stock', 'or 1 tsp dried thyme'). Per-row; not cached on IngredientAlias because alternatives depend on the source line, not the canonical.")


@dataclass
class ParsedSection:
    """A section groups ingredient lines under an optional name."""
    name: str | None
    lines: list[ParsedIngredient] = field(default_factory=list)
    failed: list[tuple[str, str]] = field(default_factory=list)  # (raw, error_msg)


# --- normalisation + cache key -------------------------------------------


# Unicode fractions strangetom doesn't reliably parse — convert to
# decimals before handing the line off.
# USDA reference-name tells. Lines that match these aren't real recipe
# lines — they're FDC foundation_food names that got pasted into the
# ingredient column as data.
_USDA_TELLS = (", raw", ", NFS", ", Grade A", ", cooked", ", boneless",
               "Spices,", "Beverages,", "Sugars,", "Eggs, Grade")


def _looks_like_usda_name(name: str) -> bool:
    """Heuristic: name looks like a verbatim FDC reference rather than
    a recipe ingredient. Used to gate implicit-to-taste so we don't
    promote 'Turkey, dark meat, meat and skin, raw' to an optional row."""
    if not name:
        return False
    return any(tell in name for tell in _USDA_TELLS) or name.count(",") >= 3


_UNICODE_FRACTIONS = {
    "½": "0.5", "⅓": "0.333", "⅔": "0.667",
    "¼": "0.25", "¾": "0.75",
    "⅛": "0.125", "⅜": "0.375", "⅝": "0.625", "⅞": "0.875",
}


def _despace_word_slashes(raw: str) -> str:
    """Replace 'word/word' patterns (letters either side of the slash)
    with 'word word' so strangetom doesn't try to parse them as
    fractions. '1/2 tsp cooking/kosher salt' → '1/2 tsp cooking kosher
    salt' — keeps the numeric '1/2' intact (digit/digit) and breaks
    the 'cooking/kosher' word collision."""
    return re.sub(r"(?<=[A-Za-z])/(?=[A-Za-z])", " ", raw)


def _expand_unicode_fractions(raw: str) -> str:
    """Convert '½ red onion' → '0.5 red onion' so strangetom can parse
    the quantity. Mixed forms like '1½' → '1.5' (treating concatenation
    as addition)."""
    out = raw
    # Mixed: digit+glyph → digit + decimal (e.g. "1½" → "1.5")
    for glyph, dec in _UNICODE_FRACTIONS.items():
        out = re.sub(rf"(\d)\s*{glyph}", lambda m, d=dec: str(float(m.group(1)) + float(d)), out)
    # Standalone glyph at any position
    for glyph, dec in _UNICODE_FRACTIONS.items():
        out = out.replace(glyph, dec)
    return out


def normalize(raw: str) -> str:
    """Canonical form for cache keying. Whitespace collapsed; case preserved."""
    return re.sub(r"\s+", " ", _despace_word_slashes(_expand_unicode_fractions(raw))).strip()


def cache_key(raw: str) -> str:
    return hashlib.sha256(normalize(raw).encode("utf-8")).hexdigest()


# --- section detection ---------------------------------------------------

_QTY_PREFIX = re.compile(r"^\s*[\d.½⅓⅔¼¾⅛⅜⅝⅞]")


_HEADER_LEAD_KEYWORDS = (
    "toppings", "garnishes", "garnish", "for the ", "optional garnishes",
    "to saute", "to puree", "to fry", "to bake",
)
_HEADER_SUFFIX_KEYWORDS = ("sauce", "gravy", "dressing", "marinade", "topping", "garnish")


def is_section_header(line: str) -> bool:
    line = line.strip()
    if not line:
        return False
    if _QTY_PREFIX.match(line):
        return False

    lc = line.lower()

    # Form 1: trailing colon ("Marinade:", "For the sauce:") OR mid-line
    # colon when the line LEADS with a header keyword ("toppings: chopped
    # chives or green onions, freshly-cracked black pepper"). The
    # post-colon content is descriptive, not an ingredient.
    if line.endswith(":"):
        head = line.rstrip(":").strip()
        return bool(head) and len(head.split()) <= 5

    # Form 2: short ALL-CAPS label ("MEAT AND MEAT SAUCE", "DRINK").
    if (
        line == line.upper()
        and any(c.isalpha() for c in line)
        and 2 <= len(line) <= 60
        and len(line.split()) <= 5
    ):
        return True

    # Form 3: leads with a known section-header keyword.
    if any(lc.startswith(kw) for kw in _HEADER_LEAD_KEYWORDS):
        return True

    # Form 4: short Title Case label ending in a known section suffix
    # ("Yogurt Sauce", "Mushroom Gravy"). Limit to ≤4 words to avoid
    # catching "I served it with garlic sauce".
    words = line.split()
    if (
        2 <= len(words) <= 4
        and any(lc.endswith(suf) for suf in _HEADER_SUFFIX_KEYWORDS)
        and line[0].isupper()
    ):
        return True

    # Form 5: single-word Title-case label that's clearly a header
    # ("Garnish", "Gravy"). One-word ingredients exist (Thyme, Eggs),
    # so we restrict to recognised header words only.
    if (
        len(words) == 1
        and lc in {"garnish", "garnishes", "gravy", "topping", "toppings",
                   "marinade", "dressing", "drink", "drinks"}
    ):
        return True

    return False


def section_name(line: str) -> str:
    return line.strip().rstrip(":").strip()


def split_blob_into_sections(blob: str) -> list[tuple[str | None, list[str]]]:
    """Pre-strangetom split. Returns [(section_name, [raw_lines])]."""
    sections: list[tuple[str | None, list[str]]] = [(None, [])]
    for raw in blob.splitlines():
        line = raw.strip()
        if not line:
            continue
        if is_section_header(line):
            sections.append((section_name(line), []))
        else:
            sections[-1][1].append(line)
    if sections[0] == (None, []):
        sections.pop(0)
    return sections


# --- unit canonicalisation -----------------------------------------------


def _round_qty(q: float) -> float:
    """Canonical g/ml/ea are integer-scaled. Sub-gram precision is
    noise; sub-ml precision is noise."""
    return float(round(q))


def _round_original(q: float) -> float:
    """Original (pre-conversion) qty preserves cooking precision —
    'half a cup' is 0.5, not 0 or 1."""
    return round(float(q), 2)


def _normalise_unit_for_pint(u: str) -> str:
    """Lowercase + strip trailing 's' so Pint matches 'Tablespoons' (which
    strangetom occasionally returns with capital + plural). 'tablespoon'
    is in Pint's default registry; 'Tablespoons' isn't."""
    u = u.strip().lower()
    # Naive depluralisation — fine for cooking units (none are
    # legitimately plural-ending-in-s like 'sauce' would be).
    if u.endswith("s") and len(u) > 1:
        u = u[:-1]
    return u


def to_canonical(qty: float, unit_text: str) -> tuple[float, str, float | None, str | None]:
    """Normalize the unit spelling but KEEP the natural dimension.

    We no longer collapse volume->ml / mass->g; the recipe's own unit is
    the stored truth (cross-unit work happens later in aggregation via the
    buy_units table). original_* stay None — there is no conversion to undo.
    Returns (qty, unit, None, None).
    """
    if unit_text is None:
        return _round_original(qty), "ea", None, None
    u = _normalise_unit_for_pint(unit_text)
    if u in {"", "each", "whole", "piece"}:
        return _round_original(qty), "ea", None, None
    return _round_original(qty), _abbrev_unit(u) or u, None, None


# --- category-preferred display units (RETIRED from the write path) -------
#
# HISTORICAL: `to_canonical()` used to be dimension-driven (mass → g,
# volume → ml) and this table restored a shopper-friendly unit afterward.
# As of the natural-unit redesign (PARSER_VERSION v27) `to_canonical()`
# keeps the recipe's own unit, so nothing in the parse/verify WRITE path
# calls `apply_category_preference` anymore — units no longer depend on
# category. Kept only for parse-trace diagnostics, its own unit tests, and
# the legacy backfill script. Do not reintroduce into the write path.
#
# Per category, list the units a shopper actually uses. First entry is
# the fallback default when no original unit is available to restore.

PREFERRED_UNITS_BY_CATEGORY: dict[str, list[str]] = {
    "Meat":       ["lb", "kg", "ea", "oz", "cup"],         # cup for chopped/shredded
    "Produce":    ["ea", "head", "bunch", "clove", "lb", "kg", "oz", "cup", "tbsp", "tsp"],
    "Dairy":      ["ea", "cup", "oz", "lb", "tbsp", "tsp"], # oz for cheese
    "Bakery":     ["ea", "cup", "tbsp", "oz"],
    "Deli":       ["lb", "oz", "ea"],
    "Frozen":     ["cup", "ea", "lb", "kg", "oz"],         # cup for frozen berries/peas
    "Pantry":     ["cup", "tbsp", "tsp", "oz"],
    "Sauces":     ["tbsp", "tsp", "cup", "oz"],
    "Pasta/Rice": ["cup", "lb", "ea", "oz"],
    "Snacks":     ["ea", "cup", "tbsp", "lb", "oz"],
    "Beverages":  ["cup", "ml", "L", "ea", "tbsp", "tsp", "shot"],  # shot for cocktails, tbsp/tsp for cooking splashes
    "Spices":     ["tsp", "tbsp", "pinch"],
    "Other":      [],
}

# strangetom returns mixed forms ("teaspoon", "Tablespoons", "tbsp").
# Map the long forms to the abbreviations used in PREFERRED_UNITS_BY_CATEGORY
# so membership checks work.
_DISPLAY_UNIT_ABBR: dict[str, str] = {
    "teaspoon": "tsp",
    "tablespoon": "tbsp",
    "fluid_ounce": "floz",
    "fluidounce": "floz",
    "ounce": "oz",
    "pound": "lb",
    "kilogram": "kg",
    "gram": "g",
    "millilitre": "ml",
    "milliliter": "ml",
    "litre": "L",
    "liter": "L",
}


def _abbrev_unit(u: str | None) -> str | None:
    if not u:
        return u
    low = _normalise_unit_for_pint(u)
    return _DISPLAY_UNIT_ABBR.get(low, low)


def apply_category_preference(
    qty: float,
    unit: str,
    original_qty: float | None,
    original_unit: str | None,
    category: str,
) -> tuple[float, str, float | None, str | None]:
    """Restore the original shopping unit (rule 1) or convert canonical
    to the category default (rule 2). Cross-dimension conversions are
    skipped — no density table here.

    Returns (qty, unit, original_qty, original_unit). When a swap
    happens the original_* fields are cleared (they served their purpose
    by letting us restore).
    """
    pref = PREFERRED_UNITS_BY_CATEGORY.get(category, [])
    if not pref:
        # No category-specific preference (typically 'Other'). The
        # parser already converted to a canonical Pint unit upstream
        # — without a category to guide the restore, that leaks
        # nonsense like '118 ml mini bocconcini' for a user who typed
        # '1/2 cup'. Fall back to the original unit if we have one;
        # the user's input is always more meaningful than the
        # canonical form for an unknown ingredient.
        orig_abbr = _abbrev_unit(original_unit)
        if orig_abbr and original_qty is not None:
            return _round_original(original_qty), orig_abbr, None, None
        return qty, unit, original_qty, original_unit

    # Rule 1: original is preferred — restore it (abbreviated for display).
    orig_abbr = _abbrev_unit(original_unit)
    if orig_abbr and orig_abbr in pref and original_qty is not None:
        return _round_original(original_qty), orig_abbr, None, None

    # Rule 1b: the CURRENT unit is already preferred. Don't touch.
    # This guards re-runs after a category upgrade (e.g. Other →
    # Spices) where Rule 1 already restored the unit during the
    # initial parse and cleared originals; without this check, Rule 2
    # would re-convert (1 tbsp → 3 tsp).
    cur_abbr = _abbrev_unit(unit)
    if cur_abbr and cur_abbr in pref:
        return qty, unit, original_qty, original_unit

    # Rule 2: convert canonical to the category default if dimensions
    # match AND the resulting value rounds to a recipe-friendly number.
    # "100 g beef → 0.22 lb" is technically correct but reads as garbage;
    # in that case fall through to keep the canonical g.
    default = pref[0]
    try:
        converted = (qty * _ureg(unit)).to(default)
    except Exception:
        return qty, unit, original_qty, original_unit
    new_qty = converted.magnitude
    if not _is_recipe_clean(new_qty):
        return qty, unit, original_qty, original_unit
    return _round_original(new_qty), default, None, None


# Recipe-friendly numbers: integers, half-step, common fractions
# (¼ / ⅓ / ½ / ⅔ / ¾ / ⅛ / ⅜ / ⅝ / ⅞). Used to gate rule 2 so we
# don't show "0.22 lb beef".
_CLEAN_FRACTIONS = (0.0, 0.125, 0.25, 1/3, 0.375, 0.5, 0.625, 2/3, 0.75, 0.875)


def _is_recipe_clean(q: float, tol: float = 0.02) -> bool:
    """True if `q` is within `tol` of an integer + clean fraction.
    `tol=0.03` lets us catch values that round visually (0.748 → ¾)
    while rejecting near-miss garbage (0.22 → not ¼)."""
    integer = int(q)
    frac = q - integer
    return any(abs(frac - f) <= tol for f in _CLEAN_FRACTIONS)


# --- category resolution -------------------------------------------------


def resolve_category(foundation_food: str | None, name: str | None) -> str:
    """Static prefix mapping → category. Unknown → 'Other'.

    Followup commit will add LLM verify for first-seen unknowns +
    FoundationFoodAlias caching of the answer. For now, 'Other' is the
    flag for manual review.
    """
    for haystack in (foundation_food, name):
        if not haystack:
            continue
        # Match the longest prefix wins
        match = ""
        for prefix in CATEGORY_BY_PREFIX:
            if haystack.startswith(prefix) and len(prefix) > len(match):
                match = prefix
        if match:
            return CATEGORY_BY_PREFIX[match]
    return "Other"


# --- core parse ----------------------------------------------------------


class ParserError(RuntimeError):
    """strangetom raised on this input AND we have no fallback strategy."""


_LEADING_QTY_RE = re.compile(
    r"^\s*(?:"
    r"(\d+)\s+(\d+)\s*/\s*(\d+)"                    # "1 1/2"  -> mixed fraction
    r"|(\d+)\s*/\s*(\d+)"                            # "1/2"
    r"|(\d+(?:\.\d+)?)\s*(?:-|\s+(?:to|or)\s+)\s*\d+"  # "1-2", "1 to 2", "3 or 4"
    r"|(\d+(?:\.\d+)?)"                              # "0.5", "2", "10"
    r")\s+"
)


def _extract_leading_qty(source: str) -> tuple[float | None, str]:
    """Parse a leading number/fraction from `source`. Returns (qty, rest).
    Used as a deterministic fallback when strangetom's CRF misses the
    quantity (e.g. '0.5 red onion'). rest is the input with the qty
    token removed (leading whitespace stripped too)."""
    m = _LEADING_QTY_RE.match(source)
    if not m:
        return None, source
    if m.group(1) and m.group(2) and m.group(3):                  # mixed "1 1/2"
        qty = float(m.group(1)) + float(m.group(2)) / float(m.group(3))
    elif m.group(4) and m.group(5):                               # "1/2"
        qty = float(m.group(4)) / float(m.group(5))
    elif m.group(6):                                              # "1-2", "1 to 2", "3 or 4"
        qty = float(m.group(6))
    elif m.group(7):                                              # "0.5"
        qty = float(m.group(7))
    else:
        return None, source
    rest = source[m.end():].lstrip()
    return qty, rest


# All common cooking units in any form we might see at the start of a
# source_line. Matched case-insensitively after the leading qty is gone.
# Tolerant of plurals (tablespoons, cups, ounces) — the `s?` suffix
# means we match either form. Order matters: longer alternatives first
# so 'tablespoon' is tried before 'tbsp'.
_LEADING_UNIT_RE = re.compile(
    r"^(?:"
    r"tablespoons?|teaspoons?|tbsp|tsp|cups?|c|"
    r"ounces?|oz|pounds?|lbs?|"
    r"grams?|g|kilograms?|kg|"
    r"millilitres?|milliliters?|ml|litres?|liters?|l|"
    r"inches?|in|pinch|dash|cloves?|sticks?|sprigs?|"
    r"slices?|stalks?|heads?|bunch(?:es)?|cans?|"
    r"jars?|bottles?|boxes?|bags?|packages?|pkg"
    r")\.?\s+",
    re.IGNORECASE,
)


def _derive_name_from_source(source_line: str, qty_text: str | None, unit_text: str | None) -> str | None:
    """When strangetom extracts qty+unit but no name, strip the qty/unit
    prefix from the literal source and return what's left. Returns None
    if nothing recognisable remains.

      '2 Tbsp gochujang' → 'gochujang'
      '1 cup corn kernels, drained' → 'corn kernels'
      '1 tsp. kosher salt, plus more' → 'kosher salt'
      '1 1/2 tsp smoked paprika' → 'smoked paprika'
    """
    if not source_line:
        return None
    rest = source_line.strip()

    # Strip leading qty token(s): integer, decimal, fraction "1 1/2",
    # unicode fraction "½", range "1-2", or any whitespace between.
    rest = re.sub(r"^[\d\.\½\⅓\⅔\¼\¾\⅛\⅜\⅝\⅞\s\-/]+", "", rest)

    # Strip "to N" / "or N" range tails — "1 to 2 tablespoons butter"
    # becomes "tablespoons butter" after we also strip the leading
    # digit above. (The first number was already eaten.)
    rest = re.sub(r"^(?:to|or)\s+\d+(?:\.\d+)?\s+", "", rest, flags=re.IGNORECASE)

    # Strip leading parenthetical-qty like "(15-oz.)" — strip both
    # before AND after unit-removal, since the paren can appear either
    # before the unit ("(15-oz.) can …") or after ("cup (250 ml) …").
    rest = re.sub(r"^\([^)]+\)\s*", "", rest)
    rest = _LEADING_UNIT_RE.sub("", rest)
    rest = re.sub(r"^\([^)]+\)\s*", "", rest)

    # Final cleanup: strip leading "of " / "each " — these follow a
    # stripped unit ("1 head of garlic" → "of garlic" → "garlic";
    # "3/4 tsp each salt" → "each salt" → "salt").
    rest = re.sub(r"^(?:of|each)\s+", "", rest, flags=re.IGNORECASE)

    # Drop everything from first comma (typically prep_note).
    name = rest.split(",")[0].strip()

    # Drop trailing parentheticals + asterisks.
    name = re.sub(r"\s*\([^)]*\)\s*$", "", name).strip()
    name = name.rstrip("*").strip()

    return name or None


# --- LLM fallback: last resort when deterministic parse genuinely fails -


class LLMFallback(BaseModel):
    """Structured parse result from the LLM. The schema mirrors the
    deterministic parser's output so we can drop these fields in."""
    qty: float = Field(description="Numeric quantity. Use 0.0 only for 'to taste' / no qty.")
    unit: str = Field(description="Unit token: tsp, tbsp, cup, oz, lb, kg, g, ml, ea, inch, clove, sprig, stalk, head, can, pinch, slice, scoop, bottle, package")
    name: str = Field(description="Natural ingredient name a home cook would say (e.g. 'cinnamon', 'ground beef'). Drop USDA jargon.")
    prep_note: str | None = Field(default=None, description="Preparation instruction if present (chopped, diced, peeled, etc).")
    optional: bool = Field(default=False, description="True iff source explicitly says optional / for garnish / no specific qty.")


def _llm_parse_fallback(source_line: str) -> LLMFallback | None:
    """Last-resort parse via Claude Haiku when strangetom + our regex
    fallbacks couldn't structure the line. Returns None if API key
    missing or call fails — caller falls back to parse_failed.

    Trigger conditions (set by `_to_parsed_ingredient`):
      - qty extraction failed AND name is unknown (truly unparseable)
      - OR a known-difficult pattern in source ('inch', 'Extra Tbsp')

    Cached transitively via ParserCache (keyed on raw+parser_version),
    so re-imports don't re-pay.
    """
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        return None
    import anthropic  # lazy import — anthropic only needed for LLM paths
    client = anthropic.Anthropic(api_key=api_key)

    tool = {
        "name": "parsed_ingredient",
        "description": "Structured parse of a recipe ingredient line.",
        "input_schema": LLMFallback.model_json_schema(),
    }
    system = (
        "Extract structured qty/unit/name/prep from a recipe ingredient "
        "line. Output ONLY the parsed_ingredient tool call.\n\n"
        "Examples:\n"
        "  '1 inch cinnamon' → qty=1 unit=inch name='cinnamon'\n"
        "  '1 inch ginger, grated' → qty=1 unit=inch name='ginger' prep='grated'\n"
        "  '1 Extra Tbsp White Sugar' → qty=1 unit=tbsp name='white sugar' prep='extra'\n"
        "  '1 heaped teaspoon black tea leaves' → qty=1 unit=tsp name='black tea leaves' prep='heaped'\n"
        "  '2 large celery stalks, cut into 1\" pieces' → qty=2 unit=stalk name='celery' prep='large, cut into 1 inch pieces'\n"
        "  '1/4 sour cream' → qty=0.25 unit=cup name='sour cream' (cup inferred — typical recipe usage)\n"
        "  '500 ground meat' → qty=500 unit=g name='ground meat' (assume gram for large bare counts)\n"
        "  '1 4 to 5 pound leg of lamb' → qty=4 unit=lb name='leg of lamb' (the leading '1' is spurious; range = 4-5)\n"
    )
    try:
        resp = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=200,
            system=system,
            tools=[tool],
            tool_choice={"type": "tool", "name": "parsed_ingredient"},
            messages=[{"role": "user", "content": source_line}],
        )
    except Exception as e:
        log.warning("llm_parse_fallback API call failed on %r: %s", source_line, e)
        return None
    for block in resp.content:
        if getattr(block, "type", None) == "tool_use" and block.name == "parsed_ingredient":
            try:
                return LLMFallback.model_validate(block.input)
            except ValidationError as e:
                log.warning("llm_parse_fallback schema validation failed: %s", e)
                return None
    return None


def _ingredient_from_strangetom(
    s,  # IngredientText returned by strangetom
    foundation_idx: int | None,
    db: Session,
    source_line: str | None = None,
) -> ParsedIngredient:
    """Build one ParsedIngredient from a strangetom result, optionally
    picking a specific foundation_food index for compound lines."""
    # Foundation food (chosen) — used for name + category
    ff_obj = None
    if foundation_idx is not None and s.foundation_foods:
        if foundation_idx < len(s.foundation_foods):
            ff_obj = s.foundation_foods[foundation_idx]

    ff_text = getattr(ff_obj, "text", None) if ff_obj else None
    ff_conf = getattr(ff_obj, "confidence", None) if ff_obj else None

    # Name preference. The recipe author's words ARE the ground truth:
    #   1. Derive from source_line (strip qty/unit) — most faithful to
    #      what the recipe actually says.
    #   2. Fall back to strangetom's s.name when source is missing.
    #   3. Then ff_text (FDC name — sometimes weirdly off-topic).
    #   4. Finally "unknown".
    # This order means a source like "Cooking oil" won't get replaced
    # by an absurd ff_text like "Plantain, cooked with oil".
    derived = None
    if source_line:
        amt = s.amount[0] if s.amount else None
        unit_text = str(getattr(amt, "unit", "") or "") if amt else None
        qty_text = str(getattr(amt, "quantity", "") or "") if amt else None
        derived = _derive_name_from_source(source_line, qty_text, unit_text)
    s_name = getattr(s.name, "text", None) if s.name else None
    name_text = derived or s_name or ff_text or "unknown"

    # Amount → canonical (qty, unit, original_qty, original_unit)
    # strangetom returns a list of IngredientAmount; we take the first
    # (compound-with-different-quantities is rare; spec already
    # acknowledges this in docs/ingredient-parsing.md).
    qty: float = 0.0
    unit: str = "ea"
    original_qty: float | None = None
    original_unit: str | None = None
    if s.amount:
        a = s.amount[0]
        q_frac = getattr(a, "quantity", None)
        u = getattr(a, "unit", None)
        if q_frac is not None:
            q = float(Fraction(q_frac)) if isinstance(q_frac, (int, float, Fraction)) else float(q_frac)
            unit_str = str(u) if u is not None else "ea"
            qty, unit, original_qty, original_unit = to_canonical(q, unit_str)

    # Deterministic qty fallback: strangetom's CRF misses leading numbers
    # in lines like "0.5 red onion" or "1/4 sour cream" — no unit, just
    # a count + a name. Extract ourselves so the qty isn't lost.
    if qty == 0 and source_line:
        leading_qty, _rest = _extract_leading_qty(source_line)
        if leading_qty is not None:
            qty = leading_qty
            # unit stays "ea" — for count-without-unit lines that's the
            # right default (½ red onion → 0.5 ea red onion).

    # Prep note + size + comment
    prep_text = getattr(s.preparation, "text", None) if s.preparation else None
    size_text = getattr(s.size, "text", None) if s.size else None
    comment_text = getattr(s.comment, "text", None) if s.comment else None

    # Fallback: catch "N oz can" / "N ounce can" / "1 (15-oz.) can"
    # patterns that strangetom drops. Recipes use this all the time
    # ("1 14.5-oz can diced tomatoes") and the lost size info matters
    # for the shopping list. Only fires when size_text wasn't already
    # extracted, so we don't override a confident parse.
    if not size_text and source_line:
        m = _CONTAINER_SIZE_RE.search(source_line)
        if m:
            num = m.group(1)
            unit_word = m.group(2).lower()
            unit_norm = {
                "ounce": "oz", "ounces": "oz", "oz": "oz",
                "gram": "g", "grams": "g", "g": "g",
                "ml": "ml", "milliliter": "ml", "milliliters": "ml",
                "millilitre": "ml", "millilitres": "ml",
                "l": "l", "liter": "l", "liters": "l",
                "litre": "l", "litres": "l",
            }.get(unit_word, unit_word)
            size_text = f"{num} {unit_norm}"

    # strangetom mangles inline fractions in prep — '1/2"' becomes
    # '#1$2"'. Looks like a regex-backreference artefact. Undo it.
    if prep_text:
        prep_text = re.sub(r"#(\d+)\$(\d+)", r"\1/\2", prep_text)

    # Optionality. Three triggers:
    #   - literal "optional" / "for garnish" → genuinely optional
    #   - "to taste" / "as needed" — ONLY when there's no qty (else
    #     "to taste" means "adjust", not "skip")
    #   - name-only line (no qty extracted but a plausible ingredient
    #     name exists): treat as implicit "to taste"
    optional = False
    if comment_text:
        lc = comment_text.lower()
        if "optional" in lc or "for garnish" in lc:
            optional = True
        elif qty == 0 and any(kw in lc for kw in ("to taste", "as needed")):
            optional = True
            unit = "ea"
    if not optional and qty == 0 and name_text and name_text != "unknown":
        # Implicit "to taste" — line had no quantity but parser found
        # a name. Mark optional so it appears on the card. EXCEPT when
        # the SOURCE LINE itself looks like a USDA reference pasted as
        # data (e.g. "Turkey, dark meat, meat and skin, raw" sitting
        # in a bibimbap recipe). Those should propagate as parse
        # failures so they don't masquerade as real ingredients.
        if not _looks_like_usda_name(source_line or name_text):
            optional = True
            if unit not in {"ea", "g", "ml"}:
                unit = "ea"

    # Confidence: floor of the relevant fields
    confidences: list[float] = []
    if ff_conf is not None:
        confidences.append(float(ff_conf))
    if s.name and getattr(s.name, "confidence", None) is not None:
        confidences.append(float(s.name.confidence))
    if s.amount:
        c = getattr(s.amount[0], "confidence", None)
        if c is not None:
            confidences.append(float(c))
    confidence = min(confidences) if confidences else None

    # Canonical name lookup (creates an IngredientAlias row on first
    # encounter; cached forever). Prefer the foundation_food text as
    # input since FDC names are more stable than free-form display names.
    canonical_input = ff_text or name_text
    canonical_name, _conf, _src = resolve_canonical(canonical_input, db)

    category = resolve_category(ff_text, name_text)

    # Parse-failure guard. Three classes:
    #   (a) qty=0 and not optional — USDA name pasted as data, no qty
    #   (b) name_text == "unknown" — strangetom couldn't extract a name
    #   (c) canonical_name == "unknown" — resolver couldn't key it
    # All three propagate as parse failures so they don't leak into
    # display or aggregation. raw_text + source_line stay for review.
    parse_failed = (
        (qty == 0 and not optional)
        or name_text == "unknown"
        or canonical_name == "unknown"
    )

    # LLM fallback. Last resort when deterministic + regex layers
    # genuinely couldn't structure the line. Bounded — at most ~the
    # count of weird rows per import (~15). Cached transitively via
    # ParserCache so re-imports don't re-pay.
    #
    # Fires when:
    #   - parse_failed is true (deterministic gave up entirely)
    #   - source contains 'N inch X' (strangetom can't model inch)
    #   - unit slot has a non-unit word in it ('extra tbsp', 'heaped
    #     teaspoon', 'large stalk' — adjective polluted strangetom's
    #     unit extraction)
    #   - source has 'N M to/or O' two-number prefix
    needs_llm_fallback = parse_failed
    if not needs_llm_fallback and source_line:
        src_lc = source_line.lower()
        if re.search(r"\b\d+\s+inch(?:es)?\b", src_lc):
            needs_llm_fallback = True
        elif re.match(r"^\s*\d+(?:\.\d+)?\s+\d+(?:\.\d+)?\s+(?:to|or)\s+\d+", src_lc):
            needs_llm_fallback = True
        # Adjective polluting the unit slot — detect by checking whether
        # the unit strangetom returned has a word that ISN'T a real unit.
        elif unit and " " in unit:
            unit_words = unit.lower().split()
            recognised = {
                "tsp", "teaspoon", "tbsp", "tablespoon", "cup", "cups",
                "oz", "ounce", "lb", "pound", "g", "gram", "kg", "ml",
                "l", "liter", "litre", "inch", "in", "ea", "clove",
                "sprig", "stalk", "head", "bunch", "can", "jar", "bottle",
                "box", "bag", "package", "pkg", "pinch", "dash", "slice",
                "stick", "scoop", "rib",
            }
            if any(w not in recognised for w in unit_words):
                needs_llm_fallback = True
    if needs_llm_fallback and source_line:
        fb = _llm_parse_fallback(source_line)
        if fb is not None:
            qty = fb.qty
            unit = fb.unit
            name_text = fb.name
            # When LLM fires, use ITS prep_note as authoritative.
            # strangetom's was clearly missing context (otherwise we
            # wouldn't be in fallback territory).
            prep_text = fb.prep_note or prep_text
            if fb.optional:
                optional = True
            # Re-resolve canonical + category from the new name.
            canonical_name, _c2, _s2 = resolve_canonical(name_text, db)
            category = resolve_category(name_text, name_text)
            # Recompute parse_failed — LLM may have rescued the line.
            parse_failed = (
                (qty == 0 and not optional)
                or name_text == "unknown"
                or canonical_name == "unknown"
            )

    return ParsedIngredient(
        qty=qty,
        unit=unit,
        name=name_text,
        canonical_name=canonical_name,
        category=category,
        prep_note=prep_text,
        size_descriptor=size_text,
        optional=optional,
        original_qty=original_qty,
        original_unit=original_unit,
        foundation_food=ff_text,
        confidence=confidence,
        parse_failed=parse_failed,
    )


_StrangetomFn = Callable[[str], object]


def _default_strangetom(raw: str) -> object:
    """Wrapped for test stubbing — production calls into the real lib."""
    return _strangetom_parse(raw, foundation_foods=True)


def parse_line(
    raw: str,
    db: Session,
    *,
    strangetom: _StrangetomFn = _default_strangetom,
    parser_version: str = PARSER_VERSION,
) -> list[ParsedIngredient]:
    """Deterministic parse of one ingredient line. Returns a LIST because
    compound lines (salt and pepper) yield multiple ingredients via
    strangetom's foundation_foods array.

    Cache key is the normalised raw text + parser_version. Cache hits
    are free; misses run strangetom + Pint + category lookup.
    """
    raw = normalize(raw)
    key = cache_key(raw)

    cached = db.get(ParserCache, key)
    if cached is not None and cached.parser_version == parser_version:
        return [ParsedIngredient.model_validate(d) for d in json.loads(cached.result_json)]

    try:
        s = strangetom(raw)
    except Exception as e:
        raise ParserError(f"strangetom failed on {raw!r}: {e}") from e

    foundations = getattr(s, "foundation_foods", None) or []

    if foundations:
        # Disambiguate compounds from alternatives by quantity presence:
        #   "2.5 lb beef chuck, … or any other slow cooking beef"
        #     → ONE 2.5lb purchase; the alternatives are buying options.
        #     Strangetom's foundation_foods returns both possibilities.
        #     Take the highest-confidence one (already sorted by strangetom).
        #   "Salt and pepper to taste"
        #     → genuinely TWO ingredients sharing the same (zero) qty.
        #     Allow multiple ParsedIngredients.
        # Rule: quantified → 1 ingredient; un-quantified → all foundations.
        has_quantity = bool(s.amount) and any(
            getattr(a, "quantity", None) for a in s.amount
        )
        if has_quantity:
            ingredients = [_ingredient_from_strangetom(s, 0, db, source_line=raw)]
        else:
            ingredients = [
                _ingredient_from_strangetom(s, idx, db, source_line=raw) for idx in range(len(foundations))
            ]
    else:
        # No FDC match — single ingredient using just s.name + s.amount
        ingredients = [_ingredient_from_strangetom(s, None, db, source_line=raw)]

    # Stamp every produced ingredient with the verbatim input line so the
    # importer can preserve it on Ingredient.source_line.
    ingredients = [pi.model_copy(update={"source_line": raw}) for pi in ingredients]

    payload = json.dumps([pi.model_dump() for pi in ingredients])
    if cached is not None:
        cached.raw_text = raw
        cached.result_json = payload
        cached.parser_version = parser_version
        db.add(cached)
    else:
        db.add(
            ParserCache(
                raw_text_hash=key,
                raw_text=raw,
                result_json=payload,
                parser_version=parser_version,
            )
        )
    # flush() not commit() — parse_line participates in the caller's
    # transaction (e.g. xlsx_importer manages per-row savepoints).
    db.flush()
    return ingredients


def parse_blob(
    blob: str,
    db: Session,
    *,
    strangetom: _StrangetomFn = _default_strangetom,
    parser_version: str = PARSER_VERSION,
) -> list[ParsedSection]:
    """Section-aware parse of an ingredient text blob. Per-line failures
    land in `ParsedSection.failed` rather than aborting; importer flags
    those on SyncRun.needs_review."""
    out: list[ParsedSection] = []
    for header, raw_lines in split_blob_into_sections(blob):
        sect = ParsedSection(name=header)
        for raw in raw_lines:
            try:
                ingredients = parse_line(raw, db, strangetom=strangetom, parser_version=parser_version)
                sect.lines.extend(ingredients)
            except ParserError as e:
                log.warning("parser failed on %r: %s", raw, e)
                sect.failed.append((raw, str(e)))
        out.append(sect)
    return out


# TODO(followup): first-seen LLM verify pass.
#   - Add FoundationFoodAlias table (foundation_food → category +
#     canonical_name override + confidence + source).
#   - When resolve_category returns "Other" OR strangetom confidence
#     < threshold, call Claude Haiku once per foundation_food and
#     cache the answer.
#   - --llm-verify=off|first-seen|every|audit-only on the importer.
