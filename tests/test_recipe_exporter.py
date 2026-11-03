"""Tests for recipe_exporter — writes one JSON file per recipe."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from sqlmodel import Session

from mise.models import Household, Ingredient, IngredientSection, Recipe, Step, StepSection
from mise.services.recipe_exporter import export_all, export_recipe


@pytest.fixture
def household(session: Session) -> Household:
    h = Household(id=1, name="Test")
    session.add(h)
    session.commit()
    return h


@pytest.fixture
def recipe_with_children(session: Session, household) -> Recipe:
    r = Recipe(household_id=1, name_english="Pad Thai", name_original="ผัดไทย", servings=4)
    session.add(r)
    session.flush()
    s = IngredientSection(recipe_id=r.id, name="Main", order=0)
    session.add(s)
    session.flush()
    session.add(Ingredient(
        section_id=s.id, order=0,
        raw_text="2 tbsp fish sauce",
        display_name="fish sauce",
        canonical_name="fish_sauce",
        prep_note=None,
        qty=30, unit="ml",
        category="Sauces",
        optional=False,
    ))
    ss = StepSection(recipe_id=r.id, name=None, order=0)
    session.add(ss)
    session.flush()
    session.add(Step(section_id=ss.id, order=1, text="Soak noodles"))
    session.commit()
    session.refresh(r)
    return r


def test_export_writes_json_file(session: Session, recipe_with_children, tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("RECIPES_EXPORT_PATH", str(tmp_path))
    result = export_recipe(recipe_with_children.id, session)

    assert result.path.exists()
    assert result.path.suffix == ".json"
    data = json.loads(result.path.read_text())
    assert data["name_english"] == "Pad Thai"
    assert data["name_original"] == "ผัดไทย"  # CJK + Thai script preserved
    assert data["servings"] == 4
    assert len(data["ingredient_sections"]) == 1
    assert data["ingredient_sections"][0]["ingredients"][0]["canonical_name"] == "fish_sauce"
    assert data["step_sections"][0]["steps"][0]["text"] == "Soak noodles"


def test_export_overwrites_stale_slug(session: Session, recipe_with_children, tmp_path, monkeypatch) -> None:
    """When name_english changes, the old slug-named file should be
    dropped so we don't accumulate orphans."""
    monkeypatch.setenv("RECIPES_EXPORT_PATH", str(tmp_path))

    first = export_recipe(recipe_with_children.id, session)
    assert first.path.exists()

    # Rename + re-export
    recipe_with_children.name_english = "Renamed Recipe"
    session.add(recipe_with_children)
    session.commit()
    second = export_recipe(recipe_with_children.id, session)

    # New file exists; old one gone
    assert second.path.exists()
    assert second.path != first.path
    assert not first.path.exists()


def test_export_recipe_not_found_raises(session: Session, household, tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("RECIPES_EXPORT_PATH", str(tmp_path))
    with pytest.raises(FileNotFoundError):
        export_recipe(9999, session)


def test_export_all_writes_each_active_recipe(session: Session, household, tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("RECIPES_EXPORT_PATH", str(tmp_path))
    for name in ["R1", "R2", "R3"]:
        session.add(Recipe(household_id=1, name_original=name, name_english=name, servings=1))
    # One archived — should be skipped
    session.add(Recipe(household_id=1, name_original="Archived", name_english="Archived", servings=1, archived=True))
    session.commit()

    results = export_all(session)
    assert len(results) == 3
    assert all(r.path.exists() for r in results)
    # Archived not on disk
    archived_files = list(Path(tmp_path).glob("*Archived*.json"))
    assert archived_files == []
