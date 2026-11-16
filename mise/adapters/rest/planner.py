"""REST adapter for the planner service.

Thin layer — every domain decision lives in services/planner.py.
JSON-only here; the HTML grid lives in preview.py alongside the
rest of the operator UI.
"""

from __future__ import annotations

from datetime import date as _date

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel
from sqlmodel import Session

from mise.db import get_session
from mise.services import planner as planner_service

router = APIRouter(prefix="/plan", tags=["plan"])


class PlaceIn(BaseModel):
    recipe_id: int
    date: _date
    meal: str
    servings: int | None = None  # overrides recipe.servings for this session


class SessionOut(BaseModel):
    id: int
    recipe_id: int
    cook_date: _date
    cook_meal: str


@router.post("/place", response_model=SessionOut, status_code=201)
def place(body: PlaceIn, db: Session = Depends(get_session)) -> SessionOut:
    try:
        sess = planner_service.place(
            body.recipe_id, body.date, body.meal, db,
            servings_override=body.servings,
        )
        db.commit()
    except ValueError as e:
        db.rollback()
        raise HTTPException(status_code=400, detail=str(e)) from e
    return SessionOut(
        id=sess.id,
        recipe_id=sess.recipe_id,
        cook_date=sess.cook_date,
        cook_meal=sess.cook_meal,
    )


class SlotSpec(BaseModel):
    date: _date
    meal: str


class PlaceExplicitIn(BaseModel):
    recipe_id: int
    slots: list[SlotSpec]
    servings: int | None = None


@router.post("/place-explicit", response_model=SessionOut, status_code=201)
def place_explicit(
    body: PlaceExplicitIn, db: Session = Depends(get_session),
) -> SessionOut:
    """Place a recipe at explicit user-picked slots — see
    planner_service.place_explicit for semantics."""
    try:
        slots = [(s.date, s.meal) for s in body.slots]
        sess = planner_service.place_explicit(
            body.recipe_id, slots, db,
            servings_override=body.servings,
        )
        db.commit()
    except ValueError as e:
        db.rollback()
        status = 409 if "already taken" in str(e) else 400
        raise HTTPException(status_code=status, detail=str(e)) from e
    return SessionOut(
        id=sess.id,
        recipe_id=sess.recipe_id,
        cook_date=sess.cook_date,
        cook_meal=sess.cook_meal,
    )


@router.delete("/{date}/{meal}", status_code=204)
def unplace(date: _date, meal: str, db: Session = Depends(get_session)) -> None:
    removed = planner_service.unplace(date, meal, db)
    if not removed:
        raise HTTPException(status_code=404, detail="no session at that slot")
    db.commit()


@router.delete("", status_code=200)
def clear_window(
    start: _date, days: int = 7, db: Session = Depends(get_session),
) -> dict:
    """Wipe every session touching [start, start+days). Returns the
    removed count so the UI can show 'Cleared N sessions'."""
    if days < 1 or days > 30:
        raise HTTPException(status_code=400, detail="days must be 1..30")
    n = planner_service.clear_window(start, days, db)
    db.commit()
    return {"removed": n}


class SwapIn(BaseModel):
    date: _date
    meal: str
    recipe_id: int
    servings: int | None = None


@router.post("/swap", response_model=SessionOut, status_code=200)
def swap(body: SwapIn, db: Session = Depends(get_session)) -> SessionOut:
    try:
        sess = planner_service.swap(
            body.date, body.meal, body.recipe_id, db,
            servings_override=body.servings,
        )
        db.commit()
    except ValueError as e:
        db.rollback()
        raise HTTPException(status_code=400, detail=str(e)) from e
    return SessionOut(
        id=sess.id, recipe_id=sess.recipe_id,
        cook_date=sess.cook_date, cook_meal=sess.cook_meal,
    )


class MoveIn(BaseModel):
    from_date: _date
    from_meal: str
    to_date: _date
    to_meal: str


