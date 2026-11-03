"""Meal planner.

Pure-function placement on a (date, meal) grid. Doesn't know about
HTTP, doesn't talk to the LLM. Same shape as shopping_aggregator —
consumed by adapters/rest/planner.py for the JSON + HTML surfaces.

Core concepts (from docs/SPEC.md §planner):
  - CookingSession = one act of cooking; owns batch + servings/slot
  - SlotAssignment = projection onto a (date, meal) cell;
    position 1 = cook, position 2+ = leftovers
  - Leftovers fill forward through SAME-meal slots only
    (dinner leftovers → tomorrow's dinner, never tomorrow's lunch)
  - Truncate at the planning horizon if leftovers overflow it

V0 deliberately defers: portion overrides, skipped/consumed marking,
templates, swap/move. The placement primitive is pure and small;
those features layer on top.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date as _date, timedelta
from math import ceil

from sqlmodel import Session, select

from mise.models import CookingSession, Household, PlanNote, Recipe, SlotAssignment


@dataclass
class SlotView:
    date: _date
    meal: str
    recipe_id: int | None
    recipe_name: str | None
    session_id: int | None
    position: int | None  # 1 = cook, 2+ = leftovers, None = empty
    # Math the UI surfaces so the user can see *why* a recipe filled
    # the slots it did. Both None for empty slots.
    recipe_servings: int | None = None
    servings_per_slot: int | None = None
    total_slots: int | None = None  # how many slots this session occupies
    note: str | None = None  # free-text entry (no recipe) — see PlanNote


@dataclass
class DayView:
    date: _date
    slots: list[SlotView]  # ordered by household.meals


def _household(db: Session) -> Household:
    """Single-tenant v1 — there's exactly one household. Materialise it
    lazily so an empty DB still works."""
    h = db.exec(select(Household)).first()
    if h is None:
        h = Household(name="Home")
        db.add(h)
        db.flush()
    return h


def _slots_to_fill(
    recipe: Recipe, household: Household, *,
    total_servings: int | None = None,
) -> int:
    """How many forward (date, meal) cells this recipe occupies at
    household default portion sizes. ceil() means 5 servings @ 2/slot
    still fills 3 slots (the last one is short, but it's a meal).
    Always at least 1.

    `total_servings` overrides the recipe default — used by the portion
    adjuster at placement time (US-04 'adjust this batch')."""
    per_slot = household.default_servings_per_meal or 2
    if per_slot <= 0:
        per_slot = 2
    servings = total_servings if total_servings is not None else (recipe.servings or per_slot)
    return max(1, ceil(servings / per_slot))


def _note_at(date: _date, meal: str, db: Session) -> PlanNote | None:
    return db.exec(
        select(PlanNote).where(PlanNote.date == date, PlanNote.meal == meal)
    ).first()


def _is_slot_occupied(date: _date, meal: str, db: Session) -> bool:
    # A free-text note holds its slot too, so leftover auto-fill flows
    # around "Eating out" instead of landing on top of it.
    return (
        db.exec(
            select(SlotAssignment).where(
                SlotAssignment.date == date,
                SlotAssignment.meal == meal,
            )
        ).first()
        is not None
    ) or _note_at(date, meal, db) is not None


def add_note(date: _date, meal: str, text: str, db: Session) -> PlanNote:
    """Put free text in (date, meal). Raises ValueError if the text is
    blank, the meal is unknown, or the slot is already taken."""
    text = " ".join((text or "").split())
    if not text:
        raise ValueError("note text is empty")
    household = _household(db)
    if meal not in household.meals:
        raise ValueError(
            f"unknown meal {meal!r}; household.meals = {household.meals}"
        )
    if _is_slot_occupied(date, meal, db):
        raise ValueError(f"slot ({date}, {meal}) is already taken")
    note = PlanNote(household_id=household.id, date=date, meal=meal, text=text[:200])
    db.add(note)
    db.flush()
    return note


def place(
    recipe_id: int, start_date: _date, meal: str, db: Session,
    *, horizon_days: int = 14,
    servings_override: int | None = None,
) -> CookingSession:
    """Place a recipe in (start_date, meal). Creates a CookingSession
    and N SlotAssignments (N = ceil(servings / per_slot)), filling
    forward through same-meal slots only, skipping already-occupied
    slots, stopping at horizon (start_date + horizon_days).

    `servings_override` is US-04's 'adjust this batch' — when set, the
    session's batch_multiplier is recorded so the recipe view can
    scale ingredient quantities. Doesn't alter Recipe.servings (that's
    the 'change recipe permanently' option, a separate flow).

    Raises ValueError if recipe doesn't exist, or the target slot is
    already taken — caller's job to either unplace first or surface
    the conflict to the user."""
    recipe = db.get(Recipe, recipe_id)
    if recipe is None:
        raise ValueError(f"recipe {recipe_id} not found")
    household = _household(db)
    if meal not in household.meals:
        raise ValueError(
            f"unknown meal {meal!r}; household.meals = {household.meals}"
        )
    if _is_slot_occupied(start_date, meal, db):
        raise ValueError(
            f"slot ({start_date}, {meal}) is already taken"
        )

    per_slot = household.default_servings_per_meal or 2
    batch_multiplier = 1.0
    if servings_override is not None and recipe.servings:
        batch_multiplier = round(servings_override / recipe.servings, 3)
    session = CookingSession(
        household_id=household.id,
        recipe_id=recipe_id,
        cook_date=start_date,
        cook_meal=meal,
        servings_per_slot=per_slot,
        batch_multiplier=batch_multiplier,
    )
    db.add(session)
    db.flush()

    n = _slots_to_fill(recipe, household, total_servings=servings_override)
    placed = 0
    cursor = start_date
    position = 1
    horizon_end = start_date + timedelta(days=horizon_days)
    while placed < n and cursor <= horizon_end:
        if not _is_slot_occupied(cursor, meal, db):
            db.add(SlotAssignment(
                session_id=session.id,
                date=cursor,
                meal=meal,
                position=position,
            ))
            db.flush()
            placed += 1
            position += 1
        cursor = cursor + timedelta(days=1)

    return session


def place_explicit(
    recipe_id: int,
    slots: list[tuple[_date, str]],
    db: Session,
    *,
    servings_override: int | None = None,
) -> CookingSession:
    """Place a recipe at user-picked explicit (date, meal) slots —
    skipping the auto-fill-forward logic of `place()`. The first slot
    becomes the cook (position 1); rest are leftovers in given order.

    Used by the recipe-page Add-to-plan picker when the user wants to
    scatter portions across non-contiguous days/meals (e.g. Mon DIN +
    Wed LUN + Thu LUN) instead of accepting the auto-forward fill."""
    if not slots:
        raise ValueError("must pick at least one slot")
    recipe = db.get(Recipe, recipe_id)
    if recipe is None:
        raise ValueError(f"recipe {recipe_id} not found")
    household = _household(db)
    for d, meal in slots:
        if meal not in household.meals:
            raise ValueError(
                f"unknown meal {meal!r}; household.meals = {household.meals}"
            )
        if _is_slot_occupied(d, meal, db):
            raise ValueError(f"slot ({d}, {meal}) is already taken")

    per_slot = household.default_servings_per_meal or 2
    batch_multiplier = 1.0
    if servings_override is not None and recipe.servings:
        batch_multiplier = round(servings_override / recipe.servings, 3)
    first_date, first_meal = slots[0]
    session = CookingSession(
        household_id=household.id,
        recipe_id=recipe_id,
        cook_date=first_date,
        cook_meal=first_meal,
        servings_per_slot=per_slot,
        batch_multiplier=batch_multiplier,
    )
    db.add(session)
    db.flush()
    for idx, (d, meal) in enumerate(slots, start=1):
        db.add(SlotAssignment(
            session_id=session.id, date=d, meal=meal, position=idx,
        ))
        db.flush()
    return session


def _session_at(date: _date, meal: str, db: Session) -> CookingSession | None:
    """Return the CookingSession that owns (date, meal), or None.
    Resolves through SlotAssignment so a leftover-slot tap finds the
    same session as the cook-slot tap."""
    slot = db.exec(
        select(SlotAssignment).where(
            SlotAssignment.date == date,
            SlotAssignment.meal == meal,
        )
    ).first()
    if slot is None:
        return None
    return db.get(CookingSession, slot.session_id)


def _effective_servings(session: CookingSession, db: Session) -> int | None:
    """Total servings this session was set to produce. Returns None if
    using recipe default (batch_multiplier == 1.0)."""
    if abs(session.batch_multiplier - 1.0) < 1e-6:
        return None
    recipe = db.get(Recipe, session.recipe_id)
    base = recipe.servings or session.servings_per_slot or 2
    return int(round(session.batch_multiplier * base))


def swap(
    date: _date, meal: str, new_recipe_id: int, db: Session,
    *, servings_override: int | None = None,
) -> CookingSession:
    """Replace the session at (date, meal) with a different recipe in
    the same starting slot. The cook slot's (date, meal) is preserved;
    leftover-fill recomputes from the new recipe's servings.

    Per spec US-06: 'Replace this session with a different recipe
    (same slot).' If the new recipe needs more slots than the old and
    one of those forward slots is already taken by an unrelated
    session, it gets skipped (place() does that already)."""
    src_session = _session_at(date, meal, db)
    if src_session is None:
        raise ValueError(f"no session at ({date}, {meal})")
    # Operate on the cook slot of the source session (in case caller
    # tapped a leftover row).
    start_date = src_session.cook_date
    start_meal = src_session.cook_meal
    unplace(start_date, start_meal, db)
    return place(
        new_recipe_id, start_date, start_meal, db,
        servings_override=servings_override,
    )


def move(
    date: _date, meal: str, new_date: _date, new_meal: str, db: Session,
) -> CookingSession:
    """Shift the entire session at (date, meal) to start at
    (new_date, new_meal). Same recipe, same portions; only the start
    slot changes.

    Per spec US-06: 'Whole session shifts.' If the destination cook
    slot is occupied by an unrelated session, raise ValueError so the
    caller can surface a conflict to the user."""
    src_session = _session_at(date, meal, db)
    if src_session is None:
        raise ValueError(f"no session at ({date}, {meal})")
    start_date = src_session.cook_date
    start_meal = src_session.cook_meal
    if start_date == new_date and start_meal == new_meal:
        return src_session  # no-op

    # Check destination conflict, EXCLUDING the source session
    # (because we're about to unplace it anyway).
    dst_slot = db.exec(
        select(SlotAssignment).where(
            SlotAssignment.date == new_date,
            SlotAssignment.meal == new_meal,
        )
    ).first()
    if (dst_slot is not None and dst_slot.session_id != src_session.id) or (
        _note_at(new_date, new_meal, db) is not None
    ):
        raise ValueError(
            f"destination ({new_date}, {new_meal}) is already taken"
        )

    recipe_id = src_session.recipe_id
    servings_override = _effective_servings(src_session, db)
    unplace(start_date, start_meal, db)
    return place(
        recipe_id, new_date, new_meal, db,
        servings_override=servings_override,
    )


def unplace(date: _date, meal: str, db: Session) -> bool:
    """Remove the whole CookingSession owning (date, meal). Per the
    spec: 'delete always removes whole session' — leftovers go with
    the cook. Returns True if anything was removed."""
    slot = db.exec(
        select(SlotAssignment).where(
            SlotAssignment.date == date,
            SlotAssignment.meal == meal,
        )
    ).first()
    if slot is None:
        note = _note_at(date, meal, db)
        if note is None:
            return False
        db.delete(note)
        db.flush()
        return True
    session_id = slot.session_id
    # Delete all SlotAssignments first, flush, then the CookingSession.
    # With FK enforcement now ON globally, the flush is required so
    # SQLAlchemy doesn't re-order the parent delete ahead of the
    # children deletes during autoflush and trip the FK constraint.
    siblings = db.exec(
        select(SlotAssignment).where(SlotAssignment.session_id == session_id)
    ).all()
    for s in siblings:
        db.delete(s)
    db.flush()
    session = db.get(CookingSession, session_id)
    if session is not None:
        db.delete(session)
        db.flush()
    return True


def clear_window(start: _date, days: int, db: Session) -> int:
    """Remove every CookingSession that has at least one SlotAssignment
    in [start, start+days). The whole session goes — partial removal
    inside a session never makes sense (a cook + its leftovers travel
    together). Returns count of sessions removed.

    Cleared sessions that started BEFORE the window but spill in are
    removed entirely (because leaving cook+early-leftovers behind
    after the user said 'clear this view' would be surprising)."""
    end = start + timedelta(days=days - 1)
    session_ids = list({
        s.session_id for s in db.exec(
            select(SlotAssignment).where(
                SlotAssignment.date >= start, SlotAssignment.date <= end,
            )
        ).all()
    })
    for sid in session_ids:
        for sa in db.exec(
            select(SlotAssignment).where(SlotAssignment.session_id == sid)
        ).all():
            db.delete(sa)
        db.flush()  # FK enforcement: children deletes must land first
        sess = db.get(CookingSession, sid)
        if sess is not None:
            db.delete(sess)
            db.flush()
    for note in db.exec(
        select(PlanNote).where(PlanNote.date >= start, PlanNote.date <= end)
    ).all():
        db.delete(note)
    db.flush()
    return len(session_ids)


def get_view(start: _date, days: int, db: Session) -> list[DayView]:
    """Return the planner grid: `days` days starting from `start`,
    each with one SlotView per household meal in display order."""
    household = _household(db)
    end = start + timedelta(days=days - 1)

    # Pull SlotAssignments joined with their CookingSession + Recipe.
    # One query rather than N. Session pulled in for servings_per_slot.
    assignments = db.exec(
        select(SlotAssignment, CookingSession, Recipe)
        .join(CookingSession, SlotAssignment.session_id == CookingSession.id)
        .join(Recipe, CookingSession.recipe_id == Recipe.id)
        .where(SlotAssignment.date >= start, SlotAssignment.date <= end)
    ).all()

    by_cell: dict[tuple[_date, str], tuple[SlotAssignment, CookingSession, Recipe]] = {}
    # Total slot count per session — used so cook cells can show
    # "N servings · fills X slots". Counted across ALL the session's
    # slots, even if some fall outside the current view window.
    session_ids = {s.id for _, s, _ in assignments}
    total_by_session: dict[int, int] = {}
    if session_ids:
        all_slots_for_sessions = db.exec(
            select(SlotAssignment).where(SlotAssignment.session_id.in_(session_ids))
        ).all()
        for sa in all_slots_for_sessions:
            total_by_session[sa.session_id] = total_by_session.get(sa.session_id, 0) + 1

    for slot, sess, recipe in assignments:
        by_cell[(slot.date, slot.meal)] = (slot, sess, recipe)
    notes = {
        (n.date, n.meal): n.text
        for n in db.exec(
            select(PlanNote).where(PlanNote.date >= start, PlanNote.date <= end)
        ).all()
    }

    out: list[DayView] = []
    for i in range(days):
        d = start + timedelta(days=i)
        slots: list[SlotView] = []
        for m in household.meals:
            cell = by_cell.get((d, m))
            if cell is None:
                slots.append(SlotView(
                    date=d, meal=m, recipe_id=None, recipe_name=None,
                    session_id=None, position=None,
                    note=notes.get((d, m)),
                ))
            else:
                slot, sess, recipe = cell
                # Effective servings = recipe default × batch_multiplier.
                # When the user picked 8 srv for a 4-srv recipe, the
                # math note should read 8 (the effective batch), not 4.
                eff_servings = int(round(
                    (recipe.servings or 0) * sess.batch_multiplier
                )) if recipe.servings else None
                slots.append(SlotView(
                    date=d, meal=m,
                    recipe_id=recipe.id,
                    recipe_name=recipe.display_title(),
                    session_id=slot.session_id,
                    position=slot.position,
                    recipe_servings=eff_servings,
                    servings_per_slot=sess.servings_per_slot,
                    total_slots=total_by_session.get(slot.session_id),
                ))
        out.append(DayView(date=d, slots=slots))
    return out


def planned_recipe_ids(start: _date, days: int, db: Session) -> list[int]:
    """Distinct recipe_ids placed in the window — fed to the shopping
    aggregator to build the week's grocery list."""
    end = start + timedelta(days=days - 1)
    rows = db.exec(
        select(CookingSession.recipe_id)
        .join(SlotAssignment, SlotAssignment.session_id == CookingSession.id)
        .where(SlotAssignment.date >= start, SlotAssignment.date <= end)
        .distinct()
    ).all()
    return list(rows)
