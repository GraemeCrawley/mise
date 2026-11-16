"""Planning votes: each phone picks its top 4 recipes for the week being
planned; the tally orders the recipe list. Never blocks planning."""

from __future__ import annotations

from collections import Counter
from datetime import date as _date
from datetime import timedelta

from sqlmodel import Session, select

from mise.models import Recipe, RecipeVote

MAX_VOTES = 4


def week_of(d: _date) -> _date:
    return d - timedelta(days=d.weekday())


def mine(db: Session, week: _date, device: str) -> list[int]:
    return [v.recipe_id for v in db.exec(select(RecipeVote).where(
        RecipeVote.week == week, RecipeVote.device == device).order_by(RecipeVote.id)).all()]


def toggle(db: Session, week: _date, device: str, recipe_id: int) -> list[int]:
    """Add or remove one pick. Raises ValueError on a fifth pick."""
    if not device:
        raise ValueError("no phone id")
    if db.get(Recipe, recipe_id) is None:
        raise ValueError("no such recipe")
    existing = db.exec(select(RecipeVote).where(
        RecipeVote.week == week, RecipeVote.device == device,
        RecipeVote.recipe_id == recipe_id)).first()
    if existing:
        db.delete(existing)
    else:
        if len(mine(db, week, device)) >= MAX_VOTES:
            raise ValueError(f"You've picked {MAX_VOTES}. Untick one first.")
        db.add(RecipeVote(week=week, device=device, recipe_id=recipe_id))
    db.flush()
    return mine(db, week, device)


def tally(db: Session, week: _date) -> tuple[Counter, int]:
    """(votes per recipe_id, number of phones that voted)."""
    rows = db.exec(select(RecipeVote).where(RecipeVote.week == week)).all()
    return Counter(v.recipe_id for v in rows), len({v.device for v in rows})
