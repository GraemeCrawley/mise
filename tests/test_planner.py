"""Unit tests for the planner service. Pure DB shape — no HTTP."""

from __future__ import annotations

from datetime import date, timedelta

import pytest
from sqlmodel import Session, select

from mise.models import (
    CookingSession,
    Household,
    Ingredient,
    IngredientSection,
    Recipe,
    SlotAssignment,
)
from mise.services.planner import (
    clear_window,
    get_view,
    move,
    place,
    planned_recipe_ids,
    swap,
    unplace,
)


@pytest.fixture
def household(session: Session) -> Household:
    h = Household(name="Test", default_servings_per_meal=2)
    session.add(h)
    session.commit()
    session.refresh(h)
    return h


def _recipe(session: Session, household_id: int, name: str, servings: int) -> Recipe:
    r = Recipe(
        household_id=household_id,
        name_english=name,
        name_original=name,
        servings=servings,
    )
    session.add(r)
    session.commit()
    session.refresh(r)
    return r


def test_place_single_slot_recipe(session: Session, household: Household) -> None:
    """2-serving recipe at 2 servings/slot fills exactly one slot."""
    r = _recipe(session, household.id, "Eggs", servings=2)
    sess = place(r.id, date(2026, 6, 1), "breakfast", session)
    session.commit()
    slots = session.exec(select(SlotAssignment)).all()
    assert len(slots) == 1
    assert slots[0].date == date(2026, 6, 1)
    assert slots[0].meal == "breakfast"
    assert slots[0].position == 1


def test_place_fills_leftovers_forward(session: Session, household: Household) -> None:
    """6-serving recipe at 2/slot fills 3 forward dinner slots."""
    r = _recipe(session, household.id, "Chili", servings=6)
    place(r.id, date(2026, 6, 1), "dinner", session)
    session.commit()
    slots = session.exec(select(SlotAssignment).order_by(SlotAssignment.date)).all()
    assert len(slots) == 3
    assert [s.date for s in slots] == [
        date(2026, 6, 1), date(2026, 6, 2), date(2026, 6, 3),
    ]
    assert all(s.meal == "dinner" for s in slots)
    assert [s.position for s in slots] == [1, 2, 3]


def test_place_skips_occupied_slots(session: Session, household: Household) -> None:
    """Leftovers jump over a dinner that's already booked."""
    chili = _recipe(session, household.id, "Chili", servings=6)
    pasta = _recipe(session, household.id, "Pasta", servings=2)
    # Pre-book Tuesday's dinner
    place(pasta.id, date(2026, 6, 2), "dinner", session)
    session.commit()
    # Now place chili Monday dinner — should land Mon, skip Tue, land Wed + Thu
    place(chili.id, date(2026, 6, 1), "dinner", session)
    session.commit()
    chili_slots = sorted(
        (s.date for s in session.exec(select(SlotAssignment)).all()
         if s.session_id != session.exec(
             select(CookingSession).where(CookingSession.recipe_id == pasta.id)
         ).first().id),
    )
    assert chili_slots == [
        date(2026, 6, 1), date(2026, 6, 3), date(2026, 6, 4),
    ]


def test_place_conflict_raises(session: Session, household: Household) -> None:
    r = _recipe(session, household.id, "A", servings=2)
    r2 = _recipe(session, household.id, "B", servings=2)
    place(r.id, date(2026, 6, 1), "lunch", session)
    session.commit()
    with pytest.raises(ValueError, match="already taken"):
        place(r2.id, date(2026, 6, 1), "lunch", session)


def test_place_rejects_unknown_meal(session: Session, household: Household) -> None:
    r = _recipe(session, household.id, "A", servings=2)
    with pytest.raises(ValueError, match="unknown meal"):
        place(r.id, date(2026, 6, 1), "brunch", session)


def test_unplace_removes_session_and_all_slots(
    session: Session, household: Household,
) -> None:
    r = _recipe(session, household.id, "Chili", servings=6)
    place(r.id, date(2026, 6, 1), "dinner", session)
    session.commit()
    assert session.exec(select(SlotAssignment)).all().__len__() == 3
    assert unplace(date(2026, 6, 2), "dinner", session) is True  # tap any slot
    session.commit()
    # ALL slots from this session are gone
    assert session.exec(select(SlotAssignment)).all() == []
    assert session.exec(select(CookingSession)).all() == []


