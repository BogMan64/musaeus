#!/usr/bin/env python3
"""
MUSAEUS — Database
Single shared SQLite connection with WAL mode.
Event log is the source of truth — the archive table is derived from it.
Rebuilding the DB from scratch is always possible.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

# ── Schema ────────────────────────────────────────────────────────────────────

_SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;
PRAGMA synchronous=NORMAL;

-- Immutable event log: every mutation appended, never updated.
CREATE TABLE IF NOT EXISTS events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id      TEXT NOT NULL,
    ts          TEXT DEFAULT (datetime('now')),
    event_type  TEXT NOT NULL,
    file_path   TEXT,
    old_value   TEXT,
    new_value   TEXT,
    stage       TEXT,
    note        TEXT
);
CREATE INDEX IF NOT EXISTS idx_events_run   ON events(run_id);
CREATE INDEX IF NOT EXISTS idx_events_path  ON events(file_path);
CREATE INDEX IF NOT EXISTS idx_events_type  ON events(event_type);

-- Archive: materialized view of the current state of every known file.
-- Rebuild with: musaeus rebuild-db
CREATE TABLE IF NOT EXISTS archive (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    file_path       TEXT UNIQUE NOT NULL,
    audio_hash      TEXT,           -- content-addressed: audio stream only
    full_hash       TEXT,           -- SHA-256 of whole file (for change detection)
    filename        TEXT,
    ext             TEXT,
    size_bytes      INTEGER,
    artist          TEXT,
    album           TEXT,
    title           TEXT,
    genre           TEXT,
    year            TEXT,
    track           INTEGER,
    duration        REAL,
    bitrate         INTEGER,
    sample_rate     INTEGER,
    channels        INTEGER,
    codec           TEXT,
    status          TEXT DEFAULT 'PENDING',
    date_added      TEXT DEFAULT (datetime('now')),
    last_seen       TEXT DEFAULT (datetime('now')),
    last_modified   TEXT
);
CREATE INDEX IF NOT EXISTS idx_archive_hash     ON archive(audio_hash);
CREATE INDEX IF NOT EXISTS idx_archive_artist   ON archive(artist);
CREATE INDEX IF NOT EXISTS idx_archive_status   ON archive(status);

-- Duplicates: staged duplicate pairs for review/resolution.
CREATE TABLE IF NOT EXISTS duplicates (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    group_id        TEXT NOT NULL,
    file_path       TEXT NOT NULL,
    duplicate_type  TEXT,           -- EXACT, NEAR, CROSS_FORMAT, LIKELY_ALT_VERSION
    confidence      REAL,
    status          TEXT DEFAULT 'pending',  -- pending | keep | archive | review
    run_id          TEXT,
    staged_at       TEXT DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_dupes_group  ON duplicates(group_id);
CREATE INDEX IF NOT EXISTS idx_dupes_path   ON duplicates(file_path);

-- Validation issues logged by the Bouncer stage.
CREATE TABLE IF NOT EXISTS validation_issues (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    file_path   TEXT NOT NULL,
    issue       TEXT NOT NULL,
    severity    TEXT DEFAULT 'warning',
    run_id      TEXT,
    checked_at  TEXT DEFAULT (datetime('now')),
    UNIQUE(file_path, issue, run_id)            -- no duplicate rows on re-run
);

-- Metadata cache: raw ffprobe results, always rebuildable.
CREATE TABLE IF NOT EXISTS metadata_cache (
    file_path       TEXT PRIMARY KEY,
    title           TEXT,
    artist          TEXT,
    album           TEXT,
    genre           TEXT,
    year            TEXT,
    track           INTEGER,
    duration        REAL,
    bitrate         INTEGER,        -- always stored as INTEGER
    sample_rate     INTEGER,
    channels        INTEGER,
    codec           TEXT,
    raw_json        TEXT,
    scanned_at      TEXT DEFAULT (datetime('now'))
);
"""


# ── Connection factory ────────────────────────────────────────────────────────

def open_db(db_path: Path) -> sqlite3.Connection:
    """
    Open (or create) the Musaeus SQLite DB.
    - WAL mode for crash safety + concurrent reads
    - Row factory for dict-style access
    - Schema applied on first open
    Returns an open connection. Caller is responsible for closing.
    """
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path), timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=10000")
    conn.executescript(_SCHEMA)
    conn.commit()
    return conn


# ── Event log helpers ─────────────────────────────────────────────────────────

def log_event(
    conn: sqlite3.Connection,
    run_id: str,
    event_type: str,
    file_path: str | None = None,
    old_value: str | None = None,
    new_value: str | None = None,
    stage: str | None = None,
    note: str | None = None,
) -> None:
    """Append one event to the immutable event log."""
    conn.execute(
        """
        INSERT INTO events (run_id, event_type, file_path, old_value, new_value, stage, note)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (run_id, event_type, file_path, old_value, new_value, stage, note),
    )


def get_file_history(conn: sqlite3.Connection, file_path: str) -> list[sqlite3.Row]:
    """Return all events for a given file path, oldest first."""
    return conn.execute(
        "SELECT * FROM events WHERE file_path = ? ORDER BY id",
        (file_path,),
    ).fetchall()


# ── Archive helpers ───────────────────────────────────────────────────────────

def upsert_archive(conn: sqlite3.Connection, row: dict) -> None:
    """Insert or update an archive row."""
    fields = [
        "file_path", "audio_hash", "full_hash", "filename", "ext", "size_bytes",
        "artist", "album", "title", "genre", "year", "track",
        "duration", "bitrate", "sample_rate", "channels", "codec",
        "status", "last_seen", "last_modified",
    ]
    placeholders = ", ".join("?" for _ in fields)
    updates = ", ".join(f"{f}=excluded.{f}" for f in fields if f != "file_path")
    conn.execute(
        f"""
        INSERT INTO archive ({", ".join(fields)}) VALUES ({placeholders})
        ON CONFLICT(file_path) DO UPDATE SET {updates}
        """,
        [row.get(f) for f in fields],
    )


def get_archive_count(conn: sqlite3.Connection) -> int:
    return conn.execute("SELECT COUNT(*) FROM archive").fetchone()[0]


def get_archive_by_status(conn: sqlite3.Connection, status: str) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM archive WHERE status = ? ORDER BY artist, album, title",
        (status,),
    ).fetchall()
