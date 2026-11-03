"""Tests for xlsx_importer using synthetic in-memory xlsx files.

Strangetom is stubbed via the parser's `strangetom=` keyword to avoid
loading the CRF model in unit tests. The importer doesn't expose that
hook directly (it calls parse_blob internally), so these tests assert
on the lighter surface — row reading, hashing, end-to-end happy path
with the real strangetom, dry-run behaviour, idempotency.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from openpyxl import Workbook
from sqlmodel import Session, select

from mise.models import Ingredient, IngredientSection, Recipe, Step, StepSection, SyncRun
from mise.services.xlsx_importer import (
    XlsxRow,
    compute_row_hash,
    import_xlsx,
    read_xlsx_rows,
)


def _write_xlsx(path: Path, rows: list[list]) -> None:
    """Write an xlsx file with the legacy column layout. `rows` is a
    list of row-data lists (after the header). Column 1 (A, the trigger)
    is unused but must be present."""
    wb = Workbook()
    ws = wb.active
    ws.title = "recipes"
    ws.append([
        "Generate Button", "Name", "Original Name", "Language",
        "Prep Time (mins)", "Cook Time (mins)", "Total Time (mins)",
        "Oven Temp (F)", "Serves", "Ingredients", "Instructions",
        "Link to recipe", "Doc link",
    ])
    for row in rows:
        ws.append(row)
    wb.save(path)


@pytest.fixture
def tmp_xlsx(tmp_path: Path) -> Path:
    p = tmp_path / "recipes.xlsx"
    _write_xlsx(
        p,
        [
            [
                None,  # trigger checkbox
                "Pad Thai",
                "ผัดไทย",
                "Thai",
                15, 20, 35, None, 4,
                "2 tbsp fish sauce\n1 tablespoon brown sugar\n1/2 pound shrimp",
                "1. Soak noodles\n2. Cook shrimp\n3. Toss with sauce",
            ],
            [
                None,
                "Simple Salt",
                None, None, None, None, None, None, 1,
                "Salt and pepper to taste",
                "1. Sprinkle",
            ],
        ],
    )
    return p


# --- read_xlsx_rows ----------------------------------------------------


def test_read_xlsx_rows_basic(tmp_xlsx: Path) -> None:
    rows = read_xlsx_rows(str(tmp_xlsx))
    assert len(rows) == 2
    assert rows[0].name_english == "Pad Thai"
    assert rows[0].name_original == "ผัดไทย"
    assert rows[0].language == "Thai"
    assert rows[0].prep_minutes == 15
    assert rows[0].cook_minutes == 20
    assert rows[0].total_minutes == 35
    assert rows[0].servings == 4
    assert "fish sauce" in rows[0].ingredients_blob
    assert "Soak noodles" in rows[0].instructions_blob


def test_read_xlsx_rows_skips_blank_name(tmp_path: Path) -> None:
    p = tmp_path / "x.xlsx"
    _write_xlsx(
        p,
        [
            [None, None, None, None, None, None, None, None, None, "", ""],  # blank
            [None, "Recipe1", None, None, None, None, None, None, 1, "salt", "mix"],
        ],
    )
    rows = read_xlsx_rows(str(p))
    assert len(rows) == 1
    assert rows[0].name_english == "Recipe1"


def test_read_xlsx_missing_sheet_raises(tmp_path: Path) -> None:
    p = tmp_path / "wrong-sheet.xlsx"
    wb = Workbook()
    wb.active.title = "WRONG"
    wb.save(p)
    with pytest.raises(FileNotFoundError):
        read_xlsx_rows(str(p))


# --- compute_row_hash --------------------------------------------------


def test_compute_row_hash_stable_for_same_content() -> None:
    r1 = XlsxRow(
        row_index=2,
        name_english="Pad Thai",
        name_original=None,
        language=None,
        prep_minutes=15,
        cook_minutes=20,
        total_minutes=35,
        oven_temp_f=None,
        servings=4,
        ingredients_blob="2 tbsp fish sauce",
        instructions_blob="1. Soak",
    )
    r2 = XlsxRow(**{**r1.__dict__, "row_index": 99})  # different row_index doesn't affect hash
    assert compute_row_hash(r1) == compute_row_hash(r2)


def test_compute_row_hash_changes_when_field_changes() -> None:
    base = XlsxRow(
        row_index=2,
        name_english="Pad Thai",
        name_original=None,
        language=None,
        prep_minutes=15,
        cook_minutes=20,
        total_minutes=35,
        oven_temp_f=None,
        servings=4,
        ingredients_blob="2 tbsp fish sauce",
        instructions_blob="1. Soak",
    )
    changed = XlsxRow(**{**base.__dict__, "servings": 6})
    assert compute_row_hash(base) != compute_row_hash(changed)


def test_compute_row_hash_normalises_whitespace_in_blobs() -> None:
    r1 = XlsxRow(
        row_index=2, name_english="X", name_original=None, language=None,
        prep_minutes=None, cook_minutes=None, total_minutes=None,
        oven_temp_f=None, servings=1, ingredients_blob="2 tbsp\n  fish  sauce",
        instructions_blob="Mix",
    )
    r2 = XlsxRow(**{**r1.__dict__, "ingredients_blob": "2 tbsp fish sauce"})
    assert compute_row_hash(r1) == compute_row_hash(r2)


# --- import_xlsx (end-to-end with real strangetom) --------------------


@pytest.fixture
def household(session: Session) -> None:
    """The importer assumes household_id=1 (single-tenant v1)."""
    from mise.models import Household
    h = Household(id=1, name="Test")
    session.add(h)
    session.commit()


def test_import_creates_recipes_and_children(session: Session, household, tmp_xlsx: Path) -> None:
    result = import_xlsx(session, str(tmp_xlsx))
    assert result.rows_total == 2
    assert result.rows_new == 2
    assert result.rows_unchanged == 0
    assert result.rows_failed == 0
    assert result.dry_run is False

    recipes = session.exec(select(Recipe)).all()
    assert {r.name_english for r in recipes} == {"Pad Thai", "Simple Salt"}

    # Pad Thai should have ingredients + steps
    pad_thai = next(r for r in recipes if r.name_english == "Pad Thai")
    ing_sections = session.exec(select(IngredientSection).where(IngredientSection.recipe_id == pad_thai.id)).all()
    assert len(ing_sections) >= 1
    ingredients = session.exec(select(Ingredient).where(Ingredient.section_id == ing_sections[0].id)).all()
    assert len(ingredients) >= 1
    step_sections = session.exec(select(StepSection).where(StepSection.recipe_id == pad_thai.id)).all()
    assert len(step_sections) >= 1
    steps = session.exec(select(Step).where(Step.section_id == step_sections[0].id)).all()
    assert {s.text for s in steps} == {"Soak noodles", "Cook shrimp", "Toss with sauce"}


def test_import_is_idempotent_on_rerun(session: Session, household, tmp_xlsx: Path) -> None:
    first = import_xlsx(session, str(tmp_xlsx))
    assert first.rows_new == 2

    second = import_xlsx(session, str(tmp_xlsx))
    assert second.rows_new == 0
    assert second.rows_changed == 0
    assert second.rows_unchanged == 2

    recipes = session.exec(select(Recipe)).all()
    assert len(recipes) == 2  # no duplicates


def test_dry_run_leaves_no_recipes_but_records_syncrun(session: Session, household, tmp_xlsx: Path) -> None:
    result = import_xlsx(session, str(tmp_xlsx), dry_run=True)
    assert result.dry_run is True
    assert result.rows_new == 2

    recipes = session.exec(select(Recipe)).all()
    assert recipes == []  # rolled back

    # SyncRun row should persist
    runs = session.exec(select(SyncRun)).all()
    assert len(runs) >= 1
    assert runs[-1].counts["new"] == 2
    assert runs[-1].source == "xlsx"


def test_import_changed_row_updates_in_place(session: Session, household, tmp_path: Path) -> None:
    p1 = tmp_path / "v1.xlsx"
    _write_xlsx(p1, [[None, "Egg", None, None, None, None, None, None, 1, "1 egg", "1. Crack"]])
    import_xlsx(session, str(p1))

    p2 = tmp_path / "v2.xlsx"
    _write_xlsx(p2, [[None, "Egg", None, None, None, None, None, None, 2, "2 eggs", "1. Crack both"]])
    result = import_xlsx(session, str(p2))

    assert result.rows_changed == 1
    assert result.rows_new == 0
    recipes = session.exec(select(Recipe)).all()
    assert len(recipes) == 1
    assert recipes[0].servings == 2
