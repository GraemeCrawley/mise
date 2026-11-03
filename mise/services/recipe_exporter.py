"""Recipe JSON exporter — durable redundancy layer.

After every recipe write (importer, POST /recipes, future PATCH), this
service dumps the recipe + its sections + ingredients + steps as a
single JSON file under RECIPES_EXPORT_PATH.

The directory is intended to be a separate git repo (bind-mounted into
the container) so the recipe library survives a SQLite-volume loss.
The auto-commit-and-push is deferred to a host-side cron (or a Phase 2
inside-container subprocess once we have ssh-key plumbing); for v0.1
this service just writes the files.

File naming: `<recipe_id>__<slug>.json` — id is the durable key, slug
gives humans a way to scan the directory.
"""

from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from sqlmodel import Session, select

from mise.models import (
    DoAheadTask,
    Ingredient,
    IngredientSection,
    Recipe,
    Step,
    StepSection,
)

log = logging.getLogger("mise.exporter")

DEFAULT_EXPORT_PATH = "/data/recipes-export"


def _export_dir() -> Path:
    p = Path(os.environ.get("RECIPES_EXPORT_PATH", DEFAULT_EXPORT_PATH))
    p.mkdir(parents=True, exist_ok=True)
    return p


def _slug(name: str) -> str:
    s = name.lower().strip()
    s = re.sub(r"[^a-z0-9]+", "-", s).strip("-")
    return s or "recipe"


@dataclass
class ExportResult:
    path: Path
    bytes_written: int


def export_recipe(recipe_id: int, db: Session) -> ExportResult:
    """Serialise a recipe (with all its child rows) to one JSON file.
    Overwrites if a previous version exists for the same id (slug may
    change when name_english changes; we drop the stale-slug file as
    cleanup)."""
    recipe = db.get(Recipe, recipe_id)
    if recipe is None:
        raise FileNotFoundError(f"Recipe {recipe_id} not found")

    payload = _serialise_recipe(recipe, db)
    slug = _slug(recipe.name_english or recipe.name_original)
    out = _export_dir() / f"{recipe.id}__{slug}.json"

    # If a previous slug for this id exists, drop it to avoid orphans
    for existing in _export_dir().glob(f"{recipe.id}__*.json"):
        if existing != out:
            try:
                existing.unlink()
            except OSError as e:
                log.warning("failed to drop stale export %s: %s", existing, e)

    data = json.dumps(payload, indent=2, default=str).encode("utf-8")
    out.write_bytes(data)
    return ExportResult(path=out, bytes_written=len(data))


def export_all(db: Session) -> list[ExportResult]:
    """Bulk export — useful right after the bootstrap import. Writes
    one JSON per active recipe; archived recipes are skipped."""
    results: list[ExportResult] = []
    for r in db.exec(select(Recipe).where(Recipe.archived == False)).all():  # noqa: E712
        results.append(export_recipe(r.id, db))
    return results


# --- serialisation -------------------------------------------------------


def _serialise_recipe(recipe: Recipe, db: Session) -> dict:
    ing_sections = []
    for s in db.exec(
        select(IngredientSection)
        .where(IngredientSection.recipe_id == recipe.id)
        .order_by(IngredientSection.order)
    ).all():
        ing_sections.append(
            {
                "name": s.name,
                "order": s.order,
                "ingredients": [
                    {
                        "order": i.order,
                        "raw_text": i.raw_text,
                        "display_name": i.display_name,
                        "canonical_name": i.canonical_name,
                        "prep_note": i.prep_note,
                        "size_descriptor": i.size_descriptor,
                        "qty": i.qty,
                        "unit": i.unit,
                        "original_qty": i.original_qty,
                        "original_unit": i.original_unit,
                        "package_qty": i.package_qty,
                        "package_unit": i.package_unit,
                        "category": i.category,
                        "optional": i.optional,
                    }
                    for i in db.exec(
                        select(Ingredient)
                        .where(Ingredient.section_id == s.id)
                        .order_by(Ingredient.order)
                    ).all()
                ],
            }
        )

    step_sections = []
    for s in db.exec(
        select(StepSection)
        .where(StepSection.recipe_id == recipe.id)
        .order_by(StepSection.order)
    ).all():
        step_sections.append(
            {
                "name": s.name,
                "order": s.order,
                "steps": [
                    {"order": st.order, "text": st.text}
                    for st in db.exec(
                        select(Step)
                        .where(Step.section_id == s.id)
                        .order_by(Step.order)
                    ).all()
                ],
            }
        )

    do_ahead = [
        {
            "order": d.order,
            "lead_time": d.lead_time,
            "task_type": d.task_type,
            "note": d.note,
        }
        for d in db.exec(
            select(DoAheadTask)
            .where(DoAheadTask.recipe_id == recipe.id)
            .order_by(DoAheadTask.order)
        ).all()
    ]

    return {
        "schema_version": "v1",
        "exported_at": datetime.now(UTC).isoformat(),
        "id": recipe.id,
        "name_original": recipe.name_original,
        "name_romanized": recipe.name_romanized,
        "name_english": recipe.name_english,
        "language": recipe.language,
        "servings": recipe.servings,
        "prep_minutes": recipe.prep_minutes,
        "cook_minutes": recipe.cook_minutes,
        "total_minutes": recipe.total_minutes,
        "oven_temp_f": recipe.oven_temp_f,
        "emoji": recipe.emoji,
        "suggested_meals": list(recipe.suggested_meals or []),
        "tags": list(recipe.tags or []),
        "source_url": recipe.source_url,
        "notes_markdown": recipe.notes_markdown,
        "archived": recipe.archived,
        "archived_reason": recipe.archived_reason,
        "source_row_hash": recipe.source_row_hash,
        "ingredient_sections": ing_sections,
        "step_sections": step_sections,
        "do_ahead_tasks": do_ahead,
    }
