"""Trace mode for the ingredient parser.

Mirrors `parse_line` step-by-step but captures the intermediate state
at every stage — strangetom raw output, derived name, canonical key,
category guess, LLM-verify input/output, final ParsedIngredient. The
output is meant to be eyeballed in a debug UI to see WHERE a parse
goes wrong.

Doesn't write to ParserCache or IngredientAlias. Uses a savepoint
that gets rolled back so the caller's session stays clean — the trace
is a pure read.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from fractions import Fraction
from functools import lru_cache
from typing import Any

from sqlmodel import Session


# Stopwords + stemming for info-loss detection. Lifted to module level
# so review/trace surfaces share one definition.
_STOP_WORDS = {
    "of", "a", "an", "the", "and", "or", "to", "for", "with", "in",
    "from", "into", "each", "other", "any", "some", "as",
    "ml", "g", "kg", "oz", "lb", "ounce", "ounces", "pound", "pounds",
    "cup", "cups", "tbsp", "tablespoon", "tablespoons", "tsp",
    "teaspoon", "teaspoons", "clove", "cloves", "can", "cans",
    "jar", "jars", "bottle", "bottles", "bunch", "head", "stalk",
    "stalks", "pinch", "dash", "slice", "slices", "stick", "sticks",
    "rib", "ribs", "sprig", "sprigs", "shot", "scoop", "ea", "pod",
    "small", "medium", "large", "extra", "thick", "thin", "long",
    "short", "wide", "deep", "tall",
    "chunk", "chunks", "piece", "pieces", "wedge", "wedges",
    "strip", "strips", "cube", "cubes", "ring", "rings",
    "round", "rounds", "ball", "balls",
    "boneless", "skinless", "raw", "cooked", "uncooked", "frozen",
    "drained", "rinsed", "cut", "uncut",
    "inch", "inches", "portion", "portions", "serving", "servings", "slow",
    "chopped", "diced", "minced", "sliced", "shredded", "grated",
    "peeled", "mashed", "toasted", "boiled", "fried", "baked",
    "scraped", "split", "rolled", "pressed", "halved", "quartered",
    "roughly", "finely", "freshly", "thinly", "coarsely", "lightly",
    "heavily", "approximately", "about", "approx", "slowly", "quickly",
    "cooking",
}


# Unit / abbreviation synonyms. Lets 'lbs' match 'lb', 'tablespoon'
# match 'tbsp', etc. — morphological stemming alone can't bridge
# abbreviation pairs. Lookup runs before AND after morphology so both
# plural ('lbs') and long-form ('pounds') reduce to the canonical
# abbreviation.
_UNIT_SYNONYMS = {
    "lb": "lb", "lbs": "lb", "pound": "lb", "pounds": "lb",
    "oz": "oz", "ounce": "oz", "ounces": "oz",
    "g": "g", "gram": "g", "grams": "g",
    "kg": "kg", "kilogram": "kg", "kilograms": "kg",
    "ml": "ml", "milliliter": "ml", "millilitre": "ml",
    "milliliters": "ml", "millilitres": "ml",
    "l": "l", "liter": "l", "liters": "l", "litre": "l", "litres": "l",
    "tsp": "tsp", "teaspoon": "tsp", "teaspoons": "tsp",
    "tbsp": "tbsp", "tablespoon": "tbsp", "tablespoons": "tbsp",
    "fl": "floz", "floz": "floz", "fluid": "floz",
    "cup": "cup", "cups": "cup",
    "pinch": "pinch", "pinches": "pinch",
    "dash": "dash", "dashes": "dash",
    "clove": "clove", "cloves": "clove",
    "stick": "stick", "sticks": "stick",
    "stalk": "stalk", "stalks": "stalk",
    "rib": "rib", "ribs": "rib",
    "sprig": "sprig", "sprigs": "sprig",
    "head": "head", "heads": "head",
    "bunch": "bunch", "bunches": "bunch",
    "can": "can", "cans": "can",
    "jar": "jar", "jars": "jar",
    "bottle": "bottle", "bottles": "bottle",
    "slice": "slice", "slices": "slice",
}


def stem_word(w: str) -> str:
    """Singularise for set-match comparison. Goal isn't morphological
    correctness — just stable hashing for source-vs-canonical diffs.
    Runs the unit-synonym table both before and after morphology so
    e.g. 'lbs' → 'lb' (synonym), 'pounds' → 'pound' (morphology) →
    'lb' (synonym)."""
    if w in _UNIT_SYNONYMS:
        return _UNIT_SYNONYMS[w]
    if len(w) <= 3:
        return _UNIT_SYNONYMS.get(w, w)
    if w.endswith("ies"):
        stemmed = w[:-3] + "y"
    elif w.endswith("ves") and len(w) > 4:
        stemmed = w[:-3] + "f"
    elif w.endswith(("ches", "shes", "sses", "xes", "zes")):
        stemmed = w[:-2]
    elif w.endswith("oes") and len(w) > 4:
        stemmed = w[:-2]
    elif w.endswith("s") and not w.endswith(("ss", "us", "is")):
        stemmed = w[:-1]
    else:
        stemmed = w
    return _UNIT_SYNONYMS.get(stemmed, stemmed)


def content_words(text: str) -> set[str]:
    """Lowercase content words from `text`, with stopwords removed and
    short tokens dropped. Used for severity-dot info-loss diffs (where
    a missing 'the' or 'of' shouldn't count as a real loss)."""
    import re as _re
    words = _re.findall(r"[a-zA-Z]+", (text or "").lower())
    return {stem_word(w) for w in words if w not in _STOP_WORDS and len(w) > 2}


def all_words(text: str) -> set[str]:
    """Every word stem in `text`. No stop filter — used for the
    side-by-side highlighter. Uses \\w+ so Unicode letters ('Sauté')
    AND digits ('1', '2') are captured."""
    import re as _re
    words = _re.findall(r"\w+", (text or "").lower())
    return {stem_word(w) for w in words}

from mise.services.canonical_resolver import snake_case
from mise.services.ingredient_parser import (
    PARSER_VERSION,
    _abbrev_unit,
    _default_strangetom,
    _derive_name_from_source,
    _extract_leading_qty,
    _looks_like_usda_name,
    apply_category_preference,
    cache_key,
    normalize,
    resolve_category,
    split_blob_into_sections,
    to_canonical,
)
from mise.services.llm_verify import (
    _alias_key,
    _canonical_is_post_modifier,
    _canonical_validates_source,
    _llm_output_grounded_in_source,
)
from mise.models import IngredientAlias, ParserCache


@dataclass
class LineTrace:
    """Every intermediate value for a single source line."""
    source_line: str
    normalized: str
    # compound-line bookkeeping. "Salt and pepper" yields multiple
    # traces sharing the same source_line; index/total reflect that.
    compound_index: int  # 0 when not compound
    compound_total: int  # 1 when not compound
    # strangetom output
    strangetom_amount_raw: dict[str, Any]
    strangetom_name: str | None
    strangetom_preparation: str | None
    strangetom_size: str | None
    strangetom_comment: str | None
    strangetom_foundation_food: str | None
    # strangetom confidences (None when field absent)
    strangetom_qty_confidence: float | None
    strangetom_name_confidence: float | None
    strangetom_ff_confidence: float | None
    strangetom_all_foundations: list[dict[str, Any]]  # full list for compounds
    # name resolution
    derived_name_from_source: str | None
    chosen_name: str
    name_chosen_via: str  # "derive" | "strangetom" | "ff_text" | "unknown"
    # qty/unit
    qty_raw: float
    unit_raw: str
    qty_after_canonical: float
    unit_after_canonical: str
    original_qty: float | None
    original_unit: str | None
    qty_from_leading_fallback: bool  # _extract_leading_qty kicked in
    # Size-into-unit leak heuristic: source has 'N <size-word> <unit>'
    # (e.g. '12 ounce jar') but strangetom dropped the size into the
    # unit slot. Highlights where '12 ounce' info got lost upstream
    # of our pipeline.
    size_likely_leaked: bool
    size_leak_detail: str | None  # human-readable explanation
    # canonical lookup
    canonical_input: str  # what got passed to resolve_canonical
    canonical_alias_existed: bool
    canonical_alias_source: str | None  # 'auto' | 'llm' | 'manual' | None
    canonical_name: str
    # category
    category_resolved: str
    # category preference
    qty_after_preference: float
    unit_after_preference: str
    preference_rule: str  # "no_pref" | "rule1_restore" | "rule1b_kept" | "rule2_convert" | "rule2_unchanged"
    # parse-failed + llm fallback (the deterministic-give-up fallback,
    # NOT the verify pass — they're separate)
    parse_failed: bool
    llm_fallback_triggered: bool
    llm_fallback_reason: str | None  # which trigger fired
    llm_fallback_called: bool        # would the actual call go out? requires API key
    # LLM verify
    llm_verify_mode: str
    llm_verify_fired: bool
    llm_verify_skip_reason: str | None
    llm_verify_input: dict[str, Any] | None
    llm_verify_raw_output: dict[str, Any] | None
    llm_verify_grounded: bool | None
    llm_verify_applied: bool  # did the answer actually get applied
    # final
    final_qty: float
    final_unit: str
    final_display_name: str
    final_canonical_name: str
    final_prep_note: str | None
    final_size_descriptor: str | None
    final_category: str
    final_optional: bool
    # Cache + LLM-extracted alternative
    parser_cache_hit: bool   # live ParserCache has this key at current PARSER_VERSION
    parser_cache_version: str | None
    alternatives_note: str | None  # from IngredientAlias if LLM verify previously set it


def trace_blob(blob: str, db: Session) -> list[tuple[str | None, list[list[LineTrace]]]]:
    """Parse `blob` with full instrumentation. Returns
    [(section_name, [[LineTrace, ...], ...]), ...].

    The inner list-of-lists is per source-line: usually one trace,
    but compound lines like 'salt and pepper to taste' yield N traces
    (one per foundation_food). Doesn't mutate the DB.

    Runs strangetom in parallel across lines via ThreadPoolExecutor —
    CRF inference is CPU-bound but releases the GIL during the C
    extension call, so threading actually scales. Cuts a 25-line
    cold trace from ~750ms to ~150ms.
    """
    from concurrent.futures import ThreadPoolExecutor

    sections = split_blob_into_sections(blob)

    # Pre-flight: warm the lru_cache for every line in parallel.
    all_lines: list[str] = []
    for _, lines in sections:
        all_lines.extend(lines)
    normalized = [normalize(line) for line in all_lines]
    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(_cached_strangetom, normalized))

    out: list[tuple[str | None, list[list[LineTrace]]]] = []
    for section_name, lines in sections:
        traces_per_line = [_trace_line(line, db) for line in lines]
        out.append((section_name, traces_per_line))
    return out


