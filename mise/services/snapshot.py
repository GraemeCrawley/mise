"""Hourly SQLite snapshot job — forensic redundancy layer.

The exporter writes JSON per recipe (durable / portable). The snapshot
writes the whole SQLite file (operational state — pantry, SRS,
SyncRun history, parser cache). Different concerns, different cadences,
both wanted.

Implementation uses sqlite3's native `.backup()` API rather than
shelling out — atomic, race-free, no SQLite-on-PATH dependency.

Configuration via env:
  SNAPSHOT_DIR              default /data/backups
  SNAPSHOT_INTERVAL_HOURS   default 1
  SNAPSHOT_RETAIN           default 168 (7 days × 24h)

Restic / Borg push to NAS lives OUTSIDE this — host-side cron picks
up files in SNAPSHOT_DIR and ships them off-box. The container only
owns the on-disk snapshots; off-site replication is the host's job.
"""

from __future__ import annotations

import logging
import os
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger

from mise.db import DATABASE_URL

log = logging.getLogger("mise.snapshot")

DEFAULT_DIR = "/data/backups"
DEFAULT_RETAIN = 168
DEFAULT_INTERVAL_HOURS = 1


def _source_db_path() -> Path:
    """Extract filesystem path from a sqlite:/// DATABASE_URL."""
    url = DATABASE_URL
    # sqlite:////absolute  (four slashes) or sqlite:///relative
    if url.startswith("sqlite:////"):
        return Path("/" + url[len("sqlite:////") :])
    if url.startswith("sqlite:///"):
        return Path(url[len("sqlite:///") :])
    raise ValueError(f"snapshot requires sqlite DATABASE_URL, got {url!r}")


def _snapshot_dir() -> Path:
    d = Path(os.environ.get("SNAPSHOT_DIR", DEFAULT_DIR))
    d.mkdir(parents=True, exist_ok=True)
    return d


def snapshot_once() -> Path:
    """Take one snapshot via sqlite3 native backup. Returns the
    written file's path. Rotates old snapshots per SNAPSHOT_RETAIN.
    """
    src = _source_db_path()
    if not src.exists():
        raise FileNotFoundError(f"source SQLite not found: {src}")
    out_dir = _snapshot_dir()
    ts = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    out = out_dir / f"mise-{ts}.db"

    src_conn = sqlite3.connect(str(src))
    dst_conn = sqlite3.connect(str(out))
    try:
        src_conn.backup(dst_conn)
    finally:
        dst_conn.close()
        src_conn.close()

    size = out.stat().st_size
    log.info("snapshot written: %s (%d bytes)", out, size)
    _rotate(out_dir)
    return out


def _rotate(out_dir: Path) -> None:
    """Keep the N most-recent snapshots; delete the rest."""
    retain = int(os.environ.get("SNAPSHOT_RETAIN", str(DEFAULT_RETAIN)))
    if retain <= 0:
        return
    files = sorted(
        out_dir.glob("mise-*.db"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    for old in files[retain:]:
        try:
            old.unlink()
            log.info("snapshot rotated out: %s", old)
        except OSError as e:
            log.warning("snapshot rotation failed for %s: %s", old, e)


def start_scheduler() -> AsyncIOScheduler:
    """Start the APScheduler with the hourly job registered. Caller
    holds the reference and shuts it down on app shutdown."""
    sched = AsyncIOScheduler()
    hours = int(os.environ.get("SNAPSHOT_INTERVAL_HOURS", str(DEFAULT_INTERVAL_HOURS)))
    sched.add_job(
        snapshot_once,
        IntervalTrigger(hours=hours),
        id="sqlite-snapshot",
        replace_existing=True,
        misfire_grace_time=600,
    )
    sched.start()
    log.info("snapshot scheduler started: every %dh", hours)
    return sched
