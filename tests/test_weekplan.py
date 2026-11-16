"""Planning session: statuses, prep-day fill around away meals, page."""

from __future__ import annotations

from datetime import date

import pytest
from sqlmodel import Session, select

from mise.models import Household, Recipe, SlotAssignment
from mise.services import weekplan

THU = date(2026, 6, 4)


@pytest.fixture
def household(session: Session) -> Household:
    h = Household(name="T", timezone="America/Toronto")
    session.add(h)
    session.commit()
    return h


def _recipe(session, household, name, servings):
    r = Recipe(household_id=household.id, name_original=name, servings=servings)
    session.add(r)
    session.commit()
    return r


def test_windows():
    assert weekplan.rest_of_week(THU) == [date(2026, 6, 5), date(2026, 6, 6), date(2026, 6, 7)]
    assert weekplan.rest_of_week(date(2026, 6, 7)) == []
    assert weekplan.next_week(THU)[0] == date(2026, 6, 8) and len(weekplan.next_week(THU)) == 7


def test_status_cycle(session, household):
    d = date(2026, 6, 8)
    weekplan.set_status(session, d, "dinner", "away")
    weekplan.set_status(session, d, "dinner", "out")
    assert weekplan.uncovered(session, [d]) == [(d, "lunch")]
    weekplan.set_status(session, d, "dinner", "home")
    assert weekplan.uncovered(session, [d]) == [(d, "lunch"), (d, "dinner")]
    with pytest.raises(ValueError):
        weekplan.set_status(session, d, "dinner", "bogus")


def test_prep_day_skips_away_and_leftovers_flow_around_it(session, household):
    nw = weekplan.next_week(THU)
    weekplan.set_status(session, nw[0], "lunch", "away")
    weekplan.set_status(session, nw[0], "dinner", "away")
    weekplan.set_status(session, nw[3], "dinner", "out")  # Thursday dinner out
    prep = weekplan.default_prep_day(session, nw)
    assert prep == nw[1]  # Tuesday, first home dinner
    chili = _recipe(session, household, "Chili", 8)  # 4 dinners
    curry = _recipe(session, household, "Curry", 8)  # 4 lunches
    weekplan.fill_from_prep(session, prep, nw[-1], lunch=(curry.id, None), dinner=(chili.id, None))
    session.commit()
    rows = session.exec(select(SlotAssignment)).all()
    dinners = sorted(r.date for r in rows if r.meal == "dinner")
    lunches = sorted(r.date for r in rows if r.meal == "lunch")
    assert dinners == [nw[1], nw[2], nw[4], nw[5]]  # skips Thursday out
    assert lunches == [nw[2], nw[3], nw[4], nw[5]]  # from Wednesday lunch
    assert weekplan.uncovered(session, nw) == [(nw[1], "lunch"), (nw[6], "lunch"), (nw[6], "dinner")]


def test_servings_override_and_end_limit(session, household):
    nw = weekplan.next_week(THU)
    chili = _recipe(session, household, "Chili", 4)
    weekplan.fill_from_prep(session, nw[5], nw[-1], dinner=(chili.id, 12))  # 6 meals wanted, only Sat+Sun left
    session.commit()
    assert sorted(r.date for r in session.exec(select(SlotAssignment)).all()) == [nw[5], nw[6]]


def test_session_page_and_endpoints(client, session, household):
    r = _recipe(session, household, "Chili", 8)
    page = client.get("/app/plan/session")
    assert page.status_code == 200 and "Your week" in page.text and "Chili" in page.text
    nw = weekplan.next_week(date.today())
    assert client.post("/plan/status", json={"date": nw[0].isoformat(), "meal": "lunch", "status": "away"}).status_code == 200
    res = client.post("/plan/prep", json={"prep_date": nw[0].isoformat(), "end_date": nw[-1].isoformat(),
                                           "dinner_recipe_id": r.id})
    assert res.json() == {"placed": 1}
    assert client.post("/plan/prep", json={"prep_date": nw[0].isoformat(), "end_date": nw[-1].isoformat()}).status_code == 400


def test_session_page_script_parses(client, tmp_path, household):
    import re
    import shutil
    import subprocess
    node = shutil.which("node")
    if not node:
        pytest.skip("node not installed")
    js = re.findall(r"<script>(.*?)</script>", client.get("/app/plan/session").text, re.S)
    f = tmp_path / "page.js"
    f.write_text([j for j in js if "wk-cell" in j][0])
    r = subprocess.run([node, "--check", str(f)], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr


def test_next_week_joins_the_page_on_thursday():
    from datetime import date
    from mise.services.weekplan import planning_days
    mon_shown, mon_prep = planning_days(date(2026, 11, 16))
    assert mon_shown[0] == date(2026, 11, 17) and mon_shown[-1] == date(2026, 11, 22)
    assert mon_prep == mon_shown
    thu_shown, thu_prep = planning_days(date(2026, 11, 19))
    assert thu_shown[0] == date(2026, 11, 20) and thu_shown[-1] == date(2026, 11, 29)
    assert thu_prep[0] == date(2026, 11, 23)
    sun_shown, _ = planning_days(date(2026, 11, 22))
    assert sun_shown[0] == date(2026, 11, 23) and len(sun_shown) == 7