def _strangetom_to_dict(s) -> dict[str, Any]:
    """Best-effort dump of strangetom's IngredientText into a dict so we
    can show the raw structure."""
    d: dict[str, Any] = {}
    amt_list = []
    for a in (s.amount or []):
        amt_list.append({
            "quantity": str(getattr(a, "quantity", "")),
            "unit": str(getattr(a, "unit", "")),
            "confidence": getattr(a, "confidence", None),
        })
    d["amount"] = amt_list
    d["name"] = {
        "text": getattr(s.name, "text", None) if s.name else None,
        "confidence": getattr(s.name, "confidence", None) if s.name else None,
    }
    d["preparation"] = getattr(s.preparation, "text", None) if s.preparation else None
    d["size"] = getattr(s.size, "text", None) if s.size else None
    d["comment"] = getattr(s.comment, "text", None) if s.comment else None
    ff_list = []
    for ff in (getattr(s, "foundation_foods", None) or []):
        ff_list.append({
            "text": getattr(ff, "text", None),
            "confidence": getattr(ff, "confidence", None),
        })
    d["foundation_foods"] = ff_list
    return d


@lru_cache(maxsize=1024)
def _cached_strangetom(normalized: str) -> tuple[Any, dict[str, Any]]:
    """In-process memo of (strangetom IngredientText, dict-extracted
    form). The CRF call is ~30ms per line — re-clicking Trace on the
    same blob was making the page feel slow. Cache keyed on the
    normalized source line so the second click returns instantly."""
    s = _default_strangetom(normalized)
    return s, _strangetom_to_dict(s)