def test_unplace_empty_slot_returns_false(
    session: Session, household: Household,
) -> None:
    assert unplace(date(2026, 6, 1), "dinner", session) is False


def test_get_view_returns_full_grid(session: Session, household: Household) -> None:
    """3 days × 3 meals = 9 slots, with placed recipes filled in."""
    r = _recipe(session, household.id, "Eggs", servings=2)
    place(r.id, date(2026, 6, 1), "breakfast", session)
    session.commit()
    view = get_view(date(2026, 6, 1), 3, session)
    assert len(view) == 3
    assert all(len(d.slots) == 3 for d in view)
    # Monday breakfast is filled, everything else empty
    mon_b = view[0].slots[0]
    assert mon_b.recipe_name == "Eggs"
    assert mon_b.position == 1
    mon_l = view[0].slots[1]
    assert mon_l.recipe_id is None


def test_get_view_respects_household_meals_order(session: Session) -> None:
    """Slot order follows household.meals — adding 'snack' later just
    works."""
    h = Household(
        name="Test", default_servings_per_meal=2,
        meals=["breakfast", "snack", "lunch", "dinner"],
    )
    session.add(h)
    session.commit()
    view = get_view(date(2026, 6, 1), 1, session)
    assert [s.meal for s in view[0].slots] == [
        "breakfast", "snack", "lunch", "dinner",
    ]


def test_planned_recipe_ids_dedupes_across_window(
    session: Session, household: Household,
) -> None:
    chili = _recipe(session, household.id, "Chili", servings=6)
    eggs = _recipe(session, household.id, "Eggs", servings=2)
    place(chili.id, date(2026, 6, 1), "dinner", session)  # 3 slots, 1 recipe
    place(eggs.id, date(2026, 6, 1), "breakfast", session)
    session.commit()
    ids = planned_recipe_ids(date(2026, 6, 1), 7, session)
    assert sorted(ids) == sorted([chili.id, eggs.id])


def test_place_with_servings_override_fills_more_slots(
    session: Session, household: Household,
) -> None:
    """A 2-serving recipe placed with override=8 fills 4 slots
    (8 / 2-per-slot), and the session records batch_multiplier=4."""
    r = _recipe(session, household.id, "Small", servings=2)
    sess = place(r.id, date(2026, 6, 1), "dinner", session, servings_override=8)
    session.commit()
    slots = session.exec(select(SlotAssignment)).all()
    assert len(slots) == 4
    assert sess.batch_multiplier == 4.0


def test_place_without_override_uses_recipe_default(
    session: Session, household: Household,
) -> None:
    """No override → batch_multiplier stays 1.0."""
    r = _recipe(session, household.id, "Default", servings=4)
    sess = place(r.id, date(2026, 6, 1), "dinner", session)
    session.commit()
    assert sess.batch_multiplier == 1.0
    # 4 servings @ 2/slot = 2 slots
    assert session.exec(select(SlotAssignment)).all().__len__() == 2


def test_swap_replaces_recipe_in_same_slot(
    session: Session, household: Household,
) -> None:
    chili = _recipe(session, household.id, "Chili", servings=6)
    pasta = _recipe(session, household.id, "Pasta", servings=2)
    place(chili.id, date(2026, 6, 1), "dinner", session)
    session.commit()
    swap(date(2026, 6, 1), "dinner", pasta.id, session)
    session.commit()
    # Old chili gone, pasta now at Mon dinner
    slots = session.exec(select(SlotAssignment)).all()
    assert len(slots) == 1
    assert slots[0].date == date(2026, 6, 1)
    sess = session.exec(select(CookingSession)).first()
    assert sess.recipe_id == pasta.id


