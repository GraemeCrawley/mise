"""Unit tests for shopping_aggregator.aggregate — pure-function checks
that don't depend on the parser or LLM."""

from __future__ import annotations

from sqlmodel import Session

from mise.models import Household, Ingredient, IngredientSection, Recipe
from mise.services.shopping_aggregator import aggregate, _dimension


def _household(db: Session, **kwargs) -> int:
    h = Household(name="Test", **kwargs)
    db.add(h)
    db.flush()
    return h.id


def _seed_recipe(
    db: Session, name: str, ings: list[tuple[float, str, str, str]],
    household_id: int | None = None,
) -> int:
    """ings = [(qty, unit, display_name, category), …]"""
    if household_id is None:
        h = db.query(Household).first() if hasattr(db, "query") else None
        from sqlmodel import select as _select
        h = db.exec(_select(Household)).first()
        household_id = h.id if h else _household(db)
    r = Recipe(name_english=name, name_original=name, servings=2, household_id=household_id)
    db.add(r)
    db.flush()
    s = IngredientSection(recipe_id=r.id, name=None, order=0)
    db.add(s)
    db.flush()
    for i, (qty, unit, name_, cat) in enumerate(ings):
        db.add(Ingredient(
            section_id=s.id, order=i,
            raw_text=name_, display_name=name_, canonical_name=name_.replace(" ", "_"),
            qty=qty, unit=unit, category=cat, optional=False,
        ))
    db.commit()
    return r.id


def test_aggregate_sums_same_canonical_and_unit(session: Session) -> None:
    session.add(Household(name="Test"))
    session.commit()
    r1 = _seed_recipe(session, "A", [(2.0, "clove", "garlic", "Produce")])
    r2 = _seed_recipe(session, "B", [(3.0, "clove", "garlic", "Produce")])
    items = aggregate([r1, r2], session)
    garlic = next(it for it in items if it.canonical_name == "garlic")
    assert garlic.qty == 5.0
    assert garlic.unit == "clove"
    assert garlic.source_count == 2


def test_aggregate_converts_mass_units(session: Session) -> None:
    session.add(Household(name="Test"))
    session.commit()
    r1 = _seed_recipe(session, "A", [(500.0, "g", "ground beef", "Meat")])
    r2 = _seed_recipe(session, "B", [(1.0, "lb", "ground beef", "Meat")])
    items = aggregate([r1, r2], session)
    beef = next(it for it in items if it.canonical_name == "ground_beef")
    # Both rows are mass dimension → one group; display_qty uses largest unit (lb).
    # 500g + 1lb: sum in lb = 500/453.592 + 1 ≈ 2.10 lb
    assert beef.source_count == 2
    assert "lb" in beef.display_qty
    qty_val = float(beef.display_qty.split()[0])
    assert abs(qty_val - 2.10) < 0.05


def test_aggregate_converts_volume_units(session: Session) -> None:
    session.add(Household(name="Test"))
    session.commit()
    r1 = _seed_recipe(session, "A", [(2.0, "cup", "water", "Beverages")])
    r2 = _seed_recipe(session, "B", [(1.0, "cup", "water", "Beverages")])
    items = aggregate([r1, r2], session)
    water = next(it for it in items if it.canonical_name == "water")
    assert water.unit == "cup"
    assert abs(water.qty - 3.0) < 0.05


def test_aggregate_shows_parts_for_unbridgeable_dimensions(session: Session) -> None:
    """For canonicals not in buy_units, count + volume rows can't be bridged
    without density — so rule 3 shows them as honest PARTS in one combined
    line rather than inventing a number."""
    session.add(Household(name="Test"))
    session.commit()
    r1 = _seed_recipe(session, "A", [(2.0, "ea", "wonton wrappers", "Pantry")])
    r2 = _seed_recipe(session, "B", [(1.0, "cup", "wonton wrappers", "Pantry")])
    items = aggregate([r1, r2], session)
    # canonical normalized: 'wonton_wrappers' → 'wonton_wrapper'
    entries = [it for it in items if it.canonical_name == "wonton_wrapper"]
    assert len(entries) == 1
    assert entries[0].display_qty == "2 whole + 1 cup"


