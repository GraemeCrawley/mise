"""Engine + session factory.

Single SQLite database per `DATABASE_URL`. The default in dev/test is an
in-memory engine; container runs against `/data/mise.db`. Alembic owns
schema migrations — never create tables from SQLModel.metadata in prod.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import event
from sqlalchemy.engine import Engine
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, create_engine

DATABASE_URL = os.environ.get("DATABASE_URL", "sqlite:///./mise.db")


def _apply_sqlite_savepoint_workaround(engine: Engine) -> None:
    """pysqlite's implicit transaction handling breaks SAVEPOINT semantics.
    Without this, db.begin_nested() emits SAVEPOINT but the outer
    rollback() doesn't actually roll back the inner work — the xlsx
    importer's per-row savepoints silently leak data.

    Documented workaround from
    https://docs.sqlalchemy.org/en/20/dialects/sqlite.html#serializable-isolation-savepoints-transactional-ddl

    Same `connect` event also turns on `PRAGMA foreign_keys=ON` so
    `ON DELETE CASCADE` constraints actually fire — SQLite's legacy
    default is to ignore FK constraints entirely.
    """
    @event.listens_for(engine, "connect")
    def _on_connect(dbapi_connection, connection_record):
        # Disable pysqlite's implicit transaction control
        dbapi_connection.isolation_level = None
        # Enable foreign-key enforcement (cascade deletes, etc.).
        # SQLite per-connection; setting it here means every checkout
        # from the pool inherits it.
        cur = dbapi_connection.cursor()
        cur.execute("PRAGMA foreign_keys=ON")
        cur.close()

    @event.listens_for(engine, "begin")
    def _on_begin(conn):
        # Issue our own BEGIN so SQLAlchemy controls the transaction
        conn.exec_driver_sql("BEGIN")


def make_engine(url: str | None = None) -> Engine:
    """Build a SQLAlchemy engine. SQLite needs:
      - check_same_thread=False (FastAPI dependency-injects across threads)
      - savepoint workaround (pysqlite breaks SAVEPOINT semantics)
      - StaticPool for in-memory DBs (each connection has its own DB,
        so tests + TestClient must share a single connection)
    """
    url = url or DATABASE_URL
    connect_args: dict = {}
    is_sqlite = url.startswith("sqlite")
    is_memory = url in ("sqlite:///:memory:", "sqlite://")
    if is_sqlite:
        connect_args["check_same_thread"] = False

    kwargs: dict = {"connect_args": connect_args, "echo": False}
    if is_memory:
        # Single shared connection — without this, tests using a
        # TestClient see "no such table" because the FastAPI request
        # gets a fresh connection (== fresh empty in-memory DB).
        kwargs["poolclass"] = StaticPool

    eng = create_engine(url, **kwargs)
    if is_sqlite:
        _apply_sqlite_savepoint_workaround(eng)
    return eng


# Module-level singleton — created lazily so tests can swap the engine.
_engine: Engine | None = None


def engine() -> Engine:
    global _engine
    if _engine is None:
        _engine = make_engine()
    return _engine


def get_session() -> Iterator[Session]:
    """FastAPI dependency. Yields a Session; closes on response."""
    with Session(engine()) as session:
        yield session


@contextmanager
def session_scope() -> Iterator[Session]:
    """For service-layer / background-job use outside the request cycle."""
    with Session(engine()) as session:
        yield session