def test_swap_via_leftover_slot_acts_on_cook(
    session: Session, household: Household,
) -> None:
    """Tapping a leftover row to swap operates on the whole session,
    not just that one cell."""
    chili = _recipe(session, household.id, "Chili", servings=6)
    pasta = _recipe(session, household.id, "Pasta", servings=2)
    place(chili.id, date(2026, 6, 1), "dinner", session)
    session.commit()
    # Tuesday is a leftover slot
    swap(date(2026, 6, 2), "dinner", pasta.id, session)
    session.commit()
    slots = session.exec(select(SlotAssignment)).all()
    assert len(slots) == 1
    # Cook stays at Monday (the original session's cook_date)
    assert slots[0].date == date(2026, 6, 1)


def test_move_shifts_whole_session(
    session: Session, household: Household,
) -> None:
    chili = _recipe(session, household.id, "Chili", servings=6)
    place(chili.id, date(2026, 6, 1), "dinner", session)
    session.commit()
    # Move from Mon dinner to Wed lunch
    move(date(2026, 6, 1), "dinner", date(2026, 6, 3), "lunch", session)
    session.commit()
    slots = sorted(
        session.exec(select(SlotAssignment)).all(),
        key=lambda s: s.date,
    )
    assert [s.date for s in slots] == [
        date(2026, 6, 3), date(2026, 6, 4), date(2026, 6, 5),
    ]
    assert all(s.meal == "lunch" for s in slots)


def test_move_preserves_servings_override(
    session: Session, household: Household,
) -> None:
    """If the session was placed with override=8 (4 slots), the move
    keeps those 4 slots at the new location."""
    r = _recipe(session, household.id, "Big", servings=2)
    place(r.id, date(2026, 6, 1), "dinner", session, servings_override=8)
    session.commit()
    assert len(session.exec(select(SlotAssignment)).all()) == 4
    move(date(2026, 6, 1), "dinner", date(2026, 6, 10), "dinner", session)
    session.commit()
    new_slots = session.exec(select(SlotAssignment)).all()
    assert len(new_slots) == 4  # override preserved


def test_move_conflict_raises(session: Session, household: Household) -> None:
    a = _recipe(session, household.id, "A", servings=2)
    b = _recipe(session, household.id, "B", servings=2)
    place(a.id, date(2026, 6, 1), "lunch", session)
    place(b.id, date(2026, 6, 2), "lunch", session)
    session.commit()
    with pytest.raises(ValueError, match="already taken"):
        move(date(2026, 6, 1), "lunch", date(2026, 6, 2), "lunch", session)


def test_move_to_own_leftover_slot_is_ok(
    session: Session, household: Household,
) -> None:
    """If the destination is one of the session's OWN leftover slots,
    it's not a conflict (we'd just be shifting forward into our own
    tail)."""
    chili = _recipe(session, household.id, "Chili", servings=6)
    place(chili.id, date(2026, 6, 1), "dinner", session)
    session.commit()
    # Move Mon → Tue (where Tue is currently a leftover slot of this session)
    move(date(2026, 6, 1), "dinner", date(2026, 6, 2), "dinner", session)
    session.commit()
    slots = sorted(
        s.date for s in session.exec(select(SlotAssignment)).all()
    )
    assert slots == [date(2026, 6, 2), date(2026, 6, 3), date(2026, 6, 4)]


def test_clear_window_removes_sessions_touching_range(
    session: Session, household: Household,
) -> None:
    chili = _recipe(session, household.id, "Chili", servings=6)
    pasta = _recipe(session, household.id, "Pasta", servings=2)
    place(chili.id, date(2026, 6, 1), "dinner", session)  # 3 slots
    place(pasta.id, date(2026, 6, 5), "lunch", session)   # 1 slot
    place(pasta.id, date(2026, 7, 1), "dinner", session)  # outside window
    session.commit()

    removed = clear_window(date(2026, 6, 1), 7, session)
    session.commit()
    assert removed == 2  # chili + first pasta gone, second pasta kept
    remaining = session.exec(select(CookingSession)).all()
    assert len(remaining) == 1
    assert remaining[0].cook_date == date(2026, 7, 1)


