"""The weekly planning session: rest of this week, who's home next week,
prep day, and filling the week from two prep recipes (one for lunches, one
for dinners). Away / out / flex are plan notes, so leftovers already flow
around them and the reminders treat them as planned."""

from __future__ import annotations

from datetime import date as _date
from datetime import timedelta

from sqlmodel import Session

from mise.models import CookingSession
from mise.services import planner as planner_service

MEALS = ("lunch", "dinner")
STATUS_TEXT = {"away": "Away", "out": "Eating out", "flex": "Flex"}


def rest_of_week(today: _date) -> list[_date]:
    """Tomorrow through this Sunday (empty on a Sunday)."""
    return [today + timedelta(days=i) for i in range(1, 7 - today.weekday())]


def next_week(today: _date) -> list[_date]:
    monday = today + timedelta(days=7 - today.weekday())
    return [monday + timedelta(days=i) for i in range(7)]


def status_of(note: str | None) -> str | None:
    """'away' / 'out' / 'flex' for the quick statuses, 'note' for other text."""
    if not note:
        return None
    for key, text in STATUS_TEXT.items():
        if note.strip().lower() == text.lower():
            return key
    return "note"


def set_status(db: Session, d: _date, meal: str, status: str) -> None:
    """status: home (clears a note) | away | out | flex. Never touches a
    meal that has a recipe in it."""
    if status != "home" and status not in STATUS_TEXT:
        raise ValueError(f"unknown status {status!r}")
    note = planner_service._note_at(d, meal, db)
    if note is not None:
        db.delete(note)
        db.flush()
    if status != "home":
        planner_service.add_note(d, meal, STATUS_TEXT[status], db)


def default_prep_day(db: Session, days: list[_date]) -> _date:
    """First day whose dinner you're home for; else the first day."""
    view = planner_service.get_view(days[0], len(days), db)
    for day in view:
        dinner = next(s for s in day.slots if s.meal == "dinner")
        if status_of(dinner.note) not in ("away", "out"):
            return day.date
    return days[0]


def _first_free(db: Session, start: _date, end: _date, meal: str) -> _date | None:
    d = start
    while d <= end:
        if not planner_service._is_slot_occupied(d, meal, db):
            return d
        d += timedelta(days=1)
    return None


def fill_from_prep(db: Session, prep_day: _date, end: _date, *,
                   lunch: tuple[int, int | None] | None = None,
                   dinner: tuple[int, int | None] | None = None) -> list[CookingSession]:
    """Cook both on prep day: the dinner recipe starts that evening, the
    lunch recipe from the next day's lunch (it's made the night before).
    Leftovers fill forward up to `end`, skipping away/out/flex meals.
    Each is (recipe_id, servings or None for the recipe's own)."""
    out: list[CookingSession] = []
    for meal, pick, start in (("dinner", dinner, prep_day),
                              ("lunch", lunch, prep_day + timedelta(days=1))):
        if pick is None:
            continue
        first = _first_free(db, start, end, meal)
        if first is None:
            raise ValueError(f"no free {meal} between {start} and {end}")
        recipe_id, servings = pick
        out.append(planner_service.place(recipe_id, first, meal, db,
                                         horizon_days=(end - first).days,
                                         servings_override=servings))
    return out


def uncovered(db: Session, days: list[_date]) -> list[tuple[_date, str]]:
    view = planner_service.get_view(days[0], len(days), db)
    return [(s.date, s.meal) for d in view for s in d.slots
            if s.meal in MEALS and s.recipe_id is None and not s.note]
