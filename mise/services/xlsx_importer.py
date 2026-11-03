"""xlsx import — one-shot bootstrap of the legacy recipe library.

NOT an ongoing workflow. After the first import completes, the xlsx
becomes archive material under legacy/sheet-exports/ and recipes are
added/edited through Mise (Phase 1 form + POST /recipes). The xlsx
importer's idempotency / row-hash logic is a defensive safety net for
the bootstrap run — never a re-import-and-sync pattern.

Legacy column layout (recovered from legacy/apps-script/README.md):

  | col | letter | field            |
  |-----|--------|------------------|
  | 1   | A      | trigger checkbox (ignored)               |
  | 2   | B      | english name → Recipe.name_english       |
  | 3   | C      | original name → Recipe.name_original     |
  | 4   | D      | language → Recipe.language               |
  | 5   | E      | prep_minutes → Recipe.prep_minutes       |
  | 6   | F      | cook_minutes → Recipe.cook_minutes       |
  | 7   | G      | total_minutes → Recipe.total_minutes     |
  | 8   | H      | oven_temp_f → Recipe.oven_temp_f         |
  | 9   | I      | servings → Recipe.servings               |
  | 10  | J      | ingredients (text blob) → parsed         |
  | 11  | K      | instructions (text blob) → parsed        |
  | 12  | L      | source_url → Recipe.source_url (link to recipe) |
  | 13  | M      | generated Doc URL (ignored)              |
"""

from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal

from openpyxl import load_workbook
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, select

from mise.models import (
    Ingredient,
    IngredientSection,
    Recipe,
    Step,
    StepSection,
    SyncRun,
)
from mise.services.ingredient_parser import parse_blob
from mise.services.instruction_parser import parse_instructions
from mise.services.llm_verify import verify_ingredient
from mise.services.recipe_exporter import export_recipe

log = logging.getLogger("mise.importer")


# Unit-word synonym groups — used by `_strip_unit_word_from_name` so a
# stored name like "garlic cloves" with unit="clove" renders as just
# "garlic". Each group's key is the canonical unit; the set is every
# spelling/plural the parser might have left in the display_name.
_UNIT_WORD_GROUPS: dict[str, set[str]] = {
    "tsp": {"tsp", "teaspoon", "teaspoons"},
    "tbsp": {"tbsp", "tablespoon", "tablespoons"},
    "cup": {"cup", "cups"},
    "ml": {"ml", "milliliter", "milliliters", "millilitre", "millilitres"},
    "l": {"l", "liter", "liters", "litre", "litres"},
    "floz": {"floz"},
    "g": {"g", "gram", "grams"},
    "kg": {"kg", "kilogram", "kilograms"},
    "oz": {"oz", "ounce", "ounces"},
    "lb": {"lb", "lbs", "pound", "pounds"},
    "ea": {"ea", "each"},
    "piece": {"piece", "pieces"},
    "slice": {"slice", "slices"},
    "scoop": {"scoop", "scoops"},
    "pinch": {"pinch", "pinches"},
    "dash": {"dash", "dashes"},
    "clove": {"clove", "cloves"},
    "sprig": {"sprig", "sprigs"},
    "stalk": {"stalk", "stalks"},
    "rib": {"rib", "ribs"},
    "stick": {"stick", "sticks"},
    "head": {"head", "heads"},
    "bunch": {"bunch", "bunches"},
    "pod": {"pod", "pods"},
    # Container units include the past-participle adjective form
    # ("canned tomatoes" with unit="can" → "tomatoes"). We deliberately
    # don't include "-ed" forms for cuts/tools (sliced, chopped, diced)
    # since those are legitimate prep notes, not redundancy.
    "can": {"can", "cans", "canned"},
    "jar": {"jar", "jars", "jarred"},
    "bottle": {"bottle", "bottles", "bottled"},
    "box": {"box", "boxes", "boxed"},
    "bag": {"bag", "bags", "bagged"},
    "package": {"package", "packages", "packaged", "pkg", "pkgs"},
    "pkg": {"pkg", "pkgs", "package", "packages", "packaged"},
}


