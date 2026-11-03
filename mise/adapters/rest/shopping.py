"""POST /shopping — aggregate ingredient lines across multiple recipes
into a grouped grocery list. Pure read; doesn't mutate anything.

Phase 0.5 shape — no scheduling, pantry, or "got it" interactions yet.
Just: give me N recipes, get one consolidated list.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import HTMLResponse
from pydantic import BaseModel
from sqlmodel import Session, select

from mise.db import get_session
from mise.models import Recipe
from mise.services.shopping_aggregator import ShoppingItem, aggregate

router = APIRouter(tags=["shopping"])


class ShoppingRequest(BaseModel):
    recipe_ids: list[int]


class ShoppingItemOut(BaseModel):
    canonical_name: str
    display_name: str
    category: str
    qty: float
    unit: str
    display_qty: str  # rendered qty+unit string; multi-part for rule 3
    source_count: int


class ShoppingResponse(BaseModel):
    items: list[ShoppingItemOut]
    recipes: list[str]   # names of the recipes included


@router.post("/shopping", response_model=ShoppingResponse)
def build_shopping_list(
    body: ShoppingRequest,
    db: Session = Depends(get_session),
) -> ShoppingResponse:
    if not body.recipe_ids:
        raise HTTPException(status_code=400, detail="recipe_ids required")

    items: list[ShoppingItem] = aggregate(body.recipe_ids, db)
    recipes = db.exec(
        select(Recipe.name_english).where(Recipe.id.in_(body.recipe_ids))
    ).all()

    return ShoppingResponse(
        items=[
            ShoppingItemOut(
                canonical_name=i.canonical_name,
                display_name=i.display_name,
                category=i.category,
                qty=i.qty,
                unit=i.unit,
                display_qty=i.display_qty or f"{i.qty} {i.unit}".strip(),
                source_count=i.source_count,
            )
            for i in items
        ],
        recipes=[r for r in recipes if r],
    )