@router.post("/move", response_model=SessionOut, status_code=200)
def move(body: MoveIn, db: Session = Depends(get_session)) -> SessionOut:
    try:
        sess = planner_service.move(
            body.from_date, body.from_meal, body.to_date, body.to_meal, db,
        )
        db.commit()
    except ValueError as e:
        db.rollback()
        # 409 for conflicts (destination occupied); 400 for not-found.
        status = 409 if "already taken" in str(e) else 400
        raise HTTPException(status_code=status, detail=str(e)) from e
    return SessionOut(
        id=sess.id, recipe_id=sess.recipe_id,
        cook_date=sess.cook_date, cook_meal=sess.cook_meal,
    )


class NoteIn(BaseModel):
    date: _date
    meal: str
    text: str


class NoteOut(BaseModel):
    id: int
    date: _date
    meal: str
    text: str


@router.post("/note", response_model=NoteOut, status_code=201)
def add_note(body: NoteIn, db: Session = Depends(get_session)) -> NoteOut:
    try:
        note = planner_service.add_note(body.date, body.meal, body.text, db)
        db.commit()
    except ValueError as e:
        db.rollback()
        status = 409 if "already taken" in str(e) else 400
        raise HTTPException(status_code=status, detail=str(e)) from e
    return NoteOut(id=note.id, date=note.date, meal=note.meal, text=note.text)


class SlotOut(BaseModel):
    date: _date
    meal: str
    recipe_id: int | None
    recipe_name: str | None
    session_id: int | None
    position: int | None
    note: str | None = None


class DayOut(BaseModel):
    date: _date
    slots: list[SlotOut]


@router.get("", response_model=list[DayOut])
def get_plan(
    start: _date, days: int = 7, db: Session = Depends(get_session),
) -> list[DayOut]:
    if days < 1 or days > 30:
        raise HTTPException(status_code=400, detail="days must be 1..30")
    view = planner_service.get_view(start, days, db)
    return [
        DayOut(
            date=d.date,
            slots=[
                SlotOut(
                    date=s.date, meal=s.meal,
                    recipe_id=s.recipe_id, recipe_name=s.recipe_name,
                    session_id=s.session_id, position=s.position,
                    note=s.note,
                )
                for s in d.slots
            ],
        )
        for d in view
    ]


# ---------------------------------------------------------------- planning session

class StatusIn(BaseModel):
    date: _date
    meal: str
    status: str  # home | away | out | flex


@router.post("/status", status_code=200)
def set_status(body: StatusIn, db: Session = Depends(get_session)) -> dict:
    from mise.services import weekplan
    try:
        weekplan.set_status(db, body.date, body.meal, body.status)
        db.commit()
    except ValueError as e:
        db.rollback()
        raise HTTPException(status_code=409 if "already taken" in str(e) else 400, detail=str(e)) from e
    return {"ok": True}


class PrepIn(BaseModel):
    prep_date: _date
    end_date: _date
    lunch_recipe_id: int | None = None
    lunch_servings: int | None = None
    dinner_recipe_id: int | None = None
    dinner_servings: int | None = None


@router.post("/prep", status_code=200)
def fill_prep(body: PrepIn, db: Session = Depends(get_session)) -> dict:
    from mise.services import weekplan
    if body.lunch_recipe_id is None and body.dinner_recipe_id is None:
        raise HTTPException(status_code=400, detail="Pick a lunch or a dinner recipe.")
    try:
        sessions = weekplan.fill_from_prep(
            db, body.prep_date, body.end_date,
            lunch=(body.lunch_recipe_id, body.lunch_servings) if body.lunch_recipe_id else None,
            dinner=(body.dinner_recipe_id, body.dinner_servings) if body.dinner_recipe_id else None,
        )
        db.commit()
    except ValueError as e:
        db.rollback()
        raise HTTPException(status_code=400, detail=str(e)) from e
    return {"placed": len(sessions)}


class VoteIn(BaseModel):
    week: _date
    recipe_id: int


@router.post("/vote", status_code=200)
def vote(body: VoteIn, request: Request, db: Session = Depends(get_session)) -> dict:
    """Toggle this phone's pick. The phone is the `mise_device` cookie."""
    from mise.services import votes
    try:
        picks = votes.toggle(db, votes.week_of(body.week), request.cookies.get("mise_device", ""),
                             body.recipe_id)
        db.commit()
    except ValueError as e:
        db.rollback()
        raise HTTPException(status_code=400, detail=str(e)) from e
    return {"mine": picks}
