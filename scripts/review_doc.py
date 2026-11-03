#!/usr/bin/env python3
"""Generate docs/REVIEW_QUEUE.md from the live Mise database.

Surfaces anything the operator should manually eyeball after an
import / period of use:

  - Ingredients the parser couldn't identify ("unknown" / category=Other)
  - Aliases still source="auto" (the LLM verify pass didn't refine them)
  - SyncRun entries with needs_review > 0
  - Recipes with suspiciously few ingredients (likely parsing failure)

Run inside the container (the DB path matches the runtime):
  docker exec mise uv run python /app/scripts/review_doc.py

Or from the host pointing at a copy of the DB.

The output lands at /docs/REVIEW_QUEUE.md (configurable via env), which
render-portal.py auto-tiles on the private-portal Plans pane.
"""

from __future__ import annotations

import os
import sys
from collections import defaultdict
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlmodel import Session, select

from mise.db import engine
from mise.models import (
    Ingredient,
    IngredientAlias,
    IngredientSection,
    Recipe,
    SyncRun,
)

OUTPUT_PATH = os.environ.get(
    "REVIEW_DOC_PATH",
    "/app/docs/REVIEW_QUEUE.md",
)


def main() -> int:
    with Session(engine()) as db:
        md = _build_markdown(db)
    Path(OUTPUT_PATH).parent.mkdir(parents=True, exist_ok=True)
    Path(OUTPUT_PATH).write_text(md)
    print(f"wrote {OUTPUT_PATH} ({len(md)} bytes)")
    return 0


