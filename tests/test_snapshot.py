"""Tests for the SQLite snapshot service.

Snapshot writes a real file via sqlite3.backup. Rotation is a pure
file-list operation. We don't unit-test the APScheduler timing —
that's APScheduler's problem.
"""

from __future__ import annotations

import sqlite3
import time
from pathlib import Path

import pytest


@pytest.fixture
def isolated_env(tmp_path: Path, monkeypatch) -> Path:
    """Point DATABASE_URL at a tmp file + snapshots at a tmp dir."""
    db_path = tmp_path / "mise.db"
    # Make a real file to back up
    conn = sqlite3.connect(str(db_path))
    conn.execute("CREATE TABLE foo (id INTEGER PRIMARY KEY, name TEXT)")
    conn.execute("INSERT INTO foo (name) VALUES ('a'), ('b'), ('c')")
    conn.commit()
    conn.close()

    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{db_path}")
    monkeypatch.setenv("SNAPSHOT_DIR", str(tmp_path / "snapshots"))
    # Force re-import so DATABASE_URL is re-read
    import importlib

    import mise.db
    importlib.reload(mise.db)
    import mise.services.snapshot
    importlib.reload(mise.services.snapshot)

    return tmp_path


def test_snapshot_writes_a_backup_file(isolated_env: Path) -> None:
    from mise.services.snapshot import snapshot_once

    out = snapshot_once()
    assert out.exists()
    assert out.name.startswith("mise-")
    assert out.suffix == ".db"
    # Verify it actually contains our test data
    conn = sqlite3.connect(str(out))
    rows = conn.execute("SELECT name FROM foo").fetchall()
    conn.close()
    assert {r[0] for r in rows} == {"a", "b", "c"}


def test_snapshot_rotation_keeps_only_most_recent(isolated_env: Path, monkeypatch) -> None:
    monkeypatch.setenv("SNAPSHOT_RETAIN", "2")
    import importlib

    import mise.services.snapshot
    importlib.reload(mise.services.snapshot)
    from mise.services.snapshot import snapshot_once

    snapshot_once()
    time.sleep(1.05)  # ensure distinct mtimes + filenames (1s resolution)
    snapshot_once()
    time.sleep(1.05)
    snapshot_once()

    out_dir = isolated_env / "snapshots"
    files = sorted(out_dir.glob("mise-*.db"))
    assert len(files) == 2  # 3rd snapshot triggered rotation of oldest


def test_snapshot_raises_when_source_missing(isolated_env: Path, monkeypatch) -> None:
    """Snapshot wants a real source DB; missing → FileNotFoundError."""
    monkeypatch.setenv("DATABASE_URL", "sqlite:////definitely/does/not/exist.db")
    import importlib

    import mise.db
    importlib.reload(mise.db)
    import mise.services.snapshot
    importlib.reload(mise.services.snapshot)
    from mise.services.snapshot import snapshot_once

    with pytest.raises(FileNotFoundError):
        snapshot_once()