def test_clear_window_removes_spillover_sessions(
    session: Session, household: Household,
) -> None:
    """A session cooked BEFORE the window with leftovers spilling
    INTO it gets removed entirely — leaving cook+head-leftovers
    behind would be surprising after 'clear this view'."""
    chili = _recipe(session, household.id, "Chili", servings=6)
    place(chili.id, date(2026, 6, 1), "dinner", session)  # Mon/Tue/Wed
    session.commit()
    # Window covers only Wed+; the cook is Monday but Wed slot is in range
    removed = clear_window(date(2026, 6, 3), 3, session)
    session.commit()
    assert removed == 1
    assert session.exec(select(SlotAssignment)).all() == []


def test_clear_window_empty_returns_zero(
    session: Session, household: Household,
) -> None:
    assert clear_window(date(2026, 6, 1), 7, session) == 0


def test_patch_household_updates_servings_per_meal(
    session: Session, household: Household,
) -> None:
    from fastapi.testclient import TestClient
    from mise.main import app
    from mise.db import get_session

    def _override():
        yield session
    app.dependency_overrides[get_session] = _override
    try:
        client = TestClient(app)
        r = client.patch(
            "/admin/household",
            json={"default_servings_per_meal": 4},
        )
        assert r.status_code == 200, r.text
        assert r.json()["default_servings_per_meal"] == 4
        session.refresh(household)
        assert household.default_servings_per_meal == 4

        # Reject zero / negative
        r = client.patch(
            "/admin/household",
            json={"default_servings_per_meal": 0},
        )
        assert r.status_code == 400
    finally:
        app.dependency_overrides.clear()


def test_place_truncates_at_horizon(session: Session, household: Household) -> None:
    """20-serving recipe placed near the edge runs out of days, doesn't
    crash. v0 horizon is 14 days."""
    r = _recipe(session, household.id, "Stew", servings=20)
    place(r.id, date(2026, 6, 1), "dinner", session, horizon_days=5)
    session.commit()
    slots = session.exec(select(SlotAssignment)).all()
    # Days 1-6 = 6 dinner slots available (start_date + 0..5 = 6 days inclusive).
    # Recipe wants 10 slots; truncate at 6.
    assert len(slots) == 6


# --- free-text notes --------------------------------------------------------


def test_note_shows_in_view_and_blocks_slot(session: Session, household: Household) -> None:
    from mise.services.planner import add_note
    r = _recipe(session, household.id, "R", 2)
    d = date(2026, 10, 12)
    add_note(d, "dinner", "  Leftover Thanksgiving   dinner ", session)
    view = get_view(d, 1, session)
    slot = next(s for s in view[0].slots if s.meal == "dinner")
    assert slot.note == "Leftover Thanksgiving dinner"
    assert slot.recipe_id is None
    with pytest.raises(ValueError, match="already taken"):
        place(r.id, d, "dinner", session)
    with pytest.raises(ValueError, match="already taken"):
        add_note(d, "dinner", "again", session)


def test_note_rejects_blank_and_occupied(session: Session, household: Household) -> None:
    from mise.services.planner import add_note
    r = _recipe(session, household.id, "R", 2)
    d = date(2026, 10, 12)
    with pytest.raises(ValueError, match="empty"):
        add_note(d, "dinner", "   ", session)
    place(r.id, d, "dinner", session)
    with pytest.raises(ValueError, match="already taken"):
        add_note(d, "dinner", "Eating out", session)


def test_leftovers_flow_around_a_note(session: Session, household: Household) -> None:
    from mise.services.planner import add_note
    r = _recipe(session, household.id, "R", 4)  # 2 slots at 2/slot
    d = date(2026, 10, 12)
    add_note(d + timedelta(days=1), "dinner", "Eating out", session)
    place(r.id, d, "dinner", session)
    dates = sorted(s.date for s in session.exec(select(SlotAssignment)).all())
    assert dates == [d, d + timedelta(days=2)]


def test_unplace_and_clear_remove_notes(session: Session, household: Household) -> None:
    from mise.services.planner import add_note
    d = date(2026, 10, 12)
    add_note(d, "dinner", "Eating out", session)
    assert unplace(d, "dinner", session) is True
    assert get_view(d, 1, session)[0].slots[-1].note is None
    add_note(d, "dinner", "Eating out", session)
    clear_window(d, 7, session)
    assert all(s.note is None for s in get_view(d, 1, session)[0].slots)


