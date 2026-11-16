"""Daily empty-meal check: which meals count as gaps, one notification per
gap per phone, no repeats the same day, and the icon-number endpoint."""

from __future__ import annotations

import json
from datetime import UTC, date, datetime

import pytest
from sqlmodel import Session

from mise.models import Household, PushSubscription, Recipe
from mise.services import push as push_service
from mise.services import reminders
from mise.services.planner import add_note, place

# 2026-06-01 is a Monday; 20:00 UTC is 16:00 in Toronto, same date.
NOW = datetime(2026, 6, 1, 20, 0, tzinfo=UTC)
TODAY = date(2026, 6, 1)


@pytest.fixture(autouse=True)
def _keyfile(tmp_path, monkeypatch):
    monkeypatch.setenv("VAPID_KEY_FILE", str(tmp_path / "vapid.pem"))


@pytest.fixture
def household(session: Session) -> Household:
    h = Household(name="Test", timezone="America/Toronto")
    session.add(h)
    session.commit()
    return h


@pytest.fixture
def sent(monkeypatch):
    calls = []

    class Res:
        status_code = 201

    def fake(info, data=None, **kw):
        calls.append({"endpoint": info["endpoint"], "data": json.loads(data)})
        return Res()

    monkeypatch.setattr(push_service, "webpush", fake)
    return calls


def _phone(session, n=1):
    for i in range(n):
        session.add(PushSubscription(endpoint=f"https://web.push.apple.com/{i}", p256dh="a",
                                     auth="b", origin="https://example.test"))
    session.commit()


def _recipe(session, household) -> Recipe:
    r = Recipe(household_id=household.id, name_original="Chili", servings=2)
    session.add(r)
    session.commit()
    return r


def test_checks_tomorrow_and_day_after_lunch_and_dinner(session, household):
    gaps = reminders.empty_meals(session, TODAY)
    assert gaps == [(date(2026, 6, 2), "lunch"), (date(2026, 6, 2), "dinner"),
                    (date(2026, 6, 3), "lunch"), (date(2026, 6, 3), "dinner")]


def test_recipe_or_note_fills_a_meal(session, household):
    place(_recipe(session, household).id, date(2026, 6, 2), "dinner", session)
    add_note(date(2026, 6, 3), "lunch", "Eating out", session)
    session.commit()
    assert reminders.empty_meals(session, TODAY) == [(date(2026, 6, 2), "lunch"),
                                                      (date(2026, 6, 3), "dinner")]


def test_one_notification_per_gap_per_phone(session, household, sent):
    _phone(session, 2)
    add_note(date(2026, 6, 2), "lunch", "Away", session)
    add_note(date(2026, 6, 3), "lunch", "Away", session)
    session.commit()
    assert reminders.run_gap_check(session, NOW) == 2
    assert len(sent) == 4
    titles = sorted({c["data"]["title"] for c in sent})
    assert titles == ["Tomorrow's dinner isn't planned", "Wednesday's dinner isn't planned"]
    tomorrow = next(c["data"] for c in sent if c["data"]["title"].startswith("Tomorrow"))
    assert tomorrow["url"] == "https://example.test" + push_service.PREFIX + "/app/plan?start=2026-06-02"
    assert tomorrow["badge"] == 2


def test_same_day_rerun_does_not_repeat(session, household, sent):
    _phone(session)
    assert reminders.run_gap_check(session, NOW) == 4
    assert reminders.run_gap_check(session, NOW) == 0
    assert len(sent) == 4


def test_nothing_to_send_without_phones_or_gaps(session, household, sent):
    assert reminders.run_gap_check(session, NOW) == 0
    _phone(session)
    for d in (date(2026, 6, 2), date(2026, 6, 3)):
        for m in ("lunch", "dinner"):
            add_note(d, m, "Flex", session)
    session.commit()
    assert reminders.run_gap_check(session, NOW) == 0
    assert sent == []


def test_today_uses_household_time_zone(session, household):
    # 02:00 UTC on the 2nd is still the evening of the 1st in Toronto.
    assert reminders.household_today(session, datetime(2026, 6, 2, 2, 0, tzinfo=UTC)) == TODAY


def test_badge_endpoint_counts_gaps(client, session, household):
    assert client.get("/push/badge").json()["count"] == 4


# 2026-06-04 is a Thursday; 23:00 UTC = 19:00 Toronto.
THU = datetime(2026, 6, 4, 23, 0, tzinfo=UTC)


def test_plan_window_runs_to_next_sunday():
    assert reminders.plan_window(date(2026, 6, 4)) == (date(2026, 6, 5), date(2026, 6, 14))
    assert reminders.plan_window(date(2026, 6, 7)) == (date(2026, 6, 8), date(2026, 6, 14))


def test_plan_reminder_once_per_day_thu_to_sun(session, household, sent):
    _phone(session)
    assert reminders.run_plan_check(session, THU) is True
    assert reminders.run_plan_check(session, THU) is False  # not twice in a day
    assert len(sent) == 1
    msg = sent[0]["data"]
    assert msg["title"] == "Time to plan the week" and "20 meals" in msg["body"]
    assert msg["url"].endswith("/app/plan?start=2026-06-05") and "badge" not in msg
    assert reminders.run_plan_check(session, datetime(2026, 6, 2, 23, 0, tzinfo=UTC)) is False  # Tuesday


def test_plan_reminder_stops_when_planned(session, household, sent):
    _phone(session)
    start, end = reminders.plan_window(date(2026, 6, 4))
    d = start
    while d <= end:
        for m in ("lunch", "dinner"):
            add_note(d, m, "Flex", session)
        d = d.fromordinal(d.toordinal() + 1)
    session.commit()
    assert reminders.run_plan_check(session, THU) is False and sent == []
