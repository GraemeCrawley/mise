"""LLM verify pass — first-seen check for category + canonical_name.

The deterministic parser (strangetom + Pint + canonical_resolver) gives
us a fast, free baseline. But FDC foundation_foods have quirks
("paprika" → "Peppers, hungarian, raw"; "bacon" → "Beef, bacon, cooked")
that the static prefix map can't reliably correct. This service runs
ONCE per unique foundation_food across the whole library and caches
the answer in IngredientAlias.

Cost shape:
  - One LLM call per UNIQUE foundation_food seen by the importer.
  - Library import (~43 recipes, ~150 unique ingredients) → ~$0.15.
  - Re-imports: ~$0 (cache hits).
  - Steady state: ~$0.001 per new recipe with a never-seen ingredient.

Verify modes (passed through from the importer):
  - "off":         no LLM call ever
  - "first-seen":  call LLM only when prefix-map returned "Other" OR
                   the alias was just auto-generated (source="auto")
  - "every":       call LLM for every line (override even good prefix
                   matches — heavy; useful for the very first import
                   to catch belt-and-braces)
  - "audit-only":  no inline LLM calls; flag candidates for a separate
                   batch audit (not implemented yet)
"""

from __future__ import annotations

import logging
import os
from typing import Literal

import anthropic
from pydantic import BaseModel, Field, ValidationError
from sqlmodel import Session

from mise.models import IngredientAlias
from mise.services.ingredient_parser import ParsedIngredient

log = logging.getLogger("mise.llm_verify")

VerifyMode = Literal["off", "first-seen", "every", "audit-only"]


def _reapply_unit_preference(pi: ParsedIngredient, original_category: str) -> ParsedIngredient:
    """No-op under natural-unit storage. Units are now stored exactly as the
    recipe wrote them and no longer depend on category, so an LLM category
    upgrade (Other → Beverages, etc.) never needs the unit re-derived. Kept
    as a pass-through so the two call sites stay stable; retire in a later
    cleanup. (Previously this re-ran apply_category_preference, which under
    natural storage would wrongly convert the natural unit to a category
    default via Rule 2.)"""
    return pi

VALID_CATEGORIES = {
    "Produce", "Dairy", "Meat", "Bakery", "Deli", "Frozen",
    "Pantry", "Sauces", "Pasta/Rice", "Snacks", "Beverages",
    "Spices", "Other",
}


class VerifyOutput(BaseModel):
    canonical_name: str = Field(description="snake_case aggregation key, specific (yellow_onion not onion)")
    category: str = Field(description="grocery section per household config")
    display_name_pretty: str = Field(
        description="Natural English label a home cook would say. Anchor on "
                    "the SOURCE LINE meaning, not the FDC name. Examples: "
                    "source 'bacon' + FDC 'Beef, bacon, cooked' -> 'bacon' "
                    "(not 'beef bacon' — recipes default to pork); "
                    "source 'flour' + FDC 'Flour, 00' -> 'all-purpose flour' "
                    "(generic flour means AP, not Italian 00); "
                    "source '1 Can Guinness Beer' -> 'Guinness beer' "
                    "(preserve brand modifier); "
                    "source '3 sprigs thyme (or 1 tsp dried thyme)' -> "
                    "'fresh thyme' (alternative goes to alternatives_note)."
    )
    alternatives_note: str | None = Field(
        default=None,
        description="If the source line offers alternatives ('chicken/beef "
                    "stock', '(or dried thyme)', 'X or Y'), capture the "
                    "alternative as a short phrase suitable for display. "
                    "Null when the source has no alternative. Examples: "
                    "'3 cups chicken/beef stock' -> 'or beef stock'; "
                    "'3 sprigs thyme (or 1 tsp dried thyme)' -> "
                    "'or 1 tsp dried thyme'; "
                    "'olive oil' -> null."
    )


