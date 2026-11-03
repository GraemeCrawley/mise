from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session

from mise.models import Household, Ingredient, IngredientSection, Recipe


def test_ingredient_edited_defaults_false():
    ing = Ingredient(
        section_id=1, order=0, raw_text="1 cup rice", display_name="rice",
        canonical_name="rice", qty=1, unit="cup", category="Pasta/Rice",
    )
    assert ing.edited is False


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _seed_ingredient(session: Session) -> int:
    """Seed a minimal Recipe + IngredientSection + Ingredient; return ingredient id."""
    h = Household(id=1, name="Test")
    session.add(h)
    session.flush()

    r = Recipe(household_id=1, name_original="Test Recipe", servings=2)
    session.add(r)
    session.flush()

    sect = IngredientSection(recipe_id=r.id, order=0)
    session.add(sect)
    session.flush()

    ing = Ingredient(
        section_id=sect.id,
        order=0,
        raw_text="1 cup rice",
        display_name="rice",
        canonical_name="rice",
        qty=1.0,
        unit="cup",
        category="Pasta/Rice",
    )
    session.add(ing)
    session.commit()
    session.refresh(ing)
    return ing.id


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def test_patch_tracked_field_sets_edited(
    client: TestClient, session: Session, tmp_path, monkeypatch,
) -> None:
    """PATCHing qty (a tracked field) must flip edited to True."""
    monkeypatch.setenv("RECIPES_EXPORT_PATH", str(tmp_path))
    ing_id = _seed_ingredient(session)

    r = client.patch(f"/recipes/ingredient/{ing_id}", json={"qty": 3.0})
    assert r.status_code == 200, r.text

    session.expire_all()
    ing = session.get(Ingredient, ing_id)
    assert ing.edited is True


def test_patch_prep_note_does_not_set_edited(
    client: TestClient, session: Session, tmp_path, monkeypatch,
) -> None:
    """PATCHing prep_note (NOT a tracked field) must leave edited False."""
    monkeypatch.setenv("RECIPES_EXPORT_PATH", str(tmp_path))
    ing_id = _seed_ingredient(session)

    r = client.patch(f"/recipes/ingredient/{ing_id}", json={"prep_note": "rinsed"})
    assert r.status_code == 200, r.text

    session.expire_all()
    ing = session.get(Ingredient, ing_id)
    assert ing.edited is False