def _trace_line(raw: str, db: Session) -> list[LineTrace]:
    """Mirror of parse_line + _ingredient_from_strangetom + verify, with
    state captured at each step. Returns a LIST — usually 1 entry, but
    a compound line ('salt and pepper to taste') yields N entries, one
    per foundation_food. No writes; pure observation."""
    src = normalize(raw)
    s, st_dict = _cached_strangetom(src)

    # Decide how many traces this line produces. Mirrors parse_line's
    # branching: compound = un-quantified line with multiple FDC matches.
    foundations = st_dict.get("foundation_foods") or []
    has_quantity = bool(s.amount) and any(
        getattr(a, "quantity", None) for a in s.amount
    )
    if foundations and not has_quantity and len(foundations) > 1:
        indices = list(range(len(foundations)))
    elif foundations:
        indices = [0]
    else:
        indices = [None]

    return [
        _trace_one(raw, src, s, st_dict, foundations, idx, len(indices), i, db)
        for i, idx in enumerate(indices)
    ]


def _trace_one(
    raw: str, src: str, s: Any, st_dict: dict[str, Any],
    foundations: list[dict[str, Any]],
    foundation_idx: int | None, compound_total: int, compound_index: int,
    db: Session,
) -> LineTrace:
    """Single trace given a specific foundation_food index."""
    # ---- name resolution ----
    derived = None
    if src:
        amt = s.amount[0] if s.amount else None
        unit_text = str(getattr(amt, "unit", "") or "") if amt else None
        qty_text = str(getattr(amt, "quantity", "") or "") if amt else None
        derived = _derive_name_from_source(src, qty_text, unit_text)
    s_name = getattr(s.name, "text", None) if s.name else None
    ff_obj = foundations[foundation_idx] if (foundation_idx is not None and foundation_idx < len(foundations)) else None
    ff_text = ff_obj.get("text") if ff_obj else None
    ff_conf = ff_obj.get("confidence") if ff_obj else None

    if derived:
        chosen_name, name_via = derived, "derive"
    elif s_name:
        chosen_name, name_via = s_name, "strangetom"
    elif ff_text:
        chosen_name, name_via = ff_text, "ff_text"
    else:
        chosen_name, name_via = "unknown", "unknown"

    # ---- qty / unit ----
    qty_raw, unit_raw = 0.0, "ea"
    qty, unit = 0.0, "ea"
    original_qty, original_unit = None, None
    if s.amount:
        a = s.amount[0]
        q_frac = getattr(a, "quantity", None)
        u = getattr(a, "unit", None)
        if q_frac is not None:
            try:
                q_num = float(Fraction(q_frac)) if isinstance(q_frac, (int, float, Fraction)) else float(q_frac)
                qty_raw = q_num
                unit_raw = str(u) if u else "ea"
                qty, unit, original_qty, original_unit = to_canonical(q_num, unit_raw)
            except Exception:
                pass

    qty_from_leading = False
    if qty == 0 and src:
        leading_qty, _ = _extract_leading_qty(src)
        if leading_qty is not None:
            qty = leading_qty
            qty_raw = leading_qty
            qty_from_leading = True

    # Size-leak heuristic: source contains 'N <size-word> <unit>' (the
    # '12 ounce jar' / '15 oz can' / '32-oz bottle' pattern) but
    # strangetom's size field is empty. Confirms the upstream
    # tokenisation dropped the size into the unit slot.
    _SIZE_WORDS = {"oz", "ounce", "ounces", "lb", "pound", "pounds",
                   "g", "gram", "grams", "kg", "ml", "l", "litre", "liter"}
    _CONTAINER_WORDS = {"jar", "can", "bottle", "box", "bag", "package", "pkg", "tin", "tub"}
    size_leaked = False
    size_leak_detail: str | None = None
    if src and not st_dict.get("size"):
        import re as _re2
        m = _re2.search(
            r"\b(\d+(?:\.\d+)?)[\s-]+([a-z]+)\s+([a-z]+)\b",
            (src or "").lower(),
        )
        if m:
            num, w1, w2 = m.group(1), m.group(2), m.group(3)
            if w1 in _SIZE_WORDS and w2 in _CONTAINER_WORDS:
                size_leaked = True
                size_leak_detail = (
                    f"source contains '{num} {w1} {w2}' but strangetom's "
                    f"size field is empty — the '{num} {w1}' likely got "
                    f"folded into the unit slot ({unit_raw!r})"
                )

    # ---- canonical lookup ----
    canonical_input = ff_text or chosen_name
    key = (canonical_input or "").lower().strip()
    existing_alias = db.get(IngredientAlias, key) if key else None
    if existing_alias is not None:
        canonical_name = existing_alias.canonical_name
        alias_source = existing_alias.source
    else:
        canonical_name = snake_case(canonical_input) if canonical_input else "unknown"
        alias_source = None

    # ---- category ----
    category = resolve_category(ff_text, chosen_name)

    # ---- preference ----
    qty_before_pref, unit_before_pref = qty, unit
    qty, unit, _oq, _ou = apply_category_preference(
        qty, unit, original_qty, original_unit, category,
    )
    pref_rule = _classify_pref_rule(
        qty_before_pref, unit_before_pref, qty, unit,
        original_qty, original_unit, category,
    )

    # ---- parse_failed ----
    comment_text = getattr(s.comment, "text", None) if s.comment else None
    optional = False
    if comment_text:
        lc = comment_text.lower()
        if "optional" in lc or "for garnish" in lc:
            optional = True
        elif qty == 0 and any(kw in lc for kw in ("to taste", "as needed")):
            optional = True
    if not optional and qty == 0 and chosen_name and chosen_name != "unknown":
        if not _looks_like_usda_name(src or chosen_name):
            optional = True
    parse_failed = (
        (qty == 0 and not optional)
        or chosen_name == "unknown"
        or canonical_name == "unknown"
    )

    # ---- LLM fallback (deterministic give-up; separate from LLM verify) ----
    # Mirrors the trigger logic in _ingredient_from_strangetom.
    import re as _re
    llm_fb_triggered = False
    llm_fb_reason: str | None = None
    if parse_failed:
        llm_fb_triggered = True
        llm_fb_reason = "deterministic parse failed (qty=0+!optional / unknown name / unknown canonical)"
    elif src:
        src_lc = src.lower()
        if _re.search(r"\b\d+\s+inch(?:es)?\b", src_lc):
            llm_fb_triggered = True
            llm_fb_reason = "'N inch X' pattern — strangetom can't model inches"
        elif _re.match(r"^\s*\d+(?:\.\d+)?\s+\d+(?:\.\d+)?\s+(?:to|or)\s+\d+", src_lc):
            llm_fb_triggered = True
            llm_fb_reason = "two-number prefix ('1 4 to 5 pound …')"
        elif unit and " " in unit:
            _recognised = {
                "tsp", "teaspoon", "tbsp", "tablespoon", "cup", "cups",
                "oz", "ounce", "lb", "pound", "g", "gram", "kg", "ml",
                "l", "liter", "litre", "inch", "in", "ea", "clove",
                "sprig", "stalk", "head", "bunch", "can", "jar", "bottle",
                "box", "bag", "package", "pkg", "pinch", "dash", "slice",
                "stick", "scoop", "rib",
            }
            if any(w not in _recognised for w in unit.lower().split()):
                llm_fb_triggered = True
                llm_fb_reason = f"adjective-polluted unit ({unit!r})"

    # Whether the fallback would actually fire on the live importer
    # (requires ANTHROPIC_API_KEY) — purely informational in trace mode.
    llm_fb_called = llm_fb_triggered and bool(os.environ.get("ANTHROPIC_API_KEY"))

    # ---- LLM verify ----
    llm_v_fired = False
    llm_v_skip_reason: str | None = None
    llm_v_input: dict[str, Any] | None = None
    llm_v_raw: dict[str, Any] | None = None
    llm_v_grounded: bool | None = None
    llm_v_applied = False

    mode = "first-seen"  # what the importer typically uses
    if not key or key == "unknown":
        llm_v_skip_reason = "no_key (display='unknown')"
    elif existing_alias is not None and existing_alias.source in ("llm", "manual"):
        # Cache short-circuit — but the new guard re-checks grounding
        force = (
            existing_alias.source == "llm"
            and (
                not _canonical_validates_source(existing_alias.canonical_name, src)
                or _canonical_is_post_modifier(existing_alias.canonical_name, src)
            )
        )
        if force:
            llm_v_skip_reason = "cache hit but grounding failed → would re-fire"
            llm_v_fired = True
        else:
            llm_v_skip_reason = f"cache hit (source={existing_alias.source})"
    else:
        # first-seen gate: only fire if Other or source=auto
        if category == "Other" or (existing_alias and existing_alias.source == "auto"):
            llm_v_fired = True
        else:
            llm_v_skip_reason = (
                f"first-seen skip (category={category!r}, "
                f"alias_source={alias_source!r})"
            )

    if llm_v_fired:
        llm_v_input = {
            "source_line": src,
            "foundation_food": ff_text,
            "display_name": chosen_name,
            "suggested_canonical": canonical_name,
            "suggested_category": category,
        }
        # We CAN'T cheaply re-fire without an API call. The trace
        # captures the inputs; the actual call is left to read from
        # cache (existing IngredientAlias) if available.
        if existing_alias is not None and existing_alias.source == "llm":
            llm_v_raw = {
                "canonical_name": existing_alias.canonical_name,
                "category": existing_alias.category,
                "display_name_pretty": existing_alias.display_name_pretty,
                "_note": "captured from existing alias row (last LLM result)",
            }
            llm_v_grounded = _llm_output_grounded_in_source(
                existing_alias.canonical_name, src,
            )
            llm_v_applied = llm_v_grounded
        else:
            llm_v_raw = {"_note": "would fire fresh — no cached LLM answer to display"}

    # ---- final ----
    if llm_v_applied and llm_v_raw and "canonical_name" in llm_v_raw:
        final_canonical = llm_v_raw["canonical_name"]
        final_category = llm_v_raw.get("category") or category
        final_display = llm_v_raw.get("display_name_pretty") or chosen_name
    else:
        final_canonical = canonical_name
        final_category = category
        final_display = chosen_name

    # Prep + size pass through unmodified — strangetom extracts them,
    # the rest of the pipeline keeps them as-is, they land on the
    # Ingredient row alongside qty/unit/name.
    prep_text = st_dict["preparation"]
    if prep_text:
        # Reverse strangetom's '#1$2' artefact (its regex backref escape
        # for inline fractions like '1/2"'). Mirrors what
        # _ingredient_from_strangetom does.
        import re as _re
        prep_text = _re.sub(r"#(\d+)\$(\d+)", r"\1/\2", prep_text)
    size_text = st_dict["size"]

    # Confidences — strangetom returns floats per field. None when absent.
    qty_conf = st_dict["amount"][0].get("confidence") if st_dict["amount"] else None
    name_conf = st_dict["name"].get("confidence") if st_dict["name"] else None

    # ParserCache lookup against the LIVE DB at the current PARSER_VERSION.
    # Trace doesn't read the cached result — just notes whether the live
    # importer would have skipped the whole parse pipeline.
    pc_key = cache_key(src) if src else None
    pc_row = db.get(ParserCache, pc_key) if pc_key else None
    pc_hit = pc_row is not None and pc_row.parser_version == PARSER_VERSION
    pc_version = pc_row.parser_version if pc_row else None

    # alternatives_note from cached IngredientAlias (LLM verify writes it).
    alt_note = existing_alias.display_name_pretty if False else None  # placeholder
    if existing_alias is not None:
        # The actual alternatives_note field lives on the cached LLM
        # verify result, NOT on IngredientAlias. We can't recover it
        # without re-firing the LLM, so this stays None except for the
        # currently-traced row when verify ran fresh (out of scope).
        alt_note = None

    return LineTrace(
        source_line=raw,
        normalized=src,
        compound_index=compound_index,
        compound_total=compound_total,
        strangetom_amount_raw=st_dict["amount"],
        strangetom_name=st_dict["name"]["text"],
        strangetom_preparation=st_dict["preparation"],
        strangetom_size=st_dict["size"],
        strangetom_comment=st_dict["comment"],
        strangetom_foundation_food=ff_text,
        strangetom_qty_confidence=qty_conf,
        strangetom_name_confidence=name_conf,
        strangetom_ff_confidence=ff_conf,
        strangetom_all_foundations=foundations,
        derived_name_from_source=derived,
        chosen_name=chosen_name,
        name_chosen_via=name_via,
        qty_raw=qty_raw,
        unit_raw=unit_raw,
        qty_after_canonical=qty_before_pref,
        unit_after_canonical=unit_before_pref,
        original_qty=original_qty,
        original_unit=original_unit,
        qty_from_leading_fallback=qty_from_leading,
        size_likely_leaked=size_leaked,
        size_leak_detail=size_leak_detail,
        canonical_input=canonical_input or "",
        canonical_alias_existed=existing_alias is not None,
        canonical_alias_source=alias_source,
        canonical_name=canonical_name,
        category_resolved=category,
        qty_after_preference=qty,
        unit_after_preference=unit,
        preference_rule=pref_rule,
        parse_failed=parse_failed,
        llm_fallback_triggered=llm_fb_triggered,
        llm_fallback_reason=llm_fb_reason,
        llm_fallback_called=llm_fb_called,
        llm_verify_mode=mode,
        llm_verify_fired=llm_v_fired,
        llm_verify_skip_reason=llm_v_skip_reason,
        llm_verify_input=llm_v_input,
        llm_verify_raw_output=llm_v_raw,
        llm_verify_grounded=llm_v_grounded,
        llm_verify_applied=llm_v_applied,
        final_qty=qty,
        final_unit=unit,
        final_display_name=final_display,
        final_canonical_name=final_canonical,
        final_prep_note=prep_text,
        final_size_descriptor=size_text,
        final_category=final_category,
        final_optional=optional,
        parser_cache_hit=pc_hit,
        parser_cache_version=pc_version,
        alternatives_note=alt_note,
    )


def _classify_pref_rule(
    qty_in: float, unit_in: str, qty_out: float, unit_out: str,
    original_qty: float | None, original_unit: str | None, category: str,
) -> str:
    """Best-effort label for which branch of apply_category_preference
    fired. The function itself doesn't return a reason so we infer."""
    from mise.services.ingredient_parser import PREFERRED_UNITS_BY_CATEGORY
    pref = PREFERRED_UNITS_BY_CATEGORY.get(category, [])
    if not pref:
        if original_qty is not None and original_unit is not None:
            return "no_pref → restored original"
        return "no_pref → unchanged"
    orig_abbr = _abbrev_unit(original_unit)
    if orig_abbr and orig_abbr in pref and original_qty is not None:
        if abs(qty_out - original_qty) < 1e-3 and unit_out == orig_abbr:
            return f"rule1 (restored {orig_abbr})"
    cur_abbr = _abbrev_unit(unit_in)
    if cur_abbr and cur_abbr in pref and qty_out == qty_in and unit_out == unit_in:
        return "rule1b (already preferred)"
    if qty_out != qty_in or unit_out != unit_in:
        return f"rule2 (converted to {pref[0]})"
    return "rule2 attempted, unchanged"