def _llm_verify(
    foundation_food: str | None,
    display_name: str,
    suggested_canonical: str,
    suggested_category: str,
    source_line: str | None = None,
    *,
    model: str = "claude-haiku-4-5-20251001",
) -> VerifyOutput | None:
    """One Claude Haiku call. Returns None if API unreachable, key
    missing, or schema validation fails — caller falls back to the
    deterministic suggestion."""
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        return None
    client = anthropic.Anthropic(api_key=api_key)

    tool = {
        "name": "verified_ingredient",
        "description": "Verified canonical_name + grocery category for this ingredient.",
        "input_schema": VerifyOutput.model_json_schema(),
    }

    cats = " | ".join(sorted(VALID_CATEGORIES))
    system = (
        f"You verify a food's grocery category, canonical_name, display "
        f"name, AND extract any alternatives from the source line. Valid "
        f"categories: {cats}.\n\n"
        f"GROUND TRUTH IS THE SOURCE LINE, not the FDC name. FDC matches "
        f"the broadest plausible foundation_food and is often wrong about "
        f"specifics:\n"
        f"  - source 'bacon' + FDC 'Beef, bacon, cooked' → canonical=bacon, "
        f"display='bacon' (recipes default to pork, not beef bacon)\n"
        f"  - source 'flour' + FDC 'Flour, 00' → canonical=all_purpose_flour, "
        f"display='all-purpose flour' (generic flour is AP, not Italian 00)\n"
        f"  - source 'paprika' + FDC 'Peppers, hungarian, raw' → "
        f"canonical=paprika, category=Spices (dried spice, not fresh produce)\n"
        f"  - source '1 Can Guinness Beer' → display='Guinness beer' "
        f"(preserve brand modifiers)\n"
        f"  - source 'whole milk' + FDC 'Milk, whole' → 'whole milk' is fine\n"
        f"  - source 'N Cloves' (no 'garlic' qualifier) → canonical=cloves, "
        f"category=Spices, display='whole cloves' (the dried spice — clove "
        f"buds — NOT garlic cloves; strangetom often mismatches to garlic)\n\n"
        f"CRITICAL — strangetom drops modifier words. When source has "
        f"'<MODIFIER> bean' / '<MODIFIER> pepper' / '<MODIFIER> sauce' / "
        f"'<MODIFIER> stock' / etc., the modifier IS part of the canonical. "
        f"Keep BOTH words in canonical_name. The FDC name and the parser's "
        f"suggested guess will likely contain ONLY the second word — DO NOT "
        f"trust them. Read source_line, find the noun phrase, keep the "
        f"modifier:\n"
        f"  - source '1 Vanilla Bean (Split & Scraped)' → "
        f"canonical=vanilla_bean, category=Spices, display='vanilla bean' "
        f"(NOT 'canned_beans', NOT 'beans' — it's a vanilla pod)\n"
        f"  - source '1 cup kidney beans' → canonical=kidney_beans, "
        f"category=Pantry (NOT just 'beans')\n"
        f"  - source '2 lima beans' → canonical=lima_beans (NOT 'beans')\n"
        f"  - source 'roasted red pepper' → canonical=roasted_red_pepper "
        f"(NOT just 'pepper' or 'red_pepper')\n"
        f"  - source 'oyster sauce' → canonical=oyster_sauce (NOT 'sauce')\n"
        f"  - source 'beef stock' → canonical=beef_stock (NOT 'stock')\n"
        f"Rule of thumb: if source_line names the ingredient as <X> <Y>, "
        f"canonical MUST be <x>_<y>, not just <y>.\n\n"
        f"canonical_name rules:\n"
        f"  - snake_case (lowercase, underscores)\n"
        f"  - specific: yellow_onion not onion; ground_pork not pork\n"
        f"  - one stable key across phrasings\n"
        f"  - EVERY word in canonical_name must appear in source_line. If "
        f"source says 'vanilla bean', canonical_name=vanilla_bean is fine "
        f"(both words present); canonical_name=canned_beans is REJECTED "
        f"('canned' isn't in the source).\n\n"
        f"alternatives_note: capture 'or X' / 'X/Y' / '(or X)' patterns from "
        f"the source line — REAL alternatives ('use X instead of Y'). Null "
        f"otherwise.\n"
        f"  - 'chicken/beef stock' → 'or beef stock'\n"
        f"  - '3 sprigs thyme (or 1 tsp dried thyme)' → 'or 1 tsp dried thyme'\n"
        f"  - 'olive oil' → null\n"
        f"DO NOT mark these as alternatives:\n"
        f"  - Optionality markers: '(optional)', 'to taste', 'as needed', "
        f"'(for garnish)' — those are handled separately, not alternatives.\n"
        f"    'Carrots, chopped (optional)' → alternatives_note=null\n"
        f"  - Compound siblings: in 'X and Y' BOTH are required ingredients, "
        f"not alternatives. You're being asked about one component of the "
        f"compound; the other is its own separate ingredient.\n"
        f"    Source 'Salt and pepper to taste', asked about salt → "
        f"alternatives_note=null (pepper is its own ingredient, not an "
        f"alternative to salt).\n\n"
        f"Return ONLY the verified_ingredient tool call."
    )

    user_msg = (
        f"source_line (verbatim from the recipe — GROUND TRUTH): "
        f"{source_line!r}\n"
        f"foundation_food (FDC match — often wrong on specifics): "
        f"{foundation_food!r}\n"
        f"display_name (parser's name field): {display_name!r}\n"
        f"deterministic guess: canonical={suggested_canonical!r}, "
        f"category={suggested_category!r}"
    )

    try:
        resp = client.messages.create(
            model=model,
            max_tokens=200,
            system=system,
            tools=[tool],
            tool_choice={"type": "tool", "name": "verified_ingredient"},
            messages=[{"role": "user", "content": user_msg}],
        )
    except Exception as e:
        log.warning("llm_verify API call failed: %s", e)
        return None

    for block in resp.content:
        if getattr(block, "type", None) == "tool_use" and block.name == "verified_ingredient":
            try:
                out = VerifyOutput.model_validate(block.input)
            except ValidationError as e:
                log.warning("llm_verify schema validation failed: %s", e)
                return None
            if out.category not in VALID_CATEGORIES:
                log.warning("llm_verify returned invalid category %r", out.category)
                return None
            # Ground the LLM answer against the actual source_line. The
            # prompt example wasn't enough to stop Haiku hallucinating
            # 'canned_beans' from '1 Vanilla Bean'; a mechanical check
            # is. If NO substantive word from canonical_name appears in
            # source_line, the LLM has invented a category that isn't
            # grounded in the recipe text — reject and let the caller
            # fall back to the deterministic parser output.
            if not _llm_output_grounded_in_source(out.canonical_name, source_line):
                log.warning(
                    "llm_verify canonical %r not grounded in source_line %r — rejecting",
                    out.canonical_name, source_line,
                )
                return None
            return out
    return None