def _build_markdown(db: Session) -> str:
    lines: list[str] = []
    a = lines.append

    a("# Review queue")
    a("")
    a(f"_Generated {datetime.now(UTC).isoformat()}. Re-run "
      "`scripts/review_doc.py` after each import to refresh._")
    a("")
    a("This page lists anything the operator should manually eyeball — "
      "ingredients the parser couldn't identify, aliases the LLM verify "
      "didn't refine, recipes with suspicious parse output. Empty "
      "sections are good news.")
    a("")

    # --- summary -----------------------------------------------------
    recipes = db.exec(select(Recipe).where(Recipe.archived == False)).all()  # noqa: E712
    aliases = db.exec(select(IngredientAlias)).all()
    syncs = db.exec(select(SyncRun).order_by(SyncRun.started_at.desc()).limit(10)).all()
    ingredients = db.exec(select(Ingredient)).all()

    a("## Summary")
    a("")
    a(f"- Active recipes: **{len(recipes)}**")
    a(f"- Ingredient rows: **{len(ingredients)}**")
    a(f"- Aliases (LLM-verified): "
      f"**{sum(1 for x in aliases if x.source == 'llm')} / {len(aliases)}**")
    a(f"- Aliases (manual overrides): "
      f"**{sum(1 for x in aliases if x.source == 'manual')}**")
    a(f"- Aliases (auto-only, deterministic snake_case): "
      f"**{sum(1 for x in aliases if x.source == 'auto')}**")
    a(f"- Recent syncs: **{len(syncs)}**")
    a("")

    # --- unknown ingredients ----------------------------------------
    unknowns_by_recipe: dict[str, list[Ingredient]] = defaultdict(list)
    for ing in ingredients:
        if ing.canonical_name == "unknown" or ing.display_name == "unknown":
            section = db.get(IngredientSection, ing.section_id)
            recipe = db.get(Recipe, section.recipe_id) if section else None
            label = recipe.name_english if recipe else "(orphan)"
            unknowns_by_recipe[label].append(ing)

    a("## Ingredients the parser couldn't identify")
    a("")
    if not unknowns_by_recipe:
        a("_None._ ✓")
    else:
        a(f"**{sum(len(v) for v in unknowns_by_recipe.values())}** ingredient "
          f"row(s) across **{len(unknowns_by_recipe)}** recipe(s) came back "
          f"as `unknown`. These need either a recipe-level edit (typo in "
          f"the source blob?) or a parser improvement.")
        a("")
        # Per-recipe, surface the raw_text — the parse-failure guard sets
        # display_name="unknown" but keeps the input string in raw_text so
        # the operator can see what got filtered ("Turkey, dark meat, ...
        # raw" being a clear "this is a USDA name pasted as data" signal).
        for name, items in sorted(unknowns_by_recipe.items()):
            a(f"### {name} ({len(items)})")
            a("")
            for ing in items:
                raw = (ing.raw_text or "").replace("`", "")
                marker = "(blank/empty)" if not raw or raw == "unknown" else f"`{raw}`"
                a(f"  - {marker}")
            a("")
    a("")

    # --- category = Other (excluding unknowns we already showed) ----
    others = [
        ing for ing in ingredients
        if ing.category == "Other" and ing.canonical_name != "unknown"
    ]
    a("## Ingredients with category = Other")
    a("")
    if not others:
        a("_None._ ✓")
    else:
        a(f"**{len(others)}** ingredient(s) didn't get a grocery section "
          f"assigned. Add the corresponding alias rows in IngredientAlias "
          f"with the right category, OR extend `CATEGORY_BY_PREFIX` in "
          f"`mise/services/ingredient_parser.py`.")
        a("")
        a("| Display name | Canonical | foundation_food |")
        a("|---|---|---|")
        for ing in others[:30]:
            a(f"| {ing.display_name} | {ing.canonical_name} | "
              f"{_foundation_for(ing.canonical_name, aliases) or '—'} |")
        if len(others) > 30:
            a(f"")
            a(f"_…and {len(others) - 30} more._")
    a("")

    # --- zero-quantity ingredients ----------------------------------
    # Quantity-extraction failures: parser matched a canonical (often a
    # USDA foundation_food name) but lost the qty. They render as "0 ea
    # Turkey, dark meat..." in the preview, which looks broken — surface
    # them so they can be fixed before the household sees them. Excludes rows
    # already in the unknowns table above.
    zero_qty = [
        ing for ing in ingredients
        if ing.qty == 0
        and ing.canonical_name != "unknown"
        and ing.display_name != "unknown"
    ]
    a("## Ingredients with quantity = 0")
    a("")
    if not zero_qty:
        a("_None._ ✓")
    else:
        a(f"**{len(zero_qty)}** ingredient row(s) have `qty == 0`. Legit "
          f"\"to taste\" entries are marked optional; anything not optional "
          f"is a parser failure where the canonical matched but the "
          f"quantity didn't extract.")
        a("")
        a("| Recipe | Display name | Raw text | Optional? |")
        a("|---|---|---|---|")
        for ing in zero_qty[:50]:
            section = db.get(IngredientSection, ing.section_id)
            recipe = db.get(Recipe, section.recipe_id) if section else None
            rname = recipe.name_english if recipe else "(orphan)"
            raw = (ing.raw_text or "").replace("|", "\\|")[:60]
            opt = "yes" if ing.optional else "**NO**"
            a(f"| {rname} | {ing.display_name} | `{raw}` | {opt} |")
        if len(zero_qty) > 50:
            a("")
            a(f"_…and {len(zero_qty) - 50} more._")
    a("")

    # --- auto-only aliases ------------------------------------------
    auto_aliases = [a for a in aliases if a.source == "auto"]
    a("## Aliases not LLM-verified (source=auto)")
    a("")
    if not auto_aliases:
        a("_None._ ✓")
    else:
        a(f"**{len(auto_aliases)}** alias(es) are still deterministic "
          f"snake_case fallbacks. Re-run an import with `llm_verify=every` "
          f"to refine them, or edit manually via direct DB write.")
        a("")
        a("| display_name | canonical_name | confidence |")
        a("|---|---|---|")
        for al in auto_aliases[:30]:
            a(f"| {al.display_name} | {al.canonical_name} | {al.confidence} |")
        if len(auto_aliases) > 30:
            a(f"")
            a(f"_…and {len(auto_aliases) - 30} more._")
    a("")

    # --- sync runs with needs_review --------------------------------
    flagged = [s for s in syncs if s.needs_review]
    a("## Recent syncs with `needs_review`")
    a("")
    if not flagged:
        a("_None._ ✓")
    else:
        for sr in flagged:
            a(f"### SyncRun #{sr.id} — {sr.status}")
            a(f"")
            a(f"- Started: {sr.started_at}")
            a(f"- Counts: {dict(sr.counts or {})}")
            a(f"- Review items:")
            for entry in (sr.needs_review or [])[:20]:
                a(f"  - `{entry}`")
            a("")
    a("")

    # --- thin recipes ------------------------------------------------
    a("## Recipes with suspiciously few ingredients")
    a("")
    counts_by_recipe = defaultdict(int)
    for ing in ingredients:
        section = db.get(IngredientSection, ing.section_id)
        if section:
            counts_by_recipe[section.recipe_id] += 1
    thin = [
        (db.get(Recipe, rid), n)
        for rid, n in counts_by_recipe.items()
        if n <= 2
    ]
    if not thin:
        a("_None._ ✓")
    else:
        a(f"Recipes with ≤2 ingredients — possible parser failure on a "
          f"blob that wasn't an actual short recipe.")
        a("")
        a("| Recipe | Ingredients |")
        a("|---|---|")
        for r, n in sorted(thin, key=lambda x: x[1]):
            if r:
                a(f"| {r.name_english} | {n} |")
    a("")

    return "\n".join(lines) + "\n"


def _guess_cause(items: list[Ingredient]) -> str:
    """Heuristic: if all rows are the same, probably a literal 'unknown'
    section in the source. If varied, parser dropped multiple lines."""
    if len(items) == 1:
        return "single blank/empty line"
    if len(items) > 5:
        return "likely the entire ingredients blob failed to parse"
    return "multiple lines couldn't be identified"


def _foundation_for(canonical: str, aliases: list[IngredientAlias]) -> str | None:
    for a in aliases:
        if a.canonical_name == canonical:
            return a.display_name
    return None


if __name__ == "__main__":
    sys.exit(main())
