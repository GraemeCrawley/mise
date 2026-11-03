"""POST /recipes — the durable ongoing-add path.

Reuses the same `upsert_recipe_from_blobs` helper that the xlsx
importer uses, so the form-driven Phase 1 UI and the legacy-bootstrap
path go through exactly the same recipe-write code.

Body shape mirrors the legacy xlsx column layout — ingredients and
instructions arrive as text blobs. The parser handles the rest.
"""

from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlmodel import Session, select

from mise.db import get_session
from mise.models import (
    Household,
    Ingredient,
    IngredientAlias,
    IngredientSection,
    Recipe,
    Step,
    StepSection,
)
from mise.services.ingredient_parser import parse_blob
from mise.services.llm_verify import VerifyMode as LLMVerifyMode
from mise.services.llm_verify import verify_ingredient
from mise.services.recipe_drafter import create_draft
from mise.services.recipe_exporter import export_recipe
from mise.services.step_annotator import unmentioned
from mise.services.xlsx_importer import (
    _strip_unit_word_from_name,
    upsert_recipe_from_blobs,
)

router = APIRouter(prefix="/recipes", tags=["recipes"])


class CreateRecipeIn(BaseModel):
    name_english: str
    name_original: str | None = None
    language: str | None = None
    prep_minutes: int | None = None
    cook_minutes: int | None = None
    total_minutes: int | None = None
    oven_temp_f: int | None = None
    servings: int = 2
    ingredients_blob: str
    instructions_blob: str
    source_url: str | None = None
    llm_verify: LLMVerifyMode = "first-seen"


class RecipeOut(BaseModel):
    id: int
    name_english: str | None
    name_original: str
    language: str | None
    servings: int
    needs_review: list[str]


@router.post("", response_model=RecipeOut, status_code=201)
def create_recipe(
    body: CreateRecipeIn,
    db: Session = Depends(get_session),
) -> RecipeOut:
    """Create or update a recipe by `name_english`. Idempotent: posting
    the same name twice replaces sections; client UI should issue PATCH
    for partial edits (Phase 1) instead."""
    needs_review: list[str] = []
    try:
        recipe = upsert_recipe_from_blobs(
            db,
            name_english=body.name_english,
            name_original=body.name_original,
            language=body.language,
            prep_minutes=body.prep_minutes,
            cook_minutes=body.cook_minutes,
            total_minutes=body.total_minutes,
            oven_temp_f=body.oven_temp_f,
            servings=body.servings,
            ingredients_blob=body.ingredients_blob,
            instructions_blob=body.instructions_blob,
            source_url=body.source_url,
            llm_verify_mode=body.llm_verify,
            needs_review=needs_review,
        )
        db.commit()
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=400, detail=str(e)) from e

    # Best-effort JSON export — failure here doesn't fail the request
    try:
        export_recipe(recipe.id, db)
    except Exception:
        pass

    return RecipeOut(
        id=recipe.id,
        name_english=recipe.name_english,
        name_original=recipe.name_original,
        language=recipe.language,
        servings=recipe.servings,
        needs_review=needs_review,
    )


@router.get("/{recipe_id}", response_model=RecipeOut)
def get_recipe(recipe_id: int, db: Session = Depends(get_session)) -> RecipeOut:
    r = db.get(Recipe, recipe_id)
    if r is None:
        raise HTTPException(status_code=404, detail="recipe not found")
    return RecipeOut(
        id=r.id,
        name_english=r.name_english,
        name_original=r.name_original,
        language=r.language,
        servings=r.servings,
        needs_review=[],
    )


def _title_case_tag(s: str) -> str:
    """Title-case a tag while preserving apostrophes ('kid's' stays
    'Kid's', not 'Kid'S' which is what str.title() would give). Trims
    whitespace; multi-word tags like 'make ahead' become 'Make Ahead'."""
    return " ".join(
        w[:1].upper() + w[1:].lower() for w in s.strip().split() if w
    )


