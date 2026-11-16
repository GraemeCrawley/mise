"""Reminders. Daily empty-meal check: at 3pm, one notification per unplanned lunch or
dinner in the next two days (tomorrow and the day after, never today).

A meal counts as planned if it has a recipe or a note ("Eating out",
"Away", "Flex"). Each gap is its own notification (never merged), tapping
opens the planner at that day, and the icon number shows how many gaps remain.

Planning reminder: Thu-Sun at 7pm, while anything from tomorrow to next
Sunday is unplanned (keeps reminding; no auto-plan).
"""

from __future__ import annotations

import logging
from datetime import date as _date
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from sqlmodel import Session, select

from mise.models import Household, PushEvent, PushSubscription
from mise.services import planner as planner_service
from mise.services import push as push_service

log = logging.getLogger(__name__)

CHECK_HOUR = 15
DAYS_AHEAD = 2
CHECKED_MEALS = ("lunch", "dinner")
KIND = "gap"


# Shown on the Notifications page as per-phone switches.
REMINDER_KINDS = (
    ("gap", "Empty meals", "3pm: one per unplanned lunch or dinner in the next two days."),
    ("plan", "Time to plan", "Thu\u2013Sun 7pm, until next week is planned."),
)


def _subs_for(db: Session, kind: str) -> list[PushSubscription]:
    return [s for s in db.exec(select(PushSubscription)).all() if kind not in (s.muted_kinds or [])]


def household_today(db: Session, now: datetime | None = None) -> _date:
    h = db.exec(select(Household)).first()
    tz = ZoneInfo((h.timezone if h else None) or "UTC")
    return (now.astimezone(tz) if now else datetime.now(tz)).date()


def empty_meals(db: Session, today: _date) -> list[tuple[_date, str]]:
    """Unplanned (date, meal) pairs for tomorrow .. today+DAYS_AHEAD."""
    view = planner_service.get_view(today + timedelta(days=1), DAYS_AHEAD, db)
    return [(s.date, s.meal) for d in view for s in d.slots
            if s.meal in CHECKED_MEALS and s.recipe_id is None and not s.note]


def _day_name(d: _date, today: _date) -> str:
    return "Tomorrow" if d == today + timedelta(days=1) else d.strftime("%A")


def build_payload(sub: PushSubscription, d: _date, meal: str, today: _date,
                  event_id: str, badge: int) -> dict:
    url = f"{sub.origin or ''}{push_service.PREFIX}/app/plan?start={d.isoformat()}"
    return {
        "title": f"{_day_name(d, today)}'s {meal} isn't planned",
        "body": "Tap to pick a recipe, or add a note like “Eating out”.",
        "url": url, "tag": event_id, "badge": badge,
        "test_id": event_id, "kind": KIND,
    }


def run_gap_check(db: Session, now: datetime | None = None) -> int:
    """Send one notification per gap to every phone. Returns how many gaps
    were notified. A gap already notified today is not sent again, so a
    restart or second run on the same day doesn't double up."""
    today = household_today(db, now)
    gaps = empty_meals(db, today)
    subs = _subs_for(db, KIND)
    if not gaps or not subs:
        return 0
    sent = 0
    for d, meal in gaps:
        event_id = f"gap:{today.isoformat()}:{d.isoformat()}:{meal}"
        already = db.exec(select(PushEvent).where(PushEvent.test_id == event_id,
                                                  PushEvent.stage == "sent")).first()
        if already:
            continue
        for sub in subs:
            push_service.send(db, sub, build_payload(sub, d, meal, today, event_id, len(gaps)),
                              test_id=event_id, kind=KIND)
        sent += 1
    return sent


# ---------------------------------------------------------------- planning

PLAN_HOUR = 19
PLAN_WEEKDAYS = (3, 4, 5, 6)  # Thu, Fri, Sat, Sun — keeps reminding until planned
PLAN_KIND = "plan"


def plan_window(today: _date) -> tuple[_date, _date]:
    """Tomorrow through the Sunday of next week (rest of this week first)."""
    next_sunday = today + timedelta(days=(6 - today.weekday()) + 7)
    return today + timedelta(days=1), next_sunday


def unplanned_until_next_week(db: Session, today: _date) -> list[tuple[_date, str]]:
    start, end = plan_window(today)
    view = planner_service.get_view(start, (end - start).days + 1, db)
    return [(s.date, s.meal) for d in view for s in d.slots
            if s.meal in CHECKED_MEALS and s.recipe_id is None and not s.note]


def run_plan_check(db: Session, now: datetime | None = None) -> bool:
    """Thu-Sun 7pm: one 'time to plan' notification while any lunch or dinner
    from tomorrow to next Sunday is empty. Once per day at most."""
    today = household_today(db, now)
    if today.weekday() not in PLAN_WEEKDAYS:
        return False
    gaps = unplanned_until_next_week(db, today)
    subs = _subs_for(db, PLAN_KIND)
    if not gaps or not subs:
        return False
    event_id = f"plan:{today.isoformat()}"
    if db.exec(select(PushEvent).where(PushEvent.test_id == event_id,
                                       PushEvent.stage == "sent")).first():
        return False
    _, end = plan_window(today)
    n = len(gaps)
    for sub in subs:
        url = f"{sub.origin or ''}{push_service.PREFIX}/app/plan?start={(today + timedelta(days=1)).isoformat()}"
        push_service.send(db, sub, {
            "title": "Time to plan the week",
            "body": f"{n} {'meal isn' if n == 1 else 'meals aren'}’t planned between tomorrow "
                    f"and Sunday {end.strftime('%b %-d')}. Tap to plan.",
            "url": url, "tag": event_id, "test_id": event_id, "kind": PLAN_KIND,
        }, test_id=event_id, kind=PLAN_KIND)
    return True


def _plan_job() -> None:
    from mise.db import engine
    try:
        with Session(engine()) as db:
            run_plan_check(db)
    except Exception:
        log.exception("plan check failed")


def _job() -> None:
    from mise.db import engine
    try:
        with Session(engine()) as db:
            n = run_gap_check(db)
        log.info("gap check: %d notified", n)
    except Exception:
        log.exception("gap check failed")


def register(scheduler) -> None:
    """Add the 3pm job to the app's scheduler, in the household's time zone."""
    from apscheduler.triggers.cron import CronTrigger

    from mise.db import engine
    with Session(engine()) as db:
        h = db.exec(select(Household)).first()
        tz = (h.timezone if h else None) or "UTC"
    scheduler.add_job(_job, CronTrigger(hour=CHECK_HOUR, minute=0, timezone=tz),
                      id="gap-check", replace_existing=True, misfire_grace_time=3600)
    scheduler.add_job(_plan_job, CronTrigger(day_of_week="thu,fri,sat,sun", hour=PLAN_HOUR,
                                             minute=0, timezone=tz),
                      id="plan-check", replace_existing=True, misfire_grace_time=3600)
    log.info("gap check scheduled daily at %d:00 %s", CHECK_HOUR, tz)