def test_note_endpoint_and_plan_page(client, session: Session, household: Household) -> None:
    today = date.today().isoformat()
    resp = client.post("/plan/note", json={"date": today, "meal": "dinner", "text": "Leftover <b>turkey</b>"})
    assert resp.status_code == 201
    assert client.post("/plan/note", json={"date": today, "meal": "dinner", "text": "x"}).status_code == 409
    assert client.post("/plan/note", json={"date": today, "meal": "lunch", "text": " "}).status_code == 400
    page = client.get("/app/plan").text
    assert "Leftover &lt;b&gt;turkey&lt;/b&gt;" in page
    assert 'id="note-form"' in page
    assert client.delete(f"/plan/{today}/dinner").status_code == 204
    assert "turkey" not in client.get("/app/plan").text


def test_plan_page_shows_make_ahead_for_next_day_lunch(client, session: Session, household: Household) -> None:
    from datetime import date as _d
    today = _d.today()
    salad = _recipe(session, household.id, "Big Salad", 4)   # cook + 1 leftover
    stew = _recipe(session, household.id, "Stew", 2)
    place(salad.id, today + timedelta(days=2), "lunch", session)
    place(stew.id, today + timedelta(days=1), "dinner", session)
    session.commit()
    page = client.get("/app/plan").text
    # Only the cook lunch gets a reminder — its leftover (day 3) does not.
    assert page.count("class='make-ahead") == 1
    weekday = (today + timedelta(days=2)).strftime("%A")
    assert f"for {weekday}’s lunch" in page
    # ...and it sits in the previous day's card, after that day's dinner.
    assert page.index("Stew</a>") < page.index("class='make-ahead")


def test_plan_page_make_ahead_on_last_visible_day(client, session: Session, household: Household) -> None:
    from datetime import date as _d
    r = _recipe(session, household.id, "Day Eight Lunch", 2)
    place(r.id, _d.today() + timedelta(days=7), "lunch", session)
    session.commit()
    assert client.get("/app/plan").text.count("class='make-ahead") == 1


def test_shop_page_sources_and_done_sections_sink(client, session: Session, household: Household) -> None:
    from datetime import date as _d
    from mise.models import ShopCheck
    r = _recipe(session, household.id, "Source Soup", 2)
    sec = IngredientSection(recipe_id=r.id, name=None, order=0)
    session.add(sec); session.flush()
    for n, (name, cat) in enumerate([("apple", "Produce"), ("milk", "Dairy")]):
        session.add(Ingredient(
            section_id=sec.id, order=n, qty=1, unit="ea", display_name=name,
            canonical_name=name, category=cat, raw_text=name,
        ))
    place(r.id, _d.today(), "dinner", session)
    session.commit()
    page = client.get("/app/shop").text
    # Single-source rows get the "which recipe" toggle + a linked source line.
    assert page.count("class='shop-src-toggle") == 2
    assert f"/app/recipe/{r.id}' class='text-amber-700 hover:underline'>Source Soup</a>" in page
    assert page.index(">Dairy</h3>") < page.index(">Produce</h3>")
    # Tick everything in Dairy -> that section renders below Produce.
    session.add(ShopCheck(household_id=household.id, list_key="week", item_key="milk|ea"))
    session.commit()
    page = client.get("/app/shop").text
    assert page.index(">Produce</h3>") < page.index(">Dairy</h3>")


def _planned_two_recipes(session, household):
    from datetime import date as _d
    out = []
    for n, (title, ings) in enumerate([
        ("Soup A", [("apple", "Produce"), ("milk", "Dairy")]),
        ("Soup B", [("apple", "Produce")]),
    ]):
        r = _recipe(session, household.id, title, 2)
        sec = IngredientSection(recipe_id=r.id, name=None, order=0)
        session.add(sec); session.flush()
        for k, (name, cat) in enumerate(ings):
            session.add(Ingredient(
                section_id=sec.id, order=k, qty=1 + n, unit="ea", display_name=name,
                canonical_name=name, category=cat, raw_text=name,
            ))
        place(r.id, _d.today() + timedelta(days=n), "dinner", session)
        out.append(r)
    session.commit()
    return out