def _strip_unit_word_from_name(name: str | None, unit: str | None) -> str:
    """Strip whole-word matches of the unit (and its synonyms) from the
    display name. "garlic cloves" with unit="clove" → "garlic".

    Whole-word match (so "cup" doesn't shave letters off "cupcake").
    If the strip would empty the name, returns the original — better to
    show a redundant word than nothing."""
    if not name or not unit:
        return name or ""
    syns = _UNIT_WORD_GROUPS.get(unit.lower().strip(), {unit.lower().strip()})
    if not syns:
        return name
    words = name.split()
    kept = [w for w in words if w.lower().strip(",.;:()") not in syns]
    cleaned = " ".join(kept).strip()
    return cleaned if cleaned else name

LLMVerifyMode = Literal["off", "first-seen", "every", "audit-only"]


# --- xlsx row read --------------------------------------------------------


@dataclass
class XlsxRow:
    """One row from the legacy sheet, after column-letter mapping but
    before parsing. Sheet index preserved for error reporting."""

    row_index: int
    name_english: str | None
    name_original: str | None
    language: str | None
    prep_minutes: int | None
    cook_minutes: int | None
    total_minutes: int | None
    oven_temp_f: int | None
    servings: int | None
    ingredients_blob: str
    instructions_blob: str
    source_url: str | None = None


def _cell(row: tuple, idx: int) -> object | None:
    """Safe cell access — sheet rows may be shorter than expected."""
    return row[idx] if idx < len(row) else None


def _as_int(v: object) -> int | None:
    if v is None or v == "":
        return None
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def _as_str(v: object) -> str:
    if v is None:
        return ""
    return str(v).strip()


def read_xlsx_rows(path: str, sheet_name: str = "recipes") -> list[XlsxRow]:
    """Read every populated row of the recipes sheet. Skips the header
    (row 1) and rows with no english name (defensive — some legacy
    sheets have intermediate blank rows)."""
    wb = load_workbook(path, read_only=True, data_only=True)
    if sheet_name not in wb.sheetnames:
        raise FileNotFoundError(
            f"sheet {sheet_name!r} not found; available: {wb.sheetnames}"
        )
    ws = wb[sheet_name]
    out: list[XlsxRow] = []
    for idx, raw_row in enumerate(ws.iter_rows(min_row=2, values_only=True), start=2):
        name_english = _as_str(_cell(raw_row, 1))  # col B (0-indexed 1)
        if not name_english:
            continue
        out.append(
            XlsxRow(
                row_index=idx,
                name_english=name_english,
                name_original=_as_str(_cell(raw_row, 2)) or None,
                language=_as_str(_cell(raw_row, 3)) or None,
                prep_minutes=_as_int(_cell(raw_row, 4)),
                cook_minutes=_as_int(_cell(raw_row, 5)),
                total_minutes=_as_int(_cell(raw_row, 6)),
                oven_temp_f=_as_int(_cell(raw_row, 7)),
                servings=_as_int(_cell(raw_row, 8)),
                ingredients_blob=_as_str(_cell(raw_row, 9)),
                instructions_blob=_as_str(_cell(raw_row, 10)),
                source_url=_as_str(_cell(raw_row, 11)) or None,
            )
        )
    return out


# --- row hashing ----------------------------------------------------------