def _strip_accents(s: str) -> str:
    """jalapeño → jalapeno, pâte → pate. Lets the grounding check
    treat 'jalapeño' (source) and 'jalapeno' (canonical) as overlapping
    — the legit accent-stripping the LLM does shouldn't trip our
    hallucination guard."""
    import unicodedata
    return "".join(
        c for c in unicodedata.normalize("NFKD", s) if not unicodedata.combining(c)
    )


def _llm_output_grounded_in_source(canonical: str, source_line: str | None) -> bool:
    """At least one substantive word (len>2) from canonical_name must
    appear in source_line. This is the mechanical guardrail behind the
    'EVERY word must appear' prompt rule — the rule is too strict to
    enforce (legit case: source 'flour' → canonical='all_purpose_flour'
    where only 'flour' overlaps), so we accept partial grounding but
    reject zero grounding. None source_line → trust (we can't check).

    Both sides are accent-stripped so 'jalapeño' source grounds
    'jalapeno' canonical. Cross-language translation
    ('pignons'→'pine_nuts') still fails grounding — those need manual
    aliases; the false-positive rate is ~3%."""
    if not source_line:
        return True
    src = _strip_accents(source_line.lower())
    words = [_strip_accents(w) for w in canonical.split("_") if len(w) > 2]
    if not words:
        return True
    return any(w in src for w in words)


