"""Shared fixtures. Ephemeral SQLite per-test ensures no cross-test
state bleeds. SQLModel.metadata.create_all is acceptable HERE (and only
here) — production schema management goes through Alembic."""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.engine import Engine
from sqlmodel import Session, SQLModel

from mise.db import get_session, make_engine
from mise.main import app


@pytest.fixture
def engine() -> Iterator[Engine]:
    # Route through make_engine() to inherit the pysqlite savepoint
    # workaround — without it, db.begin_nested() doesn't actually
    # roll back inner work on outer rollback.
    eng = make_engine("sqlite:///:memory:")
    SQLModel.metadata.create_all(eng)
    yield eng
    eng.dispose()


@pytest.fixture
def session(engine: Engine) -> Iterator[Session]:
    with Session(engine) as s:
        yield s


@pytest.fixture
def client(session: Session) -> Iterator[TestClient]:
    """TestClient with the FastAPI dependency override pointing at the
    in-memory session."""
    def _override() -> Iterator[Session]:
        yield session

    app.dependency_overrides[get_session] = _override
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()
