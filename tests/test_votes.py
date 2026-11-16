"""Planning votes: up to 4 picks per phone per week, toggling, the tally,
and the page ordering recipes by votes."""

from __future__ import annotations

from datetime import date

import pytest
from sqlmodel import Session

from mise.models import Household, Recipe
from mise.services import votes

WEEK = date(2026, 11, 23)


@pytest.fixture
def recipes(session: Session) -> list[Recipe]:
    h = Household(name="Test", timezone="America/Toronto")
    session.add(h)
    session.commit()
    rs = [Recipe(household_id=h.id, name_original=f"R{i}", servings=2) for i in range(6)]
    session.add_all(rs)
    session.commit()
    return rs


def test_week_of_is_monday():
    assert votes.week_of(date(2026, 11, 26)) == WEEK


def test_toggle_and_limit(session, recipes):
    for r in recipes[:4]:
        votes.toggle(session, WEEK, "phoneA", r.id)
    with pytest.raises(ValueError):
        votes.toggle(session, WEEK, "phoneA", recipes[4].id)
    assert votes.toggle(session, WEEK, "phoneA", recipes[0].id) == [r.id for r in recipes[1:4]]
    assert votes.toggle(session, WEEK, "phoneA", recipes[4].id)[-1] == recipes[4].id


def test_tally_counts_phones(session, recipes):
    votes.toggle(session, WEEK, "phoneA", recipes[2].id)
    votes.toggle(session, WEEK, "phoneB", recipes[2].id)
    votes.toggle(session, WEEK, "phoneB", recipes[3].id)
    votes.toggle(session, date(2026, 11, 16), "phoneA", recipes[5].id)  # other week
    counts, voters = votes.tally(session, WEEK)
    assert counts[recipes[2].id] == 2 and counts[recipes[3].id] == 1 and voters == 2
    assert recipes[5].id not in counts


def test_vote_endpoint_uses_cookie_and_page_sets_it(client, session, recipes):
    page = client.get("/app/plan/session")
    assert page.status_code == 200 and "2 · Vote" in page.text
    assert "mise_device" in page.cookies
    r = client.post("/plan/vote", json={"week": "2026-11-25", "recipe_id": recipes[1].id})
    assert r.status_code == 200 and r.json()["mine"] == [recipes[1].id]
    client.cookies.clear()
    assert client.post("/plan/vote", json={"week": "2026-11-25", "recipe_id": recipes[1].id}).status_code == 400