def test_shop_by_recipe_view(client, session: Session, household: Household) -> None:
    a, b = _planned_two_recipes(session, household)
    page = client.get("/app/shop?view=recipe").text
    assert f"data-cat='recipe-{a.id}'" in page and f"data-cat='recipe-{b.id}'" in page
    # apple is shared: one key, shown under both, each with its own amount.
    assert page.count("data-key='apple|ea'") == 2
    assert "also for Soup B" in page and "also for Soup A" in page
    assert ">1</span>" in page and ">2</span>" in page
    # Default view still groups by aisle with the aggregate (3 ea).
    agg = client.get("/app/shop").text
    assert agg.count("data-key='apple|ea'") == 1 and "id='shop-view'" in agg


def test_recipe_page_have_and_need_marks(client, session: Session, household: Household) -> None:
    from mise.models import ShopCheck
    a, b = _planned_two_recipes(session, household)
    page = client.get(f"/app/recipe/{a.id}").text
    assert page.count("need-mark") == 2 and "have-mark" not in page
    assert "Still to buy: 2 of 2" in page
    session.add(ShopCheck(household_id=household.id, list_key="week", item_key="apple|ea"))
    session.commit()
    page = client.get(f"/app/recipe/{a.id}").text
    assert page.count("need-mark") == 1 and page.count("have-mark") == 1
    assert "Still to buy: 1 of 2" in page
    assert "You have everything" in client.get(f"/app/recipe/{b.id}").text
    # Not planned -> no marks at all.
    c = _recipe(session, household.id, "Unplanned", 2)
    assert "shop-summary" not in client.get(f"/app/recipe/{c.id}").text


def test_check_off_whole_recipe(client, session: Session, household: Household) -> None:
    a, b = _planned_two_recipes(session, household)
    r = client.post(f"/app/shop/recipe/{a.id}/shopped", json={"shopped": True})
    assert r.status_code == 200 and r.json()["changed"] == 1
    page = client.get("/app/shop").text
    # Soup A's lines drop out; the shared apple shrinks to Soup B's 2 ea.
    assert "data-key='milk|ea'" not in page
    assert ">2<" in page and ">3<" not in page
    # Its chip stays, ticked, so it can be unticked.
    assert f"data-recipe='{a.id}' data-shopped='1'" in page
    assert f"data-recipe='{b.id}' data-shopped='0'" in page
    assert "You have everything" in client.get(f"/app/recipe/{a.id}").text
    client.post(f"/app/shop/recipe/{a.id}/shopped", json={"shopped": False})
    assert "data-key='milk|ea'" in client.get("/app/shop").text
    assert client.post("/app/shop/recipe/99999/shopped", json={"shopped": True}).status_code == 404


def test_recipe_share_text(client, session: Session, household: Household) -> None:
    import html as _html
    a, _b = _planned_two_recipes(session, household)
    a.source_url = "https://example.com/soup"
    session.add(a); session.commit()
    page = client.get(f"/app/recipe/{a.id}").text
    assert "id=\"share-btn\"" in page
    assert r"'\n\nIn Mise: '" in page  # JS escape, not a raw newline
    start = page.index('data-text="') + len('data-text="')
    text = _html.unescape(page[start:page.index('"', start)])
    assert text.startswith("Soup A\nServes 2")
    assert "• 1 apple" in text and "• 1 milk" in text
    assert text.rstrip().endswith("Original: https://example.com/soup")


def test_recipe_page_offers_existing_tags(client, session: Session, household: Household) -> None:
    import html as _html, json as _json
    a, b = _planned_two_recipes(session, household)
    c = _recipe(session, household.id, "Soup C", 2)
    a.tags = ["Soup", "Quick"]; b.tags = ["Soup"]; c.tags = ["Nonni", "Favourites"]
    session.add_all([a, b, c]); session.commit()
    page = client.get(f"/app/recipe/{c.id}").text
    start = page.index('data-all-tags="') + len('data-all-tags="')
    all_tags = _json.loads(_html.unescape(page[start:page.index('"', start)]))
    assert all_tags[0] == "Soup"  # most used first
    assert set(all_tags) == {"Soup", "Quick", "Nonni", "Favourites"}
    assert 'id="tag-menu"' in page