def _normalize_tags(tags: list[str]) -> list[str]:
    """Title-case and de-dup (case-insensitively) a tag list. Preserves
    first-occurrence order so the user sees their input shape back."""
    seen: set[str] = set()
    out: list[str] = []
    for t in tags:
        titled = _title_case_tag(t)
        if not titled:
            continue
        key = titled.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(titled)
    return out


class PatchRecipeIn(BaseModel):
    """Partial update — all fields optional. Per docs/api-surface.md
    'PATCH /recipes/{id} update (logs corrections)'. The corrections-
    audit row is deferred to Phase 1; for Phase 0.5 we just write."""
    name_english: str | None = None
    name_original: str | None = None
    servings: int | None = None
    prep_minutes: int | None = None
    cook_minutes: int | None = None
    total_minutes: int | None = None
    oven_temp_f: int | None = None
    tags: list[str] | None = None
    archived: bool | None = None
    archived_reason: str | None = None
    # ISO-8601 datetime string or null. Null clears the reviewed state
    # (puts the recipe back on the queue).
    reviewed_at: datetime | None = None
    source_url: str | None = None
    language: str | None = None
    emoji: str | None = None
    notes_markdown: str | None = None


@router.patch("/{recipe_id}", response_model=RecipeOut)
def patch_recipe(
    recipe_id: int, body: PatchRecipeIn, db: Session = Depends(get_session),
) -> RecipeOut:
    r = db.get(Recipe, recipe_id)
    if r is None:
        raise HTTPException(status_code=404, detail="recipe not found")
    for field, value in body.model_dump(exclude_unset=True).items():
        if field == "tags" and value is not None:
            value = _normalize_tags(value)
        setattr(r, field, value)
    db.add(r)
    db.commit()
    db.refresh(r)
    try:
        export_recipe(r.id, db)
    except Exception:
        pass
    return RecipeOut(
        id=r.id,
        name_english=r.name_english,
        name_original=r.name_original,
        language=r.language,
        servings=r.servings,
        needs_review=[],
    )


class PatchIngredientIn(BaseModel):
    """Partial ingredient edit — the household can fix the parser's quirks
    inline. Includes canonical_name + category since the review
    surface is exactly where wrong LLM categorisations get corrected
    (the original 'not the household's job' framing was wrong for review)."""
    qty: float | None = None
    unit: str | None = None
    display_name: str | None = None
    prep_note: str | None = None
    size_descriptor: str | None = None
    alternatives_note: str | None = None
    optional: bool | None = None
    canonical_name: str | None = None
    category: str | None = None


class IngredientOut(BaseModel):
    id: int
    qty: float
    unit: str
    display_name: str
    prep_note: str | None
    optional: bool


@router.patch("/ingredient/{ingredient_id}", response_model=IngredientOut)
def patch_ingredient(
    ingredient_id: int, body: PatchIngredientIn, db: Session = Depends(get_session),
) -> IngredientOut:
    i = db.get(Ingredient, ingredient_id)
    if i is None:
        raise HTTPException(status_code=404, detail="ingredient not found")

    changed = body.model_dump(exclude_unset=True)
    for field, value in changed.items():
        setattr(i, field, value)
    # Any human touch to a display/quantity field marks the row edited so
    # the natural-unit migrator never overwrites it.
    if {"qty", "unit", "display_name", "optional"} & set(changed):
        i.edited = True
    db.add(i)

    # Mirror canonical / category fixes into IngredientAlias so they
    # stick across re-parses. Without this, next import of a recipe
    # with the same FDC match would inherit the OLD (wrong) cached
    # value. Key the alias on raw_text (what the importer matches on
    # for repeats of the same source phrasing).
    if "canonical_name" in changed or "category" in changed:
        key = (i.raw_text or i.display_name or "").lower().strip()
        if key:
            alias = db.get(IngredientAlias, key)
            if alias is None:
                alias = IngredientAlias(
                    display_name=key,
                    canonical_name=i.canonical_name,
                    category=i.category,
                    confidence=1.0,
                    source="manual",
                )
            else:
                alias.canonical_name = i.canonical_name
                alias.category = i.category
                alias.confidence = 1.0
                alias.source = "manual"
            db.add(alias)

    db.commit()
    db.refresh(i)
    # Re-export the parent recipe so JSON exports stay current.
    sect = db.get(IngredientSection, i.section_id)
    if sect:
        try:
            export_recipe(sect.recipe_id, db)
        except Exception:
            pass
    return IngredientOut(
        id=i.id, qty=i.qty, unit=i.unit, display_name=i.display_name,
        prep_note=i.prep_note, optional=i.optional,
    )