def compute_row_hash(row: XlsxRow) -> str:
    """sha256 of the normalised row payload. Used for idempotent
    detection of "this exact row has already been imported". Field
    order is fixed; whitespace collapsed."""
    payload = "|".join(
        [
            (row.name_english or "").strip(),
            (row.name_original or "").strip(),
            (row.language or "").strip(),
            str(row.prep_minutes or ""),
            str(row.cook_minutes or ""),
            str(row.total_minutes or ""),
            str(row.oven_temp_f or ""),
            str(row.servings or ""),
            " ".join((row.ingredients_blob or "").split()),
            " ".join((row.instructions_blob or "").split()),
        ]
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


# --- importer orchestrator ------------------------------------------------


@dataclass
class ImportResult:
    sync_run_id: int
    rows_total: int
    rows_new: int
    rows_changed: int
    rows_unchanged: int
    rows_failed: int
    needs_review: list[str]
    dry_run: bool


def upsert_recipe_from_blobs(
    db: Session,
    *,
    name_english: str,
    name_original: str | None,
    language: str | None,
    prep_minutes: int | None,
    cook_minutes: int | None,
    total_minutes: int | None,
    oven_temp_f: int | None,
    servings: int,
    ingredients_blob: str,
    instructions_blob: str,
    source_url: str | None = None,
    source_row_hash: str | None = None,
    llm_verify_mode: LLMVerifyMode = "off",
    needs_review: list[str] | None = None,
    keep_failed_lines: bool = False,
) -> Recipe:
    """Shared recipe upsert from raw blobs. Used by:
      - xlsx_importer (legacy-row shape)
      - POST /recipes (Phase 0 add path)
      - Phase 1 PATCH (future)

    Runs the parser, applies LLM verify, persists Recipe + sections.
    Returns the (possibly newly-created) Recipe. Caller manages the
    transaction; this function only flushes."""
    if needs_review is None:
        needs_review = []

    existing = db.exec(
        select(Recipe).where(Recipe.name_english == name_english)
    ).first()

    row = XlsxRow(
        row_index=0,
        name_english=name_english,
        name_original=name_original,
        language=language,
        prep_minutes=prep_minutes,
        cook_minutes=cook_minutes,
        total_minutes=total_minutes,
        oven_temp_f=oven_temp_f,
        servings=servings,
        ingredients_blob=ingredients_blob,
        instructions_blob=instructions_blob,
        source_url=source_url,
    )
    recipe = _upsert_recipe(db, row, source_row_hash or "", existing)
    ingredient_sections = parse_blob(ingredients_blob, db)
    _write_ingredients(
        db, recipe, ingredient_sections, needs_review,
        llm_verify_mode=llm_verify_mode, keep_failed_lines=keep_failed_lines,
    )
    step_sections = parse_instructions(instructions_blob)
    _write_steps(db, recipe, step_sections)
    return recipe


def _upsert_recipe(
    db: Session, row: XlsxRow, row_hash: str, existing: Recipe | None
) -> Recipe:
    """Upsert one recipe row + replace its ingredient/step sections."""
    if existing is None:
        recipe = Recipe(
            household_id=1,
            name_original=row.name_original or row.name_english,
            name_english=row.name_english,
            language=row.language,
            servings=row.servings or 2,
            prep_minutes=row.prep_minutes,
            cook_minutes=row.cook_minutes,
            total_minutes=row.total_minutes,
            oven_temp_f=row.oven_temp_f,
            source_url=row.source_url,
            source_row_hash=row_hash,
        )
        db.add(recipe)
        db.flush()  # need recipe.id for the child rows
    else:
        # Update the existing row in place and clear its sections
        existing.name_original = row.name_original or row.name_english
        existing.name_english = row.name_english
        existing.language = row.language
        existing.servings = row.servings or 2
        existing.prep_minutes = row.prep_minutes
        existing.cook_minutes = row.cook_minutes
        existing.total_minutes = row.total_minutes
        existing.oven_temp_f = row.oven_temp_f
        # Only overwrite source_url when the xlsx has one — preserves
        # any manual override the user made via the UI for an existing
        # recipe with no link in the xlsx.
        if row.source_url:
            existing.source_url = row.source_url
        existing.source_row_hash = row_hash
        existing.updated_at = datetime.now(UTC)
        existing.archived = False  # re-import un-archives
        # Re-import means the source changed — drop "reviewed" so the
        # row re-enters the review queue.
        existing.reviewed_at = None
        # Wipe child rows — simpler than reconciling sections
        _wipe_recipe_children(db, existing.id)
        recipe = existing
    return recipe


def _wipe_recipe_children(db: Session, recipe_id: int) -> None:
    """Delete ingredient_sections (cascades ingredients) and step_sections
    (cascades steps) for a recipe. Used on re-import."""
    for sect in db.exec(
        select(IngredientSection).where(IngredientSection.recipe_id == recipe_id)
    ).all():
        for ing in db.exec(
            select(Ingredient).where(Ingredient.section_id == sect.id)
        ).all():
            db.delete(ing)
        db.delete(sect)
    for ssec in db.exec(
        select(StepSection).where(StepSection.recipe_id == recipe_id)
    ).all():
        for step in db.exec(select(Step).where(Step.section_id == ssec.id)).all():
            db.delete(step)
        db.delete(ssec)
    db.flush()


def _write_ingredients(
    db: Session,
    recipe: Recipe,
    parsed_sections,
    needs_review: list[str],
    llm_verify_mode: LLMVerifyMode = "off",
    keep_failed_lines: bool = False,
) -> None:
    """Persist parsed ingredient sections + lines. Per-line failures
    from the parser go to needs_review (NOT raised). The LLM verify
    pass runs per-ingredient when mode != "off" and may refine
    canonical_name + category."""
    for sect_order, parsed in enumerate(parsed_sections):
        section = IngredientSection(
            recipe_id=recipe.id, name=parsed.name, order=sect_order
        )
        db.add(section)
        db.flush()
        for ing_order, pi in enumerate(parsed.lines):
            # LLM verify (no-op when mode="off" or when the alias is
            # already source="llm"/"manual" for this foundation_food).
            pi = verify_ingredient(pi, db, mode=llm_verify_mode)

            # parse_failed rows: keep the parsed name in raw_text for the
            # review queue, but show "unknown" in the UI + don't let it
            # aggregate (preview filters on display_name=="unknown" and
            # the shopping aggregator keys on canonical_name).
            #
            # Otherwise prefer the LLM-humanised pretty name from the
            # IngredientAlias cache when present — "ground beef" over
            # "Beef, ground, raw".
            if pi.parse_failed:
                display_name = "unknown"
                canonical_name = "unknown"
            else:
                display_name = pi.display_name_pretty or pi.name
                display_name = _strip_unit_word_from_name(display_name, pi.unit)
                canonical_name = pi.canonical_name

            db.add(
                Ingredient(
                    section_id=section.id,
                    order=ing_order,
                    raw_text=pi.name,
                    source_line=pi.source_line,
                    alternatives_note=pi.alternatives_note,
                    display_name=display_name,
                    prep_note=pi.prep_note,
                    size_descriptor=pi.size_descriptor,
                    canonical_name=canonical_name,
                    qty=pi.qty,
                    unit=pi.unit,
                    original_qty=pi.original_qty,
                    original_unit=pi.original_unit,
                    package_qty=pi.package_qty,
                    package_unit=pi.package_unit,
                    category=pi.category,
                    optional=pi.optional,
                )
            )
        for ing_order_offset, (failed_raw, err) in enumerate(parsed.failed):
            needs_review.append(
                f"recipe={recipe.name_english!r} section={parsed.name!r} line={failed_raw!r} err={err}"
            )
            if keep_failed_lines:
                # Draft path: never drop a source line. Persist it as an
                # 'unknown' row (same shape as parse_failed rows) so the
                # draft screen can flag it amber for hand-fixing.
                db.add(
                    Ingredient(
                        section_id=section.id,
                        order=len(parsed.lines) + 1000 + ing_order_offset,
                        raw_text=failed_raw,
                        source_line=failed_raw,
                        display_name="unknown",
                        canonical_name="unknown",
                        qty=0.0,
                        unit="ea",
                        category="Other",
                    )
                )


def _write_steps(db: Session, recipe: Recipe, parsed_sections) -> None:
    for sect_order, parsed in enumerate(parsed_sections):
        section = StepSection(recipe_id=recipe.id, name=parsed.name, order=sect_order)
        db.add(section)
        db.flush()
        for step_order, text in enumerate(parsed.steps, start=1):
            db.add(Step(section_id=section.id, order=step_order, text=text))


def import_xlsx(
    db: Session,
    path: str,
    *,
    dry_run: bool = False,
    sheet_name: str = "recipes",
    llm_verify: LLMVerifyMode = "off",
) -> ImportResult:
    """Bootstrap the legacy recipe library from an xlsx.

    Per-row strategy:
      1. Compute row_hash. If a Recipe with that hash exists → unchanged.
      2. Else look up by name_english.
         - Hit → upsert that row, wipe + replace children. Count "changed".
         - Miss → insert new row + children. Count "new".
      3. Per-line parse failures land in needs_review (not raised).

    `dry_run=True` runs every step inside a SAVEPOINT that ROLLBACKs at
    the end. The SyncRun row commits BEFORE the savepoint opens, so the
    counts are persisted even when no recipe changes are.

    `llm_verify` is wired through but the actual LLM logic lands in the
    followup commit — for now only "off" is functional.
    """
    log.info("import_xlsx start: path=%s dry_run=%s llm_verify=%s", path, dry_run, llm_verify)

    # Step 1: Persist SyncRun in its own transaction. Survives any
    # subsequent rollback by the body.
    sync_run = SyncRun(status="running", source="xlsx")
    db.add(sync_run)
    db.commit()
    db.refresh(sync_run)
    sync_run_id = sync_run.id

    # Step 2: Body work. Each row gets a SAVEPOINT so per-row failures
    # discard just that row and the loop continues.
    rows = read_xlsx_rows(path, sheet_name=sheet_name)
    log.info("import_xlsx: %d rows from %s", len(rows), path)

    counts = {"new": 0, "changed": 0, "unchanged": 0, "failed": 0}
    needs_review: list[str] = []

    for row in rows:
        sp = db.begin_nested()
        try:
            row_hash = compute_row_hash(row)

            hit = db.exec(
                select(Recipe).where(Recipe.source_row_hash == row_hash)
            ).first()
            if hit is not None:
                counts["unchanged"] += 1
                sp.commit()
                continue

            existing = db.exec(
                select(Recipe).where(Recipe.name_english == row.name_english)
            ).first()

            recipe = _upsert_recipe(db, row, row_hash, existing)

            ingredient_sections = parse_blob(row.ingredients_blob, db)
            _write_ingredients(db, recipe, ingredient_sections, needs_review, llm_verify_mode=llm_verify)

            step_sections = parse_instructions(row.instructions_blob)
            _write_steps(db, recipe, step_sections)

            if existing is None:
                counts["new"] += 1
            else:
                counts["changed"] += 1

            sp.commit()
        except (IntegrityError, Exception) as e:  # noqa: BLE001
            sp.rollback()
            counts["failed"] += 1
            msg = f"row={row.row_index} name={row.name_english!r} err={e}"
            needs_review.append(msg)
            log.exception("import row failed: %s", msg)

    # Step 3: Commit or rollback the bulk work
    if dry_run:
        db.rollback()
    else:
        db.commit()
        # Step 3b: Export JSON for each recipe that landed. Failures
        # here are logged but don't fail the import — the SQLite data
        # is already durable.
        for r in db.exec(select(Recipe)).all():
            try:
                export_recipe(r.id, db)
            except Exception as e:
                log.warning("recipe export failed for id=%s: %s", r.id, e)

    # Step 4: Update SyncRun with final state (fresh autobegun transaction)
    sync_run = db.get(SyncRun, sync_run_id)
    sync_run.finished_at = datetime.now(UTC)
    sync_run.status = "success" if counts["failed"] == 0 else "partial"
    sync_run.counts = counts
    sync_run.needs_review = needs_review
    db.commit()

    return ImportResult(
        sync_run_id=sync_run_id,
        rows_total=len(rows),
        rows_new=counts["new"],
        rows_changed=counts["changed"],
        rows_unchanged=counts["unchanged"],
        rows_failed=counts["failed"],
        needs_review=needs_review,
        dry_run=dry_run,
    )