def _alias_key(foundation_food: str | None, display_name: str) -> str:
    """Key for the IngredientAlias lookup. Prefer foundation_food (FDC
    name is stable across phrasings) when available."""
    return ((foundation_food or display_name) or "").lower().strip()


# Modifier patterns that suggest strangetom may have latched onto the
# wrong noun. "1 egg yolk with a little water" → strangetom returns
# name="water", but the real ingredient is the egg yolk. When source
# contains one of these AND the cached canonical's word appears after
# the modifier (so it's downstream / less important), force a fresh
# LLM call so the cache short-circuit can't cement the wrong subject.
_SUBJECT_MODIFIERS = (" with ", " mixed with ", " plus ", " including ")


def _canonical_validates_source(canonical: str, source_line: str | None) -> bool:
    """True if the cached canonical actually applies to this source_line.

    The cache key is derived from strangetom's FDC match, which can be
    wrong ('5 Cloves' → 'Garlic, raw'; 'Vanilla Bean' → 'Yardlong bean').
    When the cached canonical's primary word doesn't even appear in the
    source line, we can't trust the cache for this row — drop it and
    let LLM verify re-derive from the actual source.
    """
    if not source_line or not canonical:
        return True  # nothing to validate against; trust cache by default
    src = source_line.lower()
    # Split canonical into words; require at least ONE to appear in the
    # source. Handles 'garlic' (single word), 'ground_beef' (either word
    # fine), 'extra_virgin_olive_oil' (any of oil/virgin/extra suffices).
    words = [w for w in canonical.split("_") if len(w) > 2]
    if not words:
        return True
    return any(w in src for w in words)


def _canonical_is_post_modifier(canonical: str, source_line: str | None) -> bool:
    """True if the canonical's primary word appears AFTER a modifier in
    the source_line — i.e. strangetom picked the modifier's noun rather
    than the source's main subject."""
    if not source_line or not canonical:
        return False
    src = source_line.lower()
    # Canonical's "primary word" — the last word in the snake_case key.
    # Heuristic: for "warm_water" / "water" the primary is "water".
    primary = canonical.rsplit("_", 1)[-1]
    for mod in _SUBJECT_MODIFIERS:
        if mod in src:
            before, after = src.split(mod, 1)
            if primary in after and primary not in before:
                return True
    return False


