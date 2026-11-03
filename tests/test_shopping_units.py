from sqlmodel import Session

from mise.models import Household, Ingredient, IngredientSection, Recipe
from mise.services.shopping_aggregator import _display_line


def _seed(db: Session, name: str, ings: list[tuple[float, str, str, str]]) -> int:
    """ings = [(qty, unit, canonical/display, category)]"""
    h = Household(name="Test")
    db.add(h)
    db.flush()
    r = Recipe(name_english=name, name_original=name, servings=2, household_id=h.id)
    db.add(r)
    db.flush()
    s = IngredientSection(recipe_id=r.id, name=None, order=0)
    db.add(s)
    db.flush()
    for i, (qty, unit, nm, cat) in enumerate(ings):
        db.add(Ingredient(
            section_id=s.id, order=i, raw_text=nm, display_name=nm,
            canonical_name=nm.replace(" ", "_"), qty=qty, unit=unit,
            category=cat, optional=False,
        ))
    db.commit()
    return r.id


def test_shopping_api_returns_display_qty_incl_multipart(client, session):
    # flour cup + g is unbridgeable -> rule 3 multi-part line.
    rid = _seed(session, "R", [(2, "cup", "flour", "Pantry"),
                               (250, "g", "flour", "Pantry")])
    resp = client.post("/shopping", json={"recipe_ids": [rid]})
    assert resp.status_code == 200
    flour = next(i for i in resp.json()["items"] if i["canonical_name"] == "flour")
    assert flour["display_qty"] == "2 cup + 250 g"


def test_edamame_two_cups_sum_in_cups():
    assert _display_line("edamame", [(1, "cup"), (1, "cup")]) == "2 cup"

def test_garlic_rolls_up_to_heads():
    assert _display_line("garlic", [(2, "clove"), (1, "cup")]) == "~5 head"

def test_tofu_grams_plus_block():
    assert _display_line("tofu", [(200, "g"), (1, "block")]) == "600 g"

def test_milk_cup_plus_tbsp_compatible():
    assert _display_line("milk", [(1, "cup"), (2, "tbsp")]).startswith("1⅛")

def test_flour_incompatible_shows_parts():
    assert _display_line("flour", [(2, "cup"), (250, "g")]) == "2 cup + 250 g"

def test_onion_ea_plus_cup():
    assert _display_line("onion", [(1, "ea"), (1, "cup")]) == "2"