def test_known_solid_volume_converts_to_count(session: Session) -> None:
    """Garlic clove + tsp combine into ONE line in buy units (cloves) via
    rule 1, even though the source rows are in different dimensions
    (package vs volume). Grouping is by canonical, not canonical+dimension,
    so _display_line sees all occurrences and sums them."""
    session.add(Household(name="Test"))
    session.commit()
    r1 = _seed_recipe(session, "A", [(3.0, "clove", "garlic", "Produce")])
    # 2 tsp minced ≈ 2 cloves (1 tsp = 1 clove per buy_units bridges)
    r2 = _seed_recipe(session, "B", [(2.0, "tsp", "garlic", "Produce")])
    items = aggregate([r1, r2], session)
    garlic_entries = [it for it in items if it.canonical_name == "garlic"]
    assert len(garlic_entries) == 1
    it = garlic_entries[0]
    # 3 cloves + 2 tsp(=2 cloves) = 5 cloves; below the head-rollup threshold.
    assert "clove" in it.display_qty
    assert abs(float(it.display_qty.split()[0]) - 5.0) < 0.5


def test_aggregate_skips_optional(session: Session) -> None:
    h_id = _household(session)
    r = Recipe(name_english="R", name_original="R", servings=2, household_id=h_id)
    session.add(r)
    session.flush()
    s = IngredientSection(recipe_id=r.id, name=None, order=0)
    session.add(s)
    session.flush()
    session.add(Ingredient(
        section_id=s.id, order=0,
        raw_text="salt", display_name="salt", canonical_name="salt",
        qty=0.0, unit="ea", category="Spices", optional=True,
    ))
    session.commit()
    items = aggregate([r.id], session)
    assert not any(it.canonical_name == "salt" for it in items)


def test_aggregate_sorted_by_household_category_order(session: Session) -> None:
    h_id = _household(session, grocery_categories=["Meat", "Produce", "Dairy"])
    r = Recipe(name_english="R", name_original="R", servings=2, household_id=h_id)
    session.add(r); session.flush()
    s = IngredientSection(recipe_id=r.id, name=None, order=0)
    session.add(s); session.flush()
    for i, (cat, name) in enumerate([("Dairy", "milk"), ("Meat", "beef"), ("Produce", "carrot")]):
        session.add(Ingredient(
            section_id=s.id, order=i,
            raw_text=name, display_name=name, canonical_name=name,
            qty=1.0, unit="ea", category=cat, optional=False,
        ))
    session.commit()
    items = aggregate([r.id], session)
    cats = [it.category for it in items]
    assert cats == ["Meat", "Produce", "Dairy"]


def test_aggregate_merges_canonical_aliases(session: Session) -> None:
    """whole_cloves and cloves should land in one shopping row."""
    session.add(Household(name="Test"))
    session.commit()
    h_id = session.exec(__import__("sqlmodel").select(Household)).first().id

    r1 = Recipe(name_english="A", name_original="A", servings=2, household_id=h_id)
    r2 = Recipe(name_english="B", name_original="B", servings=2, household_id=h_id)
    session.add_all([r1, r2]); session.flush()
    for r, canon in [(r1, "whole_cloves"), (r2, "cloves")]:
        s = IngredientSection(recipe_id=r.id, name=None, order=0)
        session.add(s); session.flush()
        session.add(Ingredient(
            section_id=s.id, order=0,
            raw_text="cloves", display_name="whole cloves", canonical_name=canon,
            qty=5.0, unit="ea", category="Spices", optional=False,
        ))
    session.commit()

    items = aggregate([r1.id, r2.id], session)
    # alias → "cloves", then singular strip → "clove"
    cloves = [it for it in items if it.canonical_name == "clove"]
    assert len(cloves) == 1
    assert cloves[0].qty == 10.0


def test_aggregate_strips_plural_in_canonical(session: Session) -> None:
    """cinnamon_stick + cinnamon_sticks → one row."""
    session.add(Household(name="Test"))
    session.commit()
    h_id = session.exec(__import__("sqlmodel").select(Household)).first().id

    r1 = Recipe(name_english="A", name_original="A", servings=2, household_id=h_id)
    r2 = Recipe(name_english="B", name_original="B", servings=2, household_id=h_id)
    session.add_all([r1, r2]); session.flush()
    for r, canon in [(r1, "cinnamon_stick"), (r2, "cinnamon_sticks")]:
        s = IngredientSection(recipe_id=r.id, name=None, order=0)
        session.add(s); session.flush()
        session.add(Ingredient(
            section_id=s.id, order=0,
            raw_text="cinnamon", display_name="cinnamon stick", canonical_name=canon,
            qty=1.0, unit="ea", category="Spices", optional=False,
        ))
    session.commit()

    items = aggregate([r1.id, r2.id], session)
    sticks = [it for it in items if "cinnamon" in it.canonical_name]
    assert len(sticks) == 1
    assert sticks[0].qty == 2.0


