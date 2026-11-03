#!/usr/bin/env python3
"""One-shot humanise pass for ugly USDA-flavoured display names.

The xlsx contains rows like "Garlic, raw", "Spices, pepper, black",
"Eggs, Grade A, Large, egg whole" — FDC reference names that snuck
into the ingredient column as data. They map to canonical and
category fine but read as USDA jargon in the preview.

This script:
  1. Collects every distinct display_name currently in Ingredient
     (lowercased + stripped — same key as IngredientAlias).
  2. For each that doesn't yet have a display_name_pretty in
     IngredientAlias, asks Claude Haiku for a natural English label.
  3. Caches the answer in IngredientAlias.display_name_pretty.
  4. Updates every Ingredient.display_name to the pretty version.

Idempotent — re-running only humanises new aliases. Skips
display_name == "unknown" (parse-failure rows are intentional sinks).

  docker exec mise uv run python /app/scripts/humanize_display_names.py
"""

from __future__ import annotations

import logging
import os
import sys
from collections import Counter

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import anthropic
from pydantic import BaseModel, Field, ValidationError
from sqlmodel import Session, select

from mise.db import engine
from mise.models import Ingredient, IngredientAlias

log = logging.getLogger("mise.humanize")
logging.basicConfig(level=logging.INFO, format="%(message)s")

MODEL = "claude-haiku-4-5-20251001"


class HumaniseOutput(BaseModel):
    display_name_pretty: str = Field(
        description="Natural English label. MUST contain no commas. MUST be "
                    "shorter than input. Drop ', raw', ', fresh', ', cooked', "
                    "', NFS', ', Grade A', ', Large', 'Spices,', 'Beverages,', "
                    "'unenriched', 'unbleached'. Move adjectives BEFORE the "
                    "noun. Examples — REQUIRED format: "
                    "'Garlic, raw' -> 'garlic'; "
                    "'Spices, pepper, black' -> 'black pepper'; "
                    "'Eggs, Grade A, Large, egg whole' -> 'large eggs'; "
                    "'Beef, ground, raw' -> 'ground beef'; "
                    "'Beef, steak, chuck' -> 'chuck steak'; "
                    "'Wine, red' -> 'red wine'; "
                    "'Cheese, cream' -> 'cream cheese'; "
                    "'Thyme, fresh' -> 'fresh thyme'."
    )


def humanise_one(client: anthropic.Anthropic, raw: str) -> str | None:
    tool = {
        "name": "humanise",
        "description": "Return the natural English name a home cook would use.",
        "input_schema": HumaniseOutput.model_json_schema(),
    }
    system = (
        "Rewrite USDA / FDC-style ingredient names into the natural "
        "English label a home cook would use on a recipe card or "
        "shopping list. MUST drop USDA qualifiers AND commas. "
        "Adjectives go BEFORE the noun. Lowercase unless proper noun. "
        "Singular for individual items, plural only for things bought "
        "as a count ('eggs', 'limes')."
    )
    try:
        resp = client.messages.create(
            model=MODEL,
            max_tokens=100,
            system=system,
            tools=[tool],
            tool_choice={"type": "tool", "name": "humanise"},
            messages=[{"role": "user", "content": raw}],
        )
    except Exception as e:
        log.warning("API call failed on %r: %s", raw, e)
        return None
    for block in resp.content:
        if getattr(block, "type", None) == "tool_use" and block.name == "humanise":
            try:
                out = HumaniseOutput.model_validate(block.input)
            except ValidationError as e:
                log.warning("schema fail on %r: %s", raw, e)
                return None
            return out.display_name_pretty.strip()
    return None


def main() -> int:
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        print("ANTHROPIC_API_KEY not set", file=sys.stderr)
        return 1
    client = anthropic.Anthropic(api_key=api_key)

    with Session(engine()) as db:
        # Step 1: collect distinct display names currently in Ingredient.
        all_names = {
            ing.display_name for ing in db.exec(select(Ingredient)).all()
            if ing.display_name and ing.display_name != "unknown"
        }
        log.info("found %d distinct display names", len(all_names))

        # Step 2: humanise the ones not yet cached. Key by lowercased
        # name to match IngredientAlias PK convention.
        cached: dict[str, str] = {}
        to_humanise: list[str] = []
        for name in all_names:
            key = name.lower().strip()
            alias = db.get(IngredientAlias, key)
            if alias and alias.display_name_pretty:
                cached[key] = alias.display_name_pretty
            else:
                to_humanise.append(name)

        log.info("%d already humanised; %d to call LLM for", len(cached), len(to_humanise))

        for i, name in enumerate(to_humanise, start=1):
            pretty = humanise_one(client, name)
            if not pretty:
                continue
            key = name.lower().strip()
            alias = db.get(IngredientAlias, key)
            if alias is None:
                alias = IngredientAlias(
                    display_name=key,
                    canonical_name=key.replace(", ", "_").replace(" ", "_"),
                    confidence=0.5,
                    source="auto",
                )
            alias.display_name_pretty = pretty
            db.add(alias)
            cached[key] = pretty
            if i % 20 == 0:
                db.commit()
                log.info("  ...%d/%d", i, len(to_humanise))
        db.commit()
        log.info("humanise pass done")

        # Step 3: backfill Ingredient.display_name from the cache.
        ings = db.exec(select(Ingredient)).all()
        updated = Counter()
        for ing in ings:
            if not ing.display_name or ing.display_name == "unknown":
                continue
            key = ing.display_name.lower().strip()
            pretty = cached.get(key)
            if pretty and pretty != ing.display_name:
                updated[(ing.display_name, pretty)] += 1
                ing.display_name = pretty
                db.add(ing)
        db.commit()

    log.info("updated %d Ingredient.display_name rows", sum(updated.values()))
    log.info("top 10 renames:")
    for (old, new), n in updated.most_common(10):
        log.info(f"  {n:3} x  {old!r:50} -> {new!r}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
