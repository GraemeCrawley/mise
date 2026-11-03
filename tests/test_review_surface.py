"""Tests for the review-surface mutating endpoints + their helpers:

  - POST   /recipes/ingredient/{id}/split
  - DELETE /recipes/ingredient/{id}
  - POST   /recipes/section/{id}/reprocess
  - POST   /recipes/{id}/review/complete
  - mise.services.xlsx_importer._strip_unit_word_from_name (pure)
  - mise.models.Recipe.display_title (pure)

All happy-pathed by hand during the 2026-06-02 review-surface buildout;
these are the safety net for the next round of refactors.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session, select

from mise.models import (
    Household,
    Ingredient,
    IngredientSection,
    Recipe,
    Step,
    StepSection,
)
from mise.services.xlsx_importer import _strip_unit_word_from_name


@pytest.fixture
def household(session: Session) -> None:
    h = Household(id=1, name="Test")
    session.add(h)
    session.commit()


@pytest.fixture
def recipe_with_rows(client: TestClient, household, tmp_path, monkeypatch) -> dict:
    """Create a recipe with three parsable ingredient rows. Returns the
    posted JSON (has the id + section/ingredient ids via a follow-up
    fetch the caller can do)."""
    monkeypatch.setenv("RECIPES_EXPORT_PATH", str(tmp_path))
    posted = client.post("/recipes", json={
        "name_english": "Test Recipe",
        "name_original": "Test Recipe",
        "servings": 2,
        "ingredients_blob": "2 cloves garlic\n1 onion\n1/4 cup olive oil",
        "instructions_blob": "1. Cook",
        "llm_verify": "off",
    }).json()
    return posted


# --- split_ingredient -----------------------------------------------------


def test_split_ingredient_duplicates_row(
    client: TestClient, session: Session, recipe_with_rows,
) -> None:
    sect = session.exec(
        select(IngredientSection).where(
            IngredientSection.recipe_id == recipe_with_rows["id"]
        )
    ).first()
    rows = session.exec(
        select(Ingredient).where(Ingredient.section_id == sect.id).order_by(Ingredient.order)
    ).all()
    assert len(rows) == 3
    src = rows[0]
    src_id, src_order = src.id, src.order
    src_name = src.display_name

    r = client.post(f"/recipes/ingredient/{src_id}/split")
    assert r.status_code == 201, r.text
    clone = r.json()
    assert clone["id"] != src_id
    assert clone["display_name"] == src_name
    assert clone["qty"] == src.qty
    assert clone["unit"] == src.unit

    # Section now has one more row; later siblings shifted up by 1.
    session.expire_all()
    rows_after = session.exec(
        select(Ingredient).where(Ingredient.section_id == sect.id).order_by(Ingredient.order)
    ).all()
    assert len(rows_after) == 4
    assert rows_after[0].id == src_id
    assert rows_after[1].id == clone["id"]
    assert rows_after[1].order == src_order + 1
    # Originally 2nd and 3rd rows should now be 3rd and 4th.
    assert rows_after[2].order == src_order + 2
    assert rows_after[3].order == src_order + 3


def test_split_ingredient_copies_source_line(
    client: TestClient, session: Session, recipe_with_rows,
) -> None:
    """Regression: early split copied only raw_text, leaving the clone's
    source column empty. Must copy source_line too."""
    sect = session.exec(
        select(IngredientSection).where(
            IngredientSection.recipe_id == recipe_with_rows["id"]
        )
    ).first()
    src = session.exec(
        select(Ingredient).where(Ingredient.section_id == sect.id).order_by(Ingredient.order)
    ).first()
    # Make sure the source_line is set on the original (importer does this).
    assert src.source_line  # sanity

    r = client.post(f"/recipes/ingredient/{src.id}/split")
    clone_id = r.json()["id"]
    session.expire_all()
    clone = session.get(Ingredient, clone_id)
    assert clone.source_line == src.source_line


def test_split_ingredient_404_on_unknown(client: TestClient, household) -> None:
    r = client.post("/recipes/ingredient/99999/split")
    assert r.status_code == 404


# --- delete_ingredient ---------------------------------------------------


def test_delete_ingredient_removes_row(
    client: TestClient, session: Session, recipe_with_rows,
) -> None:
    sect = session.exec(
        select(IngredientSection).where(
            IngredientSection.recipe_id == recipe_with_rows["id"]
        )
    ).first()
    rows = session.exec(
        select(Ingredient).where(Ingredient.section_id == sect.id)
    ).all()
    target = rows[1]

    r = client.delete(f"/recipes/ingredient/{target.id}")
    assert r.status_code == 204

    session.expire_all()
    assert session.get(Ingredient, target.id) is None
    remaining = session.exec(
        select(Ingredient).where(Ingredient.section_id == sect.id)
    ).all()
    assert len(remaining) == 2


def test_delete_ingredient_404_on_unknown(client: TestClient, household) -> None:
    r = client.delete("/recipes/ingredient/99999")
    assert r.status_code == 404


# --- reprocess_section ----------------------------------------------------


def test_reprocess_section_replaces_rows_with_new_text(
    client: TestClient, session: Session, recipe_with_rows,
) -> None:
    sect = session.exec(
        select(IngredientSection).where(
            IngredientSection.recipe_id == recipe_with_rows["id"]
        )
    ).first()
    before_count = len(session.exec(
        select(Ingredient).where(Ingredient.section_id == sect.id)
    ).all())
    assert before_count == 3

    r = client.post(
        f"/recipes/section/{sect.id}/reprocess",
        json={"text": "1 cup flour\n2 eggs"},
    )
    assert r.status_code == 200, r.text
    assert r.json()["rows_written"] >= 2

    session.expire_all()
    rows_after = session.exec(
        select(Ingredient).where(Ingredient.section_id == sect.id)
    ).all()
    names = {r.display_name.lower() for r in rows_after}
    assert "flour" in str(names).lower()
    assert "egg" in str(names).lower()


def test_reprocess_section_trims_header_colon_name(
    client: TestClient, session: Session, recipe_with_rows,
) -> None:
    """When section.name is 'HEADER: body text', after reprocess the
    name should be trimmed to just 'HEADER' (body lives in the rows)."""
    sect = session.exec(
        select(IngredientSection).where(
            IngredientSection.recipe_id == recipe_with_rows["id"]
        )
    ).first()
    sect.name = "TOPPINGS: chopped fresh chives"
    session.add(sect)
    session.commit()

    client.post(
        f"/recipes/section/{sect.id}/reprocess",
        json={"text": "1 tbsp chopped chives"},
    )
    session.expire_all()
    refreshed = session.get(IngredientSection, sect.id)
    assert refreshed.name == "TOPPINGS"


def test_reprocess_section_400_when_no_text(
    client: TestClient, session: Session, recipe_with_rows,
) -> None:
    """With no body text AND no existing rows AND no section name, we
    have nothing to parse — should 400 cleanly."""
    sect = session.exec(
        select(IngredientSection).where(
            IngredientSection.recipe_id == recipe_with_rows["id"]
        )
    ).first()
    # Drop all rows so reconstruction can't find them.
    for ing in session.exec(
        select(Ingredient).where(Ingredient.section_id == sect.id)
    ).all():
        session.delete(ing)
    sect.name = None
    session.add(sect)
    session.commit()

    r = client.post(f"/recipes/section/{sect.id}/reprocess", json={"text": ""})
    assert r.status_code == 400


# --- complete_recipe_review -----------------------------------------------


def test_complete_review_stamps_and_returns_next_id(
    client: TestClient, session: Session, household, tmp_path, monkeypatch,
) -> None:
    monkeypatch.setenv("RECIPES_EXPORT_PATH", str(tmp_path))
    # Three recipes, all unreviewed.
    ids = []
    for n in ("A", "B", "C"):
        ids.append(client.post("/recipes", json={
            "name_english": n, "name_original": n, "servings": 1,
            "ingredients_blob": "1 egg", "instructions_blob": "1. Crack",
            "llm_verify": "off",
        }).json()["id"])

    r = client.post(f"/recipes/{ids[0]}/review/complete")
    assert r.status_code == 200
    out = r.json()
    assert out["id"] == ids[0]
    assert out["reviewed_at"] is not None
    # Next unreviewed is one of the other two (by id-order it's ids[1]).
    assert out["next_unreviewed_id"] == ids[1]

    session.expire_all()
    assert session.get(Recipe, ids[0]).reviewed_at is not None


def test_complete_review_returns_null_when_queue_empty(
    client: TestClient, session: Session, household, tmp_path, monkeypatch,
) -> None:
    monkeypatch.setenv("RECIPES_EXPORT_PATH", str(tmp_path))
    rid = client.post("/recipes", json={
        "name_english": "Solo", "name_original": "Solo", "servings": 1,
        "ingredients_blob": "1 egg", "instructions_blob": "1. Crack",
        "llm_verify": "off",
    }).json()["id"]

    out = client.post(f"/recipes/{rid}/review/complete").json()
    assert out["next_unreviewed_id"] is None


# --- _strip_unit_word_from_name (pure) -----------------------------------


@pytest.mark.parametrize(
    "name,unit,expected",
    [
        ("garlic cloves", "clove", "garlic"),
        ("celery stalks", "stalk", "celery"),
        ("can garbanzo beans", "can", "garbanzo beans"),
        ("canned tomatoes", "can", "tomatoes"),
        ("jarred peppers", "jar", "peppers"),
        ("sliced almonds", "slice", "sliced almonds"),  # prep word, kept
        ("chopped onion", "ea", "chopped onion"),       # unrelated unit
        ("flour", "cup", "flour"),                       # nothing to strip
        ("cloves", "clove", "cloves"),                   # strip → empty → keep orig
        ("", "clove", ""),                                # empty in → empty out
        ("garlic cloves", "", "garlic cloves"),          # no unit → no change
    ],
)
def test_strip_unit_word_from_name(name: str, unit: str, expected: str) -> None:
    assert _strip_unit_word_from_name(name, unit) == expected


# --- Cascade delete on Recipe → children ---------------------------------


def test_delete_recipe_cascades_to_children(
    session: Session, household,
) -> None:
    """Hard-deleting a Recipe should drop its sections, ingredients,
    step-sections, and steps via the ON DELETE CASCADE foreign keys
    added in migration c3d4e5f6a7b8 / cleaned in d4e5f6a7b8c9. Without
    cascade, a future id-reuse would inherit stale child rows (which
    is how we first noticed this bug)."""
    r = Recipe(
        household_id=1, name_original="Cascade Test",
        name_english="Cascade Test", servings=1,
    )
    session.add(r)
    session.flush()

    isect = IngredientSection(recipe_id=r.id, name=None, order=0)
    session.add(isect)
    session.flush()
    ing = Ingredient(
        section_id=isect.id, order=0, raw_text="egg", display_name="egg",
        canonical_name="egg", qty=1.0, unit="ea", category="Pantry",
    )
    session.add(ing)

    ssect = StepSection(recipe_id=r.id, name=None, order=0)
    session.add(ssect)
    session.flush()
    step = Step(section_id=ssect.id, order=1, text="Crack")
    session.add(step)
    session.commit()

    rid, isid, iid, ssid, stid = r.id, isect.id, ing.id, ssect.id, step.id

    session.delete(r)
    session.commit()
    session.expire_all()

    assert session.get(Recipe, rid) is None
    assert session.get(IngredientSection, isid) is None, "section orphaned"
    assert session.get(Ingredient, iid) is None, "ingredient orphaned"
    assert session.get(StepSection, ssid) is None, "step section orphaned"
    assert session.get(Step, stid) is None, "step orphaned"


# --- Recipe.display_title (pure) -----------------------------------------


@pytest.mark.parametrize(
    "english,original,expected",
    [
        ("Mulled Wine", "Conditum Paradoxum", "Mulled Wine"),
        ("Beef and Guinness Stew", "Stobhach Gaelach", "Beef and Guinness Stew"),
        # Mostly-non-Latin name_english with romanization hint in parens.
        # Parens stripped first → 0% Latin in main name → fall back.
        ("عرايس (عر-Arr ايس-Aise)", "Arayes (Meat Brides)", "Arayes (Meat Brides)"),
        ("मटर पनीर (म-ma ट-ta र-ra)", "Matar Paneer", "Matar Paneer"),
        ("饺子 (饺-jiǎo 子-zi)", "Dumplings", "Dumplings"),
        # No name_english → use name_original.
        (None, "Just Original", "Just Original"),
        # Both empty → "(unnamed)" sentinel.
        (None, None, "(unnamed)"),
    ],
)
def test_recipe_display_title(english, original, expected) -> None:
    # Build a transient Recipe without committing — display_title is
    # a pure method, no DB needed.
    r = Recipe(
        household_id=1,
        name_english=english,
        name_original=original or "",  # column is NOT NULL
        servings=1,
    )
    # For the None-name_original case, simulate by clearing after init.
    if original is None:
        r.name_original = ""
    assert r.display_title() == expected