def verify_ingredient(
    pi: ParsedIngredient,
    db: Session,
    *,
    mode: VerifyMode = "first-seen",
) -> ParsedIngredient:
    """Return a possibly-revised ParsedIngredient with category and
    canonical_name refined by the LLM verify pass. Side-effect: writes
    the result back to IngredientAlias (source="llm") so re-imports
    skip the LLM call.

    Honours `mode`:
      off            → returns pi unchanged
      first-seen     → LLM only when category=="Other" or source=="auto"
      every          → LLM on every call (overrides even good guesses)
      audit-only     → returns pi; queues for separate batch audit (TODO)
    """
    key = _alias_key(pi.foundation_food, pi.name)
    if not key or key == "unknown":
        # "unknown" is a poison-pill key: every parser-failed row would
        # collide on it. The first LLM call would see *some* row's
        # source_line and cement a wrong canonical for all subsequent
        # unknowns. Skip entirely — these belong in the review queue.
        return pi

    # Snapshot the original category so we can detect upgrades and
    # re-run unit-preference logic when the category changes.
    original_category = pi.category

    # Cache application happens regardless of mode — pulling a cached
    # answer is free, and mode="off" should mean "don't ALSO call the
    # LLM", not "ignore everything we've already learned".
    existing = db.get(IngredientAlias, key)
    # Guard: if the cached canonical sits after a "with X" / "mixed with X"
    # modifier in the source line, strangetom probably picked the modifier
    # over the subject. Skip the cache AND force LLM call so it can see
    # source_line. Doesn't apply to manual overrides — operator-confirmed.
    # Force a fresh LLM call when the cached answer doesn't pass
    # validation against THIS row's source_line. Two failure modes:
    #   (a) the cached canonical's words don't appear in source_line at
    #       all ('5 Cloves' → cached garlic; no 'garlic' in source) —
    #       strangetom mis-classified; the cache holds someone else's
    #       answer for a colliding alias key.
    #   (b) the cached canonical sits AFTER a 'with X' / 'mixed with X'
    #       modifier in the source — strangetom picked the modifier
    #       noun, not the subject ('egg yolk with a little water' →
    #       cached water).
    # Manual overrides aren't bypassed — those are operator-confirmed.
    force_llm = (
        existing is not None
        and existing.source == "llm"
        and (
            not _canonical_validates_source(existing.canonical_name, pi.source_line)
            or _canonical_is_post_modifier(existing.canonical_name, pi.source_line)
        )
    )
    if not force_llm and existing is not None and existing.source in ("llm", "manual"):
        # Special case: a cached category of "Other" is not useful info
        # (the LLM saw the parser's Other guess and didn't refine it).
        # If the parser has since produced a SPECIFIC category — usually
        # because we added a new CATEGORY_BY_PREFIX entry — let parser
        # win.
        cached_cat = existing.category
        if cached_cat == "Other" and pi.category != "Other":
            cached_cat = pi.category
        update = {
            "canonical_name": existing.canonical_name,
            "category": cached_cat or pi.category,
        }
        if existing.display_name_pretty:
            update["display_name_pretty"] = existing.display_name_pretty
        pi = pi.model_copy(update=update)
        # We have a fully-verified cached answer — no need to call LLM,
        # even in mode="every" (that mode is for first-time enrichment).
        return _reapply_unit_preference(pi, original_category)

    if mode == "off" and not force_llm:
        return pi

    # First-seen gating: only call LLM if our prefix map punted (Other),
    # our canonical came from the deterministic snake_case path, OR the
    # modifier guard above flagged this as a likely misidentification.
    if mode == "first-seen" and not force_llm:
        needs_llm = pi.category == "Other" or (
            existing is not None and existing.source == "auto"
        )
        if not needs_llm:
            return pi
    elif mode == "audit-only":
        # TODO: queue (pi, key) for batch audit. For now, no-op.
        return pi
    # mode == "every" falls through to LLM call

    verified = _llm_verify(
        foundation_food=pi.foundation_food,
        display_name=pi.name,
        suggested_canonical=pi.canonical_name,
        suggested_category=pi.category,
        source_line=pi.source_line,
    )
    if verified is None:
        return pi

    # Cache the verified answer — UNLESS force_llm fired. When force_llm
    # is true the cached alias is correct in general (e.g. "water" really
    # is water) but wrong for THIS specific row's source_line. Overwriting
    # the alias would corrupt every legitimate use of it. Apply the LLM
    # answer to this pi only.
    if not force_llm:
        if existing is None:
            db.add(
                IngredientAlias(
                    display_name=key,
                    canonical_name=verified.canonical_name,
                    category=verified.category,
                    display_name_pretty=verified.display_name_pretty,
                    confidence=0.9,
                    source="llm",
                )
            )
        else:
            existing.canonical_name = verified.canonical_name
            existing.category = verified.category
            existing.display_name_pretty = verified.display_name_pretty
            existing.confidence = 0.9
            existing.source = "llm"
            db.add(existing)
        db.flush()

    pi = pi.model_copy(
        update={
            "canonical_name": verified.canonical_name,
            "category": verified.category,
            "display_name_pretty": verified.display_name_pretty,
            # alternatives_note is per-source-line, not cached — only
            # surfaces when a fresh LLM call happened on this row.
            "alternatives_note": verified.alternatives_note,
        }
    )
    return _reapply_unit_preference(pi, original_category)