class AddIngredientIn(BaseModel):
    """Structured ingredient add — Phase 1's escape hatch from the
    parser. The cook types qty / unit / name directly; no source_line,
    no LLM, no canonical guessing. canonical_name derives mechanically
    from display_name (slug). Category required so it groups on the
    shopping list."""
    qty: float
    unit: str
    display_name: str
    category: str
    prep_note: str | None = None
    optional: bool = False
    section_id: int | None = None  # if omitted, append to first section


def _slug_canonical(name: str) -> str:
    """display_name → canonical_name. Lowercase, words joined by
    underscore, strips punctuation. Deliberate that this is dumb:
    'red bell pepper' → 'red_bell_pepper'. Aggregation merges on
    this string so consistency matters more than cleverness."""
    import re
    s = name.lower().strip()
    s = re.sub(r"[^a-z0-9\s]", "", s)
    s = re.sub(r"\s+", "_", s)
    return s or "unknown"


@router.post("/{recipe_id}/ingredients", response_model=IngredientOut, status_code=201)
def add_ingredient(
    recipe_id: int, body: AddIngredientIn, db: Session = Depends(get_session),
) -> IngredientOut:
    r = db.get(Recipe, recipe_id)
    if r is None:
        raise HTTPException(status_code=404, detail="recipe not found")

    # Resolve the target section. Default: first section by order.
    # Create one if the recipe has none (stub-recipe case).
    if body.section_id is not None:
        sect = db.get(IngredientSection, body.section_id)
        if sect is None or sect.recipe_id != recipe_id:
            raise HTTPException(status_code=400, detail="bad section_id")
    else:
        sect = db.exec(
            select(IngredientSection)
            .where(IngredientSection.recipe_id == recipe_id)
            .order_by(IngredientSection.order)
        ).first()
        if sect is None:
            sect = IngredientSection(recipe_id=recipe_id, name=None, order=0)
            db.add(sect)
            db.flush()

    # Append: order = max(existing) + 1
    existing = db.exec(
        select(Ingredient).where(Ingredient.section_id == sect.id)
    ).all()
    next_order = max((i.order for i in existing), default=-1) + 1

    ing = Ingredient(
        section_id=sect.id,
        order=next_order,
        raw_text=body.display_name,
        display_name=body.display_name,
        canonical_name=_slug_canonical(body.display_name),
        qty=body.qty,
        unit=body.unit,
        category=body.category,
        prep_note=body.prep_note,
        optional=body.optional,
    )
    db.add(ing)
    db.commit()
    db.refresh(ing)

    try:
        export_recipe(recipe_id, db)
    except Exception:
        pass

    return IngredientOut(
        id=ing.id, qty=ing.qty, unit=ing.unit, display_name=ing.display_name,
        prep_note=ing.prep_note, optional=ing.optional,
    )


@router.get("/{recipe_id}/cook-check")
def cook_check(recipe_id: int, db: Session = Depends(get_session)) -> dict:
    """Review check: ingredients whose amounts cook mode can't place in
    any step (no step names them in a way the matcher understands)."""
    if db.get(Recipe, recipe_id) is None:
        raise HTTPException(status_code=404, detail="recipe not found")
    ings = db.exec(
        select(Ingredient).join(IngredientSection, Ingredient.section_id == IngredientSection.id)
        .where(IngredientSection.recipe_id == recipe_id)
        .order_by(IngredientSection.order, Ingredient.order)
    ).all()
    steps = db.exec(
        select(Step).join(StepSection, Step.section_id == StepSection.id)
        .where(StepSection.recipe_id == recipe_id)
        .order_by(StepSection.order, Step.order)
    ).all()
    rows = [(i.display_name, i.qty, i.unit) for i in ings if i.display_name != "unknown"]
    return {"steps": len(steps), "unmentioned": unmentioned([s.text for s in steps], rows)}


