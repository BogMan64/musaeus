"""
ORPHEUS — Orpheus DB
Library: lib/orpheus_db.py — imported by orpheus_console.py, stage_orpheus_dedupe_review.py, orpheus_folder_case_merge.py
Purpose: Shared SQLite helpers: write_with_retry (lock-safe writes), ensure_naming_db_tables, and pipeline checkpoint/resume functions used across the pipeline.
"""

from __future__ import annotations

import logging
import sqlite3
import time
import uuid
from datetime import datetime
from pathlib import Path

_LOG = logging.getLogger(__name__)


def open_db(db_path: Path, *, timeout: float = 30.0) -> sqlite3.Connection:
    """Open the Orpheus database with safe, consistent settings.

    Always enforces:
    - WAL journal mode  → crash-safe; readers don't block writers
    - FULL synchronous  → every write is fsynced; no data loss on power cut
    - busy_timeout      → wait up to `timeout` seconds on a locked DB
    - foreign_keys ON   → referential integrity enforced
    - Row factory       → rows accessible by column name

    Use this instead of raw sqlite3.connect() everywhere so these settings
    are never accidentally omitted.  Added 2026-06-06 to close the crash-loss
    window identified during Orpheus bulletproofing.

    Note: busy_timeout is set by PRAGMA only (not Python connect timeout)
    to avoid a redundant 2× wait.  Changed 2026-06-25 per code review.
    """
    try:
        conn = sqlite3.connect(str(db_path), check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=FULL")
        conn.execute(f"PRAGMA busy_timeout={int(timeout * 1000)}")  # sole timeout mechanism
        conn.execute("PRAGMA foreign_keys=ON")
        return conn
    except sqlite3.Error as e:
        _LOG.error("Failed to open database: %s", e)
        raise


def ensure_naming_db_tables(db_path: Path | None = None) -> bool:
    try:
        # ...
        return True
    except Exception as e:
        _LOG.error("Failed to ensure naming-db tables: %s", e)
        return False


def write_with_retry(
    conn: sqlite3.Connection,
    sql: str,
    params: tuple = (),
    max_retries: int = 5,
    delay: float = 0.5,
) -> None:
    """Execute a write SQL statement with retry on 'database is locked'.

    Retries up to max_retries times with exponential backoff + jitter.
    Other OperationalError types are raised immediately (no retry).
    Changed to jittered backoff 2026-06-25 per code review.
    """
    import random

    for attempt in range(max_retries):
        try:
            conn.execute(sql, params)
            conn.commit()
            return
        except sqlite3.OperationalError as e:
            if "database is locked" in str(e).lower() and attempt < max_retries - 1:
                sleep_for = delay * (2**attempt) + random.uniform(0, 0.1)
                time.sleep(sleep_for)
            else:
                raise


# ── pipeline checkpoint / resume ─────────────────────────────────────────────


def ensure_checkpoint_tables(conn: sqlite3.Connection) -> None:
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS pipeline_checkpoints (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id       TEXT NOT NULL,
            pipeline     TEXT NOT NULL,
            step_name    TEXT NOT NULL,
            step_index   INTEGER NOT NULL,
            status       TEXT NOT NULL DEFAULT 'started',
            started_at   TEXT,
            completed_at TEXT,
            error_msg    TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_checkpoint_run
            ON pipeline_checkpoints(run_id, pipeline);
    """)


def checkpoint_start_run(conn: sqlite3.Connection, pipeline: str) -> str:
    """Start a new pipeline run. Inserts a header row and returns a unique run_id.

    The run_id combines a timestamp with a UUID fragment so concurrent or
    rapid-start runs never collide.  Fixed 2026-06-25 per code review.
    """
    run_id = datetime.now().strftime("%Y%m%d_%H%M%S") + "_" + uuid.uuid4().hex[:8]
    conn.execute(
        "INSERT INTO pipeline_checkpoints "
        "(run_id, pipeline, step_name, step_index, status, started_at) "
        "VALUES (?, ?, '_run_header', -1, 'started', datetime('now'))",
        (run_id, pipeline),
    )
    conn.commit()
    return run_id


def checkpoint_step_start(
    conn: sqlite3.Connection,
    run_id: str,
    pipeline: str,
    step_index: int,
    step_name: str,
) -> None:
    conn.execute(
        """
        INSERT INTO pipeline_checkpoints
          (run_id, pipeline, step_name, step_index, status, started_at)
        VALUES (?, ?, ?, ?, 'started', datetime('now'))
        """,
        (run_id, pipeline, step_name, step_index),
    )
    conn.commit()


def checkpoint_step_done(
    conn: sqlite3.Connection,
    run_id: str,
    pipeline: str,
    step_index: int,
) -> None:
    conn.execute(
        """
        UPDATE pipeline_checkpoints
           SET status='completed', completed_at=datetime('now')
         WHERE run_id=? AND pipeline=? AND step_index=?
        """,
        (run_id, pipeline, step_index),
    )
    conn.commit()


def checkpoint_step_failed(
    conn: sqlite3.Connection,
    run_id: str,
    pipeline: str,
    step_index: int,
    error: str,
) -> None:
    """Mark a pipeline step as failed with an error message.

    Uses named parameters so param order is independent of SET clause order.
    Fixed 2026-06-25 per code review.
    """
    conn.execute(
        """
        UPDATE pipeline_checkpoints
           SET status='failed', completed_at=datetime('now'), error_msg=:err
         WHERE run_id=:rid AND pipeline=:pipe AND step_index=:idx
        """,
        {"err": error, "rid": run_id, "pipe": pipeline, "idx": step_index},
    )
    conn.commit()


def checkpoint_find_incomplete(
    conn: sqlite3.Connection,
    pipeline: str,
) -> tuple[str, int] | None:
    """Return (run_id, last_completed_step_index) if an incomplete run exists, else None.

    Returns None when there is genuinely no prior run (not even a header row).
    Returns ('run_id', -1) when a run exists but has zero completed steps.
    This lets callers distinguish "no prior run" from "prior run with no progress."
    Fixed 2026-06-25 per code review.
    """
    run_row = conn.execute(
        """
        SELECT run_id FROM pipeline_checkpoints
         WHERE pipeline=? AND step_index=-1
         ORDER BY started_at DESC LIMIT 1
        """,
        (pipeline,),
    ).fetchone()
    if run_row is None:
        return None  # no prior run at all
    run_id = run_row[0]
    completed_row = conn.execute(
        """
        SELECT MAX(step_index) FROM pipeline_checkpoints
         WHERE run_id=? AND pipeline=? AND status='completed' AND step_index >= 0
        """,
        (run_id, pipeline),
    ).fetchone()
    last_completed = int(completed_row[0]) if completed_row and completed_row[0] is not None else -1
    return run_id, last_completed


def checkpoint_clear(conn: sqlite3.Connection, pipeline: str) -> None:
    """Mark all open runs for this pipeline as closed."""
    conn.execute(
        """
        UPDATE pipeline_checkpoints
           SET status='closed'
         WHERE pipeline=? AND status='started'
        """,
        (pipeline,),
    )
    conn.commit()
