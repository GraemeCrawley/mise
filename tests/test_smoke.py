"""Smoke tests — schema is consistent, app starts, healthz works.
Real tests land alongside their domain code."""

from __future__ import annotations

from datetime import date

from fastapi.testclient import TestClient
from sqlmodel import Session, select

from mise.models import CookingSession, Household, Recipe


def test_healthz(client: TestClient) -> None:
    r = client.get("/healthz")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok"
    assert "version" in body


def test_schema_roundtrip(session: Session) -> None:
    """Insert household → recipe → cooking session. If this passes,
    the foreign keys + JSON columns + relationships are wired right."""
    h = Household(name="Test")
    session.add(h)
    session.commit()
    session.refresh(h)

    r = Recipe(
        household_id=h.id,
        name_original="Pad Thai",
        servings=4,
    )
    session.add(r)
    session.commit()
    session.refresh(r)

    s = CookingSession(
        household_id=h.id,
        recipe_id=r.id,
        cook_date=date(2026, 6, 1),
        cook_meal="dinner",
    )
    session.add(s)
    session.commit()

    rows = session.exec(select(CookingSession)).all()
    assert len(rows) == 1
    assert rows[0].cook_meal == "dinner"