@router.post("/{recipe_id}/review/complete", status_code=200)
def complete_recipe_review(
    recipe_id: int, db: Session = Depends(get_session),
) -> dict:
    """Stamp the recipe as reviewed. Used by the review queue to signal
    'I've eyeballed every row, the data is clean enough to ship'.

    Returns the next-unreviewed-recipe id so the client can advance —
    saves a separate query and keeps the front-end simple."""
    from datetime import UTC, datetime as _dt
    r = db.get(Recipe, recipe_id)
    if r is None:
        raise HTTPException(status_code=404, detail="recipe not found")
    r.reviewed_at = _dt.now(UTC)
    db.add(r)
    db.commit()

    # Next un-reviewed active recipe (by id) — gives the UI a "next →"
    # target without an extra round-trip.
    next_id = db.exec(
        select(Recipe)
        .where(Recipe.archived == False)  # noqa: E712
        .where(Recipe.reviewed_at.is_(None))
        .order_by(Recipe.id)
    ).first()
    return {
        "id": r.id,
        "reviewed_at": r.reviewed_at.isoformat() if r.reviewed_at else None,
        "next_unreviewed_id": next_id.id if next_id else None,
    }


class ReprocessSectionIn(BaseModel):
    """Body for POST /section/{id}/reprocess. Empty text → server
    reconstructs from existing rows or falls back to section.name."""
    text: str | None = None


@router.post("/section/{section_id}/reprocess")
def reprocess_section(
    section_id: int,
    body: ReprocessSectionIn,
    db: Session = Depends(get_session),
) -> dict:
    """Re-run the parser on this section's text — drops the existing
    ingredient rows and replaces them with a fresh parse.

    Useful when the original import lost rows (compound lines, weird
    headers) and the user edits the source text inline before
    reparsing. Section name is preserved unless the user clears it via
    the existing section-header edit."""
    sect = db.get(IngredientSection, section_id)
    if sect is None:
        raise HTTPException(status_code=404, detail="section not found")

    existing = db.exec(
        select(Ingredient)
        .where(Ingredient.section_id == section_id)
        .order_by(Ingredient.order)
    ).all()

    text = (body.text or "").strip()
    if not text:
        # Reconstruct: prefer existing rows' source_lines; else fall
        # back to the section name (handles the "empty TOPPINGS:" case
        # where the parser swallowed the body into the header).
        lines = [
            (i.source_line or i.raw_text) for i in existing
            if (i.source_line or i.raw_text)
        ]
        if lines:
            text = "\n".join(lines)
        elif sect.name:
            t = sect.name
            if ":" in t:
                _, _, tail = t.partition(":")
                text = tail.strip() if tail.strip() else t
            else:
                text = t

    if not text.strip():
        raise HTTPException(status_code=400, detail="no text to reparse")

    # Trim section.name down to the header portion. After reprocess the
    # body of "HEADER: rest of text" lives in the new rows, so the name
    # shouldn't keep dragging it along.
    if sect.name and ":" in sect.name:
        head, _, _ = sect.name.partition(":")
        new_name = head.strip() or None
        if sect.name != new_name:
            sect.name = new_name
            db.add(sect)

    for i in existing:
        db.delete(i)
    db.flush()

    parsed_sections = parse_blob(text, db)
    order = 0
    written = 0
    for ps in parsed_sections:
        for pi in ps.lines:
            # LLM verify — first-seen mode, same as the xlsx importer.
            # Without this every fresh row landed as category="Other"
            # because the deterministic resolver only fires when the
            # alias cache already has the answer.
            pi = verify_ingredient(pi, db, mode="first-seen")
            if pi.parse_failed:
                display_name = "unknown"
                canonical_name = "unknown"
            else:
                display_name = pi.display_name_pretty or pi.name
                display_name = _strip_unit_word_from_name(display_name, pi.unit)
                canonical_name = pi.canonical_name
            db.add(Ingredient(
                section_id=section_id,
                order=order,
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
            ))
            order += 1
            written += 1

    db.commit()
    try:
        export_recipe(sect.recipe_id, db)
    except Exception:
        pass

    return {"section_id": section_id, "rows_written": written}