def test_aggregate_keeps_known_plural_unchanged() -> None:
    """molasses stays molasses, doesn't become molasse."""
    from mise.services.shopping_aggregator import _normalize_canonical
    assert _normalize_canonical("molasses") == "molasses"
    assert _normalize_canonical("couscous") == "couscous"


def test_aggregate_merges_mass_and_count_solids(session: Session) -> None:
    """Yellow onion '9 ea' + '4 lb': yellow_onion is not in buy_units, so
    ea (count) and lb (mass) land in separate dimension groups — two rows."""
    session.add(Household(name="Test"))
    session.commit()
    h_id = session.exec(__import__("sqlmodel").select(Household)).first().id
    r1 = Recipe(name_english="A", name_original="A", servings=2, household_id=h_id)
    r2 = Recipe(name_english="B", name_original="B", servings=2, household_id=h_id)
    session.add_all([r1, r2]); session.flush()
    for r, qty, unit in [(r1, 9.0, "ea"), (r2, 4.0, "lb")]:
        s = IngredientSection(recipe_id=r.id, name=None, order=0)
        session.add(s); session.flush()
        session.add(Ingredient(
            section_id=s.id, order=0,
            raw_text="onion", display_name="yellow onion",
            canonical_name="yellow_onion",
            qty=qty, unit=unit, category="Produce", optional=False,
        ))
    session.commit()
    items = aggregate([r1.id, r2.id], session)
    onions = [it for it in items if it.canonical_name == "yellow_onion"]
    # yellow_onion isn't in the (minimal) buy_units table, so it falls back to
    # natural units. ea (count) + lb (mass) don't convert → rule 3 shows parts
    # in ONE combined line.
    assert len(onions) == 1
    assert onions[0].display_qty == "9 whole + 4 lb"


def test_aggregate_merges_length_and_count_cinnamon(session: Session) -> None:
    """Cinnamon stick '4 ea' + '1 inch': cinnamon_stick is not in buy_units,
    so ea (count) and inch (length) don't convert → rule 3 shows parts in one
    combined line."""
    session.add(Household(name="Test"))
    session.commit()
    h_id = session.exec(__import__("sqlmodel").select(Household)).first().id
    r1 = Recipe(name_english="A", name_original="A", servings=2, household_id=h_id)
    r2 = Recipe(name_english="B", name_original="B", servings=2, household_id=h_id)
    session.add_all([r1, r2]); session.flush()
    for r, qty, unit in [(r1, 4.0, "ea"), (r2, 1.0, "inch")]:
        s = IngredientSection(recipe_id=r.id, name=None, order=0)
        session.add(s); session.flush()
        session.add(Ingredient(
            section_id=s.id, order=0,
            raw_text="cinnamon", display_name="cinnamon stick",
            canonical_name="cinnamon_stick",
            qty=qty, unit=unit, category="Spices", optional=False,
        ))
    session.commit()
    items = aggregate([r1.id, r2.id], session)
    sticks = [it for it in items if it.canonical_name == "cinnamon_stick"]
    assert len(sticks) == 1
    assert sticks[0].display_qty == "4 whole + 1 inch"


def test_pod_and_clove_share_dimension(session: Session) -> None:
    """1 pod garlic + 4 clove garlic → both 'package' dim, both unit pods/cloves
    differ but they merge after _SOLID_VOLUME_TO_COUNT converts. Actually
    package units stay distinct (count sum) — but at least same dim."""
    from mise.services.shopping_aggregator import _dimension
    assert _dimension("pod") == _dimension("clove") == "package"
    assert _dimension("rib") == _dimension("stalk") == "package"


def test_aliases_collapse_canonical_drift() -> None:
    """LLM drift cases collapse to the canonical form."""
    from mise.services.shopping_aggregator import _normalize_canonical
    assert _normalize_canonical("green_cardamom_pods") == "green_cardamom"
    assert _normalize_canonical("fresh_lemon_juice") == "lemon_juice"
    # yukon_gold_russet_potatoes → potatoes (alias) → potato (singular)
    assert _normalize_canonical("yukon_gold_russet_potatoes") == "potato"


def test_dimension_classification() -> None:
    assert _dimension("g") == "mass"
    assert _dimension("lb") == "mass"
    assert _dimension("cup") == "volume"
    assert _dimension("ml") == "volume"
    assert _dimension("clove") == "package"
    assert _dimension("ea") == "count"
    assert _dimension("inch") == "length"
    assert _dimension("frobnitz") == "count"  # unknown → count fallback
