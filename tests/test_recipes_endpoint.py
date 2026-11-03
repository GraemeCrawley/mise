"""Tests for POST /recipes — the durable ongoing-add path."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session, select

from mise.models import Household, Recipe


@pytest.fixture
def household(session: Session) -> None:
    h = Household(id=1, name="Test")
    session.add(h)
    session.commit()


def test_post_recipe_creates(client: TestClient, household, tmp_path, monkeypatch) -> None:
    # Send exports to a temp dir so the test doesn't write to /data
    monkeypatch.setenv("RECIPES_EXPORT_PATH", str(tmp_path))

    body = {
        "name_english": "Test Recipe",
        "name_original": "Test Recipe",
        "servings": 4,
        "ingredients_blob": "2 tablespoons olive oil\n1 onion, diced",
        "instructions_blob": "1. Heat oil\n2. Add onion",
        "llm_verify": "off",
    }
    r = client.post("/recipes", json=body)
    assert r.status_code == 201, r.text
    out = r.json()
    assert out["name_english"] == "Test Recipe"
    assert out["servings"] == 4
    assert isinstance(out["needs_review"], list)


def test_post_recipe_returns_id_that_get_finds(client: TestClient, household, tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("RECIPES_EXPORT_PATH", str(tmp_path))
    body = {
        "name_english": "X",
        "name_original": "X",
        "servings": 1,
        "ingredients_blob": "1 egg",
        "instructions_blob": "1. Crack",
        "llm_verify": "off",
    }
    posted = client.post("/recipes", json=body).json()
    fetched = client.get(f"/recipes/{posted['id']}")
    assert fetched.status_code == 200
    assert fetched.json()["name_english"] == "X"


def test_post_ingredient_appends_to_first_section(
    client: TestClient, household, session: Session, tmp_path, monkeypatch,
) -> None:
    monkeypatch.setenv("RECIPES_EXPORT_PATH", str(tmp_path))
    posted = client.post("/recipes", json={
        "name_english": "Add Test", "name_original": "Add Test", "servings": 2,
        "ingredients_blob": "1 egg",
        "instructions_blob": "1. Crack",
        "llm_verify": "off",
    }).json()

    add = client.post(f"/recipes/{posted['id']}/ingredients", json={
        "qty": 2.0, "unit": "tbsp", "display_name": "olive oil",
        "category": "Pantry",
    })
    assert add.status_code == 201, add.text
    out = add.json()
    assert out["display_name"] == "olive oil"
    assert out["qty"] == 2.0
    assert out["unit"] == "tbsp"

    # Re-fetched recipe JSON shows the new row
    detail = client.get(f"/recipes/{posted['id']}/json").json()
    names = [
        ing["display_name"]
        for sect in detail["ingredient_sections"]
        for ing in sect["ingredients"]
    ]
    assert "olive oil" in names


def test_post_ingredient_creates_section_for_stub_recipe(
    client: TestClient, household, session: Session, tmp_path, monkeypatch,
) -> None:
    monkeypatch.setenv("RECIPES_EXPORT_PATH", str(tmp_path))
    from mise.models import Recipe
    r = Recipe(name_english="Stub", name_original="Stub", servings=2, household_id=1)
    session.add(r); session.commit(); session.refresh(r)

    add = client.post(f"/recipes/{r.id}/ingredients", json={
        "qty": 1.0, "unit": "ea", "display_name": "egg", "category": "Dairy",
    })
    assert add.status_code == 201, add.text


def test_post_ingredient_slugs_canonical_name(
    client: TestClient, household, session: Session, tmp_path, monkeypatch,
) -> None:
    monkeypatch.setenv("RECIPES_EXPORT_PATH", str(tmp_path))
    posted = client.post("/recipes", json={
        "name_english": "Slug Test", "name_original": "Slug Test", "servings": 2,
        "ingredients_blob": "1 egg", "instructions_blob": "1. Crack",
        "llm_verify": "off",
    }).json()
    client.post(f"/recipes/{posted['id']}/ingredients", json={
        "qty": 1.0, "unit": "ea", "display_name": "Red Bell Pepper!",
        "category": "Produce",
    })
    from mise.models import Ingredient
    ing = session.exec(
        select(Ingredient).where(Ingredient.display_name == "Red Bell Pepper!")
    ).first()
    assert ing.canonical_name == "red_bell_pepper"


def test_post_blank_creates_header_only(
    client: TestClient, household, session: Session, tmp_path, monkeypatch,
) -> None:
    """The Phase 1 'create from form' path — no blobs, no parser."""
    monkeypatch.setenv("RECIPES_EXPORT_PATH", str(tmp_path))
    r = client.post("/recipes/blank", json={
        "name_english": "Brand New", "servings": 4,
        "prep_minutes": 15, "cook_minutes": 30,
    })
    assert r.status_code == 201, r.text
    out = r.json()
    assert out["name_english"] == "Brand New"
    assert out["servings"] == 4

    from mise.models import IngredientSection, StepSection
    # No sections yet — they're created lazily on first ingredient/step add
    sections = session.exec(
        select(IngredientSection).where(IngredientSection.recipe_id == out["id"])
    ).all()
    assert sections == []


def test_post_blank_then_add_ingredients_and_steps(
    client: TestClient, household, session: Session, tmp_path, monkeypatch,
) -> None:
    """Full new-recipe flow: blank → add ingredients → add steps."""
    monkeypatch.setenv("RECIPES_EXPORT_PATH", str(tmp_path))
    r = client.post("/recipes/blank", json={"name_english": "Test", "servings": 2})
    rid = r.json()["id"]

    client.post(f"/recipes/{rid}/ingredients", json={
        "qty": 2.0, "unit": "tbsp", "display_name": "olive oil",
        "category": "Pantry",
    })
    step = client.post(f"/recipes/{rid}/steps", json={"text": "Heat the oil"})
    assert step.status_code == 201
    assert step.json()["text"] == "Heat the oil"
    assert step.json()["order"] == 1

    detail = client.get(f"/recipes/{rid}/json").json()
    assert detail["ingredient_sections"][0]["ingredients"][0]["display_name"] == "olive oil"
    assert detail["step_sections"][0]["steps"][0]["text"] == "Heat the oil"


def test_patch_tags_title_cases_and_dedupes(
    client: TestClient, household, session: Session, tmp_path, monkeypatch,
) -> None:
    monkeypatch.setenv("RECIPES_EXPORT_PATH", str(tmp_path))
    posted = client.post("/recipes", json={
        "name_english": "T", "name_original": "T", "servings": 2,
        "ingredients_blob": "1 egg", "instructions_blob": "1. Crack",
        "llm_verify": "off",
    }).json()
    # Mix of cases + dup; expect title-case + de-dup
    r = client.patch(f"/recipes/{posted['id']}", json={
        "tags": ["favourite", "MAKE AHEAD", "Favourite", "potluck"],
    })
    assert r.status_code == 200, r.text
    from mise.models import Recipe
    refreshed = session.get(Recipe, posted["id"])
    session.refresh(refreshed)
    assert refreshed.tags == ["Favourite", "Make Ahead", "Potluck"]


def test_title_case_preserves_apostrophes() -> None:
    from mise.adapters.rest.recipes import _title_case_tag
    assert _title_case_tag("kid's favourite") == "Kid's Favourite"
    assert _title_case_tag("  potluck  ") == "Potluck"


def test_post_recipe_same_name_updates_in_place(client: TestClient, household, session: Session, tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("RECIPES_EXPORT_PATH", str(tmp_path))
    body = {
        "name_english": "Same Name",
        "name_original": "Same Name",
        "servings": 2,
        "ingredients_blob": "1 egg",
        "instructions_blob": "1. Crack",
        "llm_verify": "off",
    }
    client.post("/recipes", json=body)
    body["servings"] = 4
    client.post("/recipes", json=body)

    # Only one recipe row, with the updated servings
    recipes = session.exec(select(Recipe)).all()
    assert len(recipes) == 1
    assert recipes[0].servings == 4


# ---- draft + step endpoints --------------------------------------------------

def test_post_draft_creates_recipe(client, session, household, monkeypatch, tmp_path):
    import mise.adapters.rest.recipes as recipes_mod
    from mise.models import Recipe

    monkeypatch.setenv("RECIPES_EXPORT_PATH", str(tmp_path))

    def fake_create_draft(db, *, text=None, url=None, llm_verify_mode="first-seen"):
        r = Recipe(household_id=1, name_english="Drafted Dal", name_original="Drafted Dal", servings=4)
        db.add(r)
        db.flush()
        return r

    monkeypatch.setattr(recipes_mod, "create_draft", fake_create_draft)
    resp = client.post("/recipes/draft", json={"text": "some ramble"})
    assert resp.status_code == 201
    body = resp.json()
    assert body["title"] == "Drafted Dal"
    assert isinstance(body["id"], int)


def test_post_draft_maps_valueerror_to_400(client, monkeypatch):
    import mise.adapters.rest.recipes as recipes_mod

    def boom(db, *, text=None, url=None, llm_verify_mode="first-seen"):
        raise ValueError("Couldn't find a recipe on that page.")

    monkeypatch.setattr(recipes_mod, "create_draft", boom)
    resp = client.post("/recipes/draft", json={"url": "https://example.com/x"})
    assert resp.status_code == 400
    assert "Couldn't find a recipe" in resp.json()["detail"]


def test_post_draft_requires_input(client):
    resp = client.post("/recipes/draft", json={})
    assert resp.status_code == 400


def _make_recipe_with_step(session):
    from mise.models import Recipe, Step, StepSection
    r = Recipe(household_id=1, name_english="S", name_original="S", servings=2)
    session.add(r); session.flush()
    sec = StepSection(recipe_id=r.id, name=None, order=0)
    session.add(sec); session.flush()
    st = Step(section_id=sec.id, order=1, text="Old text")
    session.add(st); session.flush()
    return r, st


def test_patch_step(client, session, household, monkeypatch, tmp_path):
    from mise.models import Step
    monkeypatch.setenv("RECIPES_EXPORT_PATH", str(tmp_path))
    r, st = _make_recipe_with_step(session)
    resp = client.patch(f"/recipes/step/{st.id}", json={"text": "New text"})
    assert resp.status_code == 200
    session.expire_all()
    assert session.get(Step, st.id).text == "New text"


def test_patch_step_404(client):
    assert client.patch("/recipes/step/99999", json={"text": "x"}).status_code == 404


def test_delete_step(client, session, household, monkeypatch, tmp_path):
    from mise.models import Step
    monkeypatch.setenv("RECIPES_EXPORT_PATH", str(tmp_path))
    r, st = _make_recipe_with_step(session)
    resp = client.delete(f"/recipes/step/{st.id}")
    assert resp.status_code == 204
    session.expire_all()
    assert session.get(Step, st.id) is None


def test_archived_draft_page_404s(client, session, household, monkeypatch, tmp_path):
    monkeypatch.setenv("RECIPES_EXPORT_PATH", str(tmp_path))
    posted = client.post("/recipes", json={
        "name_english": "Discarded Draft", "name_original": "Discarded Draft",
        "servings": 2, "ingredients_blob": "1 egg", "instructions_blob": "1. Crack",
        "llm_verify": "off",
    }).json()
    client.patch(f"/recipes/{posted['id']}", json={"archived": True, "archived_reason": "discarded"})
    resp = client.get(f"/app/recipe/{posted['id']}/draft", follow_redirects=False)
    assert resp.status_code == 404


def test_edit_screen_opens_for_saved_recipe(client, session, household, monkeypatch, tmp_path):
    """✎ Edit on a saved recipe must open the editor, not bounce back to
    the recipe page — and must not offer Discard (it would archive it)."""
    monkeypatch.setenv("RECIPES_EXPORT_PATH", str(tmp_path))
    posted = client.post("/recipes", json={
        "name_english": "Saved", "name_original": "Saved", "servings": 2,
        "ingredients_blob": "1 egg", "instructions_blob": "1. Crack", "llm_verify": "off",
    }).json()
    draft = client.get(f"/app/recipe/{posted['id']}/draft", follow_redirects=False)
    assert 'id="d-discard"' in draft.text
    assert client.post(f"/recipes/{posted['id']}/review/complete").status_code == 200
    resp = client.get(f"/app/recipe/{posted['id']}/draft", follow_redirects=False)
    assert resp.status_code == 200
    assert "Editing" in resp.text
    assert 'data-f="display_name"' in resp.text
    assert 'id="d-discard"' not in resp.text


def test_recipe_page_shows_inline_step_quantities(client, session, household, monkeypatch, tmp_path):
    monkeypatch.setenv("RECIPES_EXPORT_PATH", str(tmp_path))
    posted = client.post("/recipes", json={
        "name_english": "Eggs", "name_original": "Eggs", "servings": 2,
        "ingredients_blob": "4 eggs\n2 cups rice", "instructions_blob": "1. Cook eggs\n2. Serve over rice",
        "llm_verify": "off",
    }).json()
    page = client.get(f"/app/recipe/{posted['id']}").text
    assert "Cook eggs<span class='step-qty'> (<span class='ing-qty' data-qty='4.0' data-unit=''>4</span>)</span>" in page
    assert "data-unit='cup'>2 cup</span>)</span>" in page


def test_review_queue_item_opens_draft_screen(client, session, household, monkeypatch, tmp_path):
    """The 'awaiting review' queue must land on the app-native draft screen,
    not the legacy /preview/review editor."""
    monkeypatch.setenv("RECIPES_EXPORT_PATH", str(tmp_path))
    posted = client.post("/recipes", json={
        "name_english": "Queued", "name_original": "Queued", "servings": 2,
        "ingredients_blob": "1 egg", "instructions_blob": "1. Crack", "llm_verify": "off",
    }).json()
    resp = client.get(f"/app/review/{posted['id']}", follow_redirects=False)
    assert resp.status_code == 307
    assert resp.headers["location"].endswith(f"/app/recipe/{posted['id']}/draft")


@pytest.mark.parametrize("source,name,prep,expected", [
    # parser dropped the head noun (tuna), kept the packing medium
    ("1 (4.5-ounce) can Albacore Tuna in Extra Virgin Olive Oil", "extra virgin olive oil", "drained", True),
    # correct parses — head noun preserved, descriptors/parens/plurals aside
    ("1 carrot, peeled and grated", "carrot", "peeled and grated", False),
    ("3 cloves garlic (minced or pressed)", "garlic", "minced or pressed", False),
    ("2 eggs, beaten", "egg", None, False),
    ("1/4 cup extra virgin olive oil", "olive oil", None, False),
    ("2 tablespoons freshly ground black pepper", "black pepper", None, False),
])
def test_parse_divergence(source, name, prep, expected):
    from mise.adapters.rest.app import _parse_divergence
    assert _parse_divergence(source, name, prep) is expected


def test_cook_mode_bracket_includes_prep(client, session, household, monkeypatch, tmp_path):
    monkeypatch.setenv("RECIPES_EXPORT_PATH", str(tmp_path))
    posted = client.post("/recipes", json={
        "name_english": "Onions", "name_original": "Onions", "servings": 2,
        "ingredients_blob": "1 onion, finely diced", "instructions_blob": "1. Add the onion and cook",
        "llm_verify": "off",
    }).json()
    page = client.get(f"/app/recipe/{posted['id']}").text
    assert "data-unit=''>1</span>, finely diced)</span>" in page


def test_cook_check_lists_ingredients_no_step_names(client, session, household, monkeypatch, tmp_path):
    monkeypatch.setenv("RECIPES_EXPORT_PATH", str(tmp_path))
    posted = client.post("/recipes", json={
        "name_english": "Check", "name_original": "Check", "servings": 2,
        "ingredients_blob": "1 onion\n2 bay leaves", "instructions_blob": "1. Fry the onion",
        "llm_verify": "off",
    }).json()
    d = client.get(f"/recipes/{posted['id']}/cook-check").json()
    assert d["steps"] == 1 and [n.lower() for n in d["unmentioned"]] == ["bay leaves"]
    assert 'id="cook-check"' in client.get(f"/app/recipe/{posted['id']}/draft").text
    assert client.get("/recipes/99999/cook-check").status_code == 404