@router.delete("/ingredient/{ingredient_id}", status_code=204)
def delete_ingredient(
    ingredient_id: int, db: Session = Depends(get_session),
):
    """Hard delete a single ingredient row. Used for cleaning up an
    accidental split or a parser-introduced phantom row. No undo —
    if the user wanted soft-delete, the existing IngredientAlias
    cache means re-adding by the same source line restores the row."""
    i = db.get(Ingredient, ingredient_id)
    if i is None:
        raise HTTPException(status_code=404, detail="ingredient not found")
    section_id = i.section_id
    db.delete(i)
    db.commit()
    sect = db.get(IngredientSection, section_id)
    if sect:
        try:
            export_recipe(sect.recipe_id, db)
        except Exception:
            pass
    return None


@router.post("/ingredient/{ingredient_id}/split", response_model=IngredientOut, status_code=201)
def split_ingredient(
    ingredient_id: int, db: Session = Depends(get_session),
) -> IngredientOut:
    """Duplicate an ingredient inline — review-time helper for compound
    rows like 'salt and black pepper' that the parser collapsed into
    one. The clone lands immediately after the source row; the user
    edits qty/name on the new row to split them apart.

    Shifts the order of every later sibling +1 to keep ordering
    stable."""
    src = db.get(Ingredient, ingredient_id)
    if src is None:
        raise HTTPException(status_code=404, detail="ingredient not found")

    # Bump every sibling at or after src.order+1 to make a hole.
    siblings = db.exec(
        select(Ingredient)
        .where(Ingredient.section_id == src.section_id)
        .where(Ingredient.order > src.order)
        .order_by(Ingredient.order.desc())
    ).all()
    for s in siblings:
        s.order = s.order + 1
        db.add(s)

    clone = Ingredient(
        section_id=src.section_id,
        order=src.order + 1,
        raw_text=src.raw_text,
        source_line=src.source_line,
        display_name=src.display_name,
        canonical_name=src.canonical_name,
        qty=src.qty,
        unit=src.unit,
        category=src.category,
        prep_note=src.prep_note,
        size_descriptor=src.size_descriptor,
        optional=src.optional,
    )
    db.add(clone)
    db.commit()
    db.refresh(clone)

    sect = db.get(IngredientSection, src.section_id)
    if sect:
        try:
            export_recipe(sect.recipe_id, db)
        except Exception:
            pass

    return IngredientOut(
        id=clone.id, qty=clone.qty, unit=clone.unit,
        display_name=clone.display_name, prep_note=clone.prep_note,
        optional=clone.optional,
    )


class CreateBlankRecipeIn(BaseModel):
    """Header-only recipe creation. Ingredients + steps come via the
    per-row POSTs after — Phase 1's structured-input answer to parser
    fragility on new recipes. No blobs, no parser, no LLM."""
    name_english: str
    name_original: str | None = None
    servings: int = 2
    language: str | None = None
    prep_minutes: int | None = None
    cook_minutes: int | None = None
    total_minutes: int | None = None
    oven_temp_f: int | None = None


@router.post("/blank", response_model=RecipeOut, status_code=201)
def create_blank_recipe(
    body: CreateBlankRecipeIn, db: Session = Depends(get_session),
) -> RecipeOut:
    household = db.exec(select(Household)).first()
    if household is None:
        # Single-tenant Phase 0/1 — auto-create the implicit household.
        household = Household(name="Home")
        db.add(household)
        db.flush()

    r = Recipe(
        household_id=household.id,
        name_english=body.name_english,
        name_original=body.name_original or body.name_english,
        servings=body.servings,
        language=body.language,
        prep_minutes=body.prep_minutes,
        cook_minutes=body.cook_minutes,
        total_minutes=body.total_minutes,
        oven_temp_f=body.oven_temp_f,
    )
    db.add(r)
    db.commit()
    db.refresh(r)

    try:
        export_recipe(r.id, db)
    except Exception:
        pass

    return RecipeOut(
        id=r.id,
        name_english=r.name_english,
        name_original=r.name_original,
        language=r.language,
        servings=r.servings,
        needs_review=[],
    )


class DraftIn(BaseModel):
    """One capture box, server decides: lone-URL text routes to the
    link path inside create_draft."""
    text: str | None = None
    url: str | None = None


@router.post("/draft", status_code=201)
def create_draft_recipe(body: DraftIn, db: Session = Depends(get_session)) -> dict:
    if not (body.text and body.text.strip()) and not (body.url and body.url.strip()):
        raise HTTPException(status_code=400, detail="Paste a recipe or a link first.")
    try:
        recipe = create_draft(db, text=body.text, url=body.url)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    db.commit()
    db.refresh(recipe)

    # Best-effort JSON export — failure here doesn't fail the request
    try:
        export_recipe(recipe.id, db)
    except Exception:
        pass

    return {"id": recipe.id, "title": recipe.name_english}


class PatchStepIn(BaseModel):
    text: str


@router.patch("/step/{step_id}", status_code=200)
def patch_step(step_id: int, body: PatchStepIn, db: Session = Depends(get_session)) -> dict:
    step = db.get(Step, step_id)
    if step is None:
        raise HTTPException(status_code=404, detail="step not found")
    step.text = body.text.strip()
    db.add(step)
    db.commit()

    # Best-effort JSON export — failure here doesn't fail the request
    sect = db.get(StepSection, step.section_id)
    if sect:
        try:
            export_recipe(sect.recipe_id, db)
        except Exception:
            pass

    return {"id": step.id, "text": step.text}


@router.delete("/step/{step_id}", status_code=204)
def delete_step(step_id: int, db: Session = Depends(get_session)) -> None:
    step = db.get(Step, step_id)
    if step is None:
        raise HTTPException(status_code=404, detail="step not found")
    # Capture recipe_id before deleting the step
    sect = db.get(StepSection, step.section_id)
    recipe_id = sect.recipe_id if sect else None
    db.delete(step)
    db.commit()

    # Best-effort JSON export — failure here doesn't fail the request
    if recipe_id is not None:
        try:
            export_recipe(recipe_id, db)
        except Exception:
            pass


class AddStepIn(BaseModel):
    """Structured step add — mirrors AddIngredientIn shape."""
    text: str
    section_id: int | None = None  # if omitted, append to first section


class StepOut(BaseModel):
    id: int
    order: int
    text: str


@router.post("/{recipe_id}/steps", response_model=StepOut, status_code=201)
def add_step(
    recipe_id: int, body: AddStepIn, db: Session = Depends(get_session),
) -> StepOut:
    r = db.get(Recipe, recipe_id)
    if r is None:
        raise HTTPException(status_code=404, detail="recipe not found")

    if body.section_id is not None:
        sect = db.get(StepSection, body.section_id)
        if sect is None or sect.recipe_id != recipe_id:
            raise HTTPException(status_code=400, detail="bad section_id")
    else:
        sect = db.exec(
            select(StepSection)
            .where(StepSection.recipe_id == recipe_id)
            .order_by(StepSection.order)
        ).first()
        if sect is None:
            sect = StepSection(recipe_id=recipe_id, name=None, order=0)
            db.add(sect)
            db.flush()

    existing = db.exec(
        select(Step).where(Step.section_id == sect.id)
    ).all()
    next_order = max((s.order for s in existing), default=0) + 1

    step = Step(section_id=sect.id, order=next_order, text=body.text)
    db.add(step)
    db.commit()
    db.refresh(step)

    try:
        export_recipe(recipe_id, db)
    except Exception:
        pass

    return StepOut(id=step.id, order=step.order, text=step.text)
