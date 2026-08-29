#!/usr/bin/env python3
"""
MUSAEUS — Database
Single shared SQLite connection with WAL mode.
Event log is the source of truth — the archive table is derived from it.
Rebuilding the DB from scratch is always possible.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path

# ── Schema ────────────────────────────────────────────────────────────────────

_SCHEMA = """
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
    last_modified   TEXT,
    lufs            REAL,           -- integrated loudness (EBU R128)
    lufs_tp         REAL,           -- true peak dBTP
    rg_gain         REAL,           -- ReplayGain track gain dB
    rg_peak         REAL,           -- ReplayGain track peak (linear)
    rg_tagged_at    TEXT,           -- ISO timestamp of last RG tag write
    car_export_path TEXT,           -- path in the car-export library
    noise_profile   TEXT            -- noise variant used in car export
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

-- Bit-rot baseline for ALAC_Archive content (musaeus/stages/bitrot.py).
-- Deliberately keyed by path, not archive.id -- ALAC_Archive is itself
-- deliberately not DB-row-tracked (build_alac_library.py's own docstring:
-- avoiding a second path column that could drift out of sync with real
-- filesystem state), so this table follows the same philosophy rather
-- than fighting it. Established once via `musaeus bitrot --rebaseline`
-- (a deliberate, explicit action -- never automatic, since silently
-- re-baselining on every run would absorb real corruption into the "new
-- normal" instead of catching it), then verified against on every
-- `musaeus bitrot` run afterward.
CREATE TABLE IF NOT EXISTS archive_tier_hashes (
    path          TEXT PRIMARY KEY,
    sha256        TEXT NOT NULL,
    size_bytes    INTEGER,
    baselined_at  TEXT DEFAULT (datetime('now'))
);
"""


# ── Live migrations ───────────────────────────────────────────────────────────

# Each entry: (table, column_name, column_def)
# Applied in order every time open_db() is called — idempotent.
_MIGRATIONS: list[tuple[str, str, str]] = [
    ("archive", "lufs", "REAL"),
    ("archive", "lufs_tp", "REAL"),
    ("archive", "rg_gain", "REAL"),
    ("archive", "rg_peak", "REAL"),
    ("archive", "rg_tagged_at", "TEXT"),
    ("archive", "car_export_path", "TEXT"),
    ("archive", "noise_profile", "TEXT"),
    # Act 3 — Canonicalize + Finalize (ALAC-Library). Nullable timestamp
    # columns, same pattern as rg_tagged_at: NULL means "not done yet",
    # set means "done", stages skip already-done rows unless --force.
    ("archive", "canonicalized_at", "TEXT"),
    # What Canonicalize actually did to this file:
    #   PASSTHROUGH  — already ALAC-in-.m4a (or already AAC-in-.m4a for a
    #                  sub-lossless source), no re-encode needed
    #   CONVERTED    — lossless source (flac/wav/aiff) re-containered to
    #                  ALAC-in-.m4a, no lossy re-encode
    #   TRANSCODED   — sub-lossless source (mp3/ogg/etc) re-encoded to
    #                  256k AAC-in-.m4a; a real lossy-to-lossy transcode,
    #                  also logged to TuneMyMusic.csv
    ("archive", "canon_action", "TEXT"),
    ("archive", "finalized_at", "TEXT"),
    # Phase 2A — ALAC-Library LUFS bake (scripts/alac_library/build_alac_library.py,
    # standalone, not a pipeline stage). Same nullable-timestamp resumability
    # pattern as canonicalized_at/finalized_at. lufs_baked_target records what
    # target was actually baked to (-18.0 today), self-documenting without
    # needing to re-derive it from the script's source.
    ("archive", "lufs_baked_at", "TEXT"),
    ("archive", "lufs_baked_target", "REAL"),
    # BPM tagging (musaeus/stages/bpm.py, standalone -- not wired into
    # DEFAULT_PIPELINE, same precedent as GhostStage/PermissionsStage).
    # Same nullable-timestamp resumability pattern as the others above.
    # bpm/musical_key/energy/danceability all come from one Essentia
    # analysis pass over the same decoded audio -- ported together from
    # orpheus_audio_analyzer.py since computing just one would still pay
    # the full decode+analysis cost of the others. "key" avoided as a
    # column name (reserved-word-adjacent); "musical_key" to be
    # unambiguous.
    ("archive", "bpm", "REAL"),
    ("archive", "musical_key", "TEXT"),
    ("archive", "energy", "REAL"),
    ("archive", "danceability", "REAL"),
    ("archive", "bpm_analyzed_at", "TEXT"),
    # Bit-rot check (musaeus/stages/bitrot.py). First design (2026-08-19,
    # same night): compared against archive.full_hash. Superseded within
    # the same session once live-vault testing showed full_hash goes
    # stale for any file that passes through Canonicalize/Forge/Tagger --
    # all of which legitimately rewrite bytes AFTER Sentinel computes
    # full_hash, which is nearly every finalized file, not just baked
    # ones. full_hash is fine for what it was actually built for
    # (Sentinel's own retag-vs-audio-change detection); it was never
    # meant to survive the rest of the pipeline. bitrot_checked_at/
    # bitrot_ok are dead columns from that superseded design -- left in
    # place (SQLite ALTER TABLE can't cheaply drop columns) but unused;
    # see archive_tier_hashes below for the real mechanism.
    ("archive", "bitrot_checked_at", "TEXT"),
    ("archive", "bitrot_ok", "INTEGER"),
    # ── Columns reclaimed from per-stage ad-hoc migrations ───────────────────
    # Six stages (integrity, acousticid, transcode, mb_enrich, auditor,
    # albumart) each carried a private _ensure_columns() that ran its own
    # PRAGMA table_info + ALTER TABLE at stage-run time, bypassing this list
    # entirely. That had three real costs beyond the duplication:
    #   1. A column existed only once its owning stage had run, so ordinary
    #      SELECTs had to be wrapped in try/except and silently fall back to
    #      a DIFFERENT row set when the column was missing (acousticid's
    #      fallback dropped the `chromaprint IS NULL` filter entirely, so a
    #      first run previewed and processed every CATALOGUED row).
    #   2. _ensure_columns() called conn.commit(), which stages are
    #      explicitly forbidden from doing (see stages/base.py).
    #   3. Stages skipped it under dry_run, so a dry run took the fallback
    #      branch and previewed a different row set than the run would touch.
    # Declaring them here instead means open_db() has the full schema before
    # any stage runs, which is what README's "you never need to run manual
    # migrations" already promised.
    ("archive", "integrity_ok", "INTEGER"),
    ("archive", "integrity_checked_at", "TEXT"),
    ("archive", "chromaprint", "TEXT"),
    ("archive", "chromaprint_duration", "REAL"),
    ("archive", "acousticid_recording", "TEXT"),
    ("archive", "acousticid_score", "REAL"),
    ("archive", "acousticid_checked_at", "TEXT"),
    ("archive", "transcode_path", "TEXT"),
    ("archive", "transcode_at", "TEXT"),
    ("archive", "mb_artist_id", "TEXT"),
    ("archive", "mb_artist_name", "TEXT"),
    ("archive", "mb_release_id", "TEXT"),
    ("archive", "mb_enriched_at", "TEXT"),
    ("archive", "auditor_lufs", "REAL"),
    ("archive", "auditor_tp", "REAL"),
    ("archive", "auditor_flagged", "INTEGER DEFAULT 0"),
    ("archive", "auditor_checked_at", "TEXT"),
    ("archive", "has_art", "INTEGER"),
    ("archive", "art_checked_at", "TEXT"),
]


def _apply_migrations(conn: sqlite3.Connection) -> None:
    """Add new columns to existing tables when the schema evolves."""
    for table, col, coltype in _MIGRATIONS:
        existing = {row[1] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}
        if col not in existing:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {coltype}")
    conn.commit()


# ── Connection factory ────────────────────────────────────────────────────────


def open_db(db_path: Path) -> sqlite3.Connection:
    """
    Open (or create) the Musaeus SQLite DB.
    - WAL mode for crash safety + concurrent reads
    - Row factory for dict-style access
    - Schema applied on first open
    - Live column migrations applied on every open (idempotent)
    Returns an open connection. Caller is responsible for closing.
    """
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path), timeout=30)
    conn.row_factory = sqlite3.Row
    # Apply PRAGMAs via individual execute() calls BEFORE executescript() so
    # that busy_timeout is active when DDL tries to acquire the write lock.
    # executescript() bypasses the normal busy-timeout machinery and also
    # issues an implicit COMMIT, so PRAGMAs inside the script body are too late.
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA busy_timeout=10000")
    conn.executescript(_SCHEMA)
    conn.commit()
    _apply_migrations(conn)
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
    """
    Insert or update an archive row.

    On INSERT (new row), every field below gets a value (None where the
    caller didn't supply one — that's a normal NULL for a brand-new row).

    On UPDATE (existing row, i.e. ON CONFLICT), only fields the caller
    ACTUALLY PASSED IN `row` are overwritten. This matters because
    several callers intentionally update a narrow subset of columns —
    e.g. sentinel.py's audio_hash/status pass, or scholar.py's metadata
    pass which never mentions filename/ext at all. Before this fix, the
    UPDATE clause unconditionally set every field to `row.get(f)`, which
    is None for anything the caller omitted — so, concretely, every
    Scholar run was silently nulling out filename and ext that Ingest
    had correctly set moments earlier. Only including a field in the
    UPDATE when `f in row` fixes that without changing INSERT behaviour
    (a fresh row still gets every column, defaulting to None/NULL).
    """
    fields = [
        "file_path",
        "audio_hash",
        "full_hash",
        "filename",
        "ext",
        "size_bytes",
        "artist",
        "album",
        "title",
        "genre",
        "year",
        "track",
        "duration",
        "bitrate",
        "sample_rate",
        "channels",
        "codec",
        "status",
        "last_seen",
        "last_modified",
    ]
    placeholders = ", ".join("?" for _ in fields)
    updates = ", ".join(f"{f}=excluded.{f}" for f in fields if f != "file_path" and f in row)
    if not updates:
        # Nothing but file_path was supplied (or ON CONFLICT would be a
        # true no-op) — DO NOTHING avoids an invalid empty SET clause.
        conflict_clause = "ON CONFLICT(file_path) DO NOTHING"
    else:
        conflict_clause = f"ON CONFLICT(file_path) DO UPDATE SET {updates}"
    conn.execute(
        f"""
        INSERT INTO archive ({", ".join(fields)}) VALUES ({placeholders})
        {conflict_clause}
        """,
        [row.get(f) for f in fields],
    )


def get_archive_count(conn: sqlite3.Connection) -> int:
    return int(conn.execute("SELECT COUNT(*) FROM archive").fetchone()[0])


def get_archive_by_status(conn: sqlite3.Connection, status: str) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM archive WHERE status = ? ORDER BY artist, album, title",
        (status,),
    ).fetchall()


# ── Persistent hash index (survives a musaeus.db wipe) ───────────────────────
#
# musaeus.db is transient per-batch working state, wiped after every
# completed batch (see config.alac_library / db_history_dir). Cross-batch
# duplicate detection against already-finalized ALAC-Library content has
# no DB rows to query once that wipe happens, so it needs its own tiny,
# separate, persistent SQLite file living under ALAC-Library itself:
# config.hash_index_path. This is intentionally a different schema/file
# from the main vault DB -- it is never wiped and only ever grows.

_HASH_INDEX_SCHEMA = """
CREATE TABLE IF NOT EXISTS finalized_hashes (
    audio_hash   TEXT NOT NULL,
    file_path    TEXT NOT NULL,   -- final ALAC-Library path at time of finalize
    finalized_at TEXT DEFAULT (datetime('now')),
    PRIMARY KEY (audio_hash, file_path)
);
CREATE INDEX IF NOT EXISTS idx_finalized_hash ON finalized_hashes(audio_hash);
"""


def open_hash_index(path: Path) -> sqlite3.Connection:
    """Open (or create) the persistent cross-batch audio-hash index."""
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=10000")
    conn.executescript(_HASH_INDEX_SCHEMA)
    conn.commit()
    return conn


def record_finalized_hash(conn: sqlite3.Connection, audio_hash: str, file_path: str) -> None:
    """Record that *file_path* (audio_hash) now exists in ALAC-Library."""
    conn.execute(
        "INSERT OR IGNORE INTO finalized_hashes (audio_hash, file_path) VALUES (?, ?)",
        (audio_hash, file_path),
    )


def lookup_finalized_hash(conn: sqlite3.Connection, audio_hash: str) -> list[sqlite3.Row]:
    """Return every ALAC-Library file_path already recorded for *audio_hash*."""
    return conn.execute(
        "SELECT file_path, finalized_at FROM finalized_hashes WHERE audio_hash = ?",
        (audio_hash,),
    ).fetchall()


def snapshot_db_before_wipe(db_path: Path, history_dir: Path) -> Path | None:
    """
    Copy db_path to a timestamped file under history_dir before a reset
    wipes it -- documented as intended behavior (config.db_history_dir's
    own docstring) since 2026-08-17, but neither reset code path
    (cli.py's _cmd_reset, console.py's _reset_menu hard reset) actually
    called it. Returns None (no-op) if db_path doesn't exist yet -- a
    fresh install with nothing to snapshot.

    Uses sqlite3's backup API rather than a plain file copy: this
    project's connections run in WAL mode (PRAGMA journal_mode=WAL), so
    recent commits can still be sitting in a `-wal` sidecar file rather
    than the main .db file -- a raw copy of just the main file could
    silently miss them. backup() produces a correct, consistent
    point-in-time snapshot regardless of WAL state, no manual checkpoint
    step needed.
    """
    if not db_path.exists():
        return None

    history_dir.mkdir(parents=True, exist_ok=True)
    # Microsecond resolution: two resets seconds apart are unlikely, but
    # two calls within the same test/script run are not, and a collision
    # would silently overwrite an earlier snapshot rather than error.
    timestamp = datetime.now(tz=timezone.utc).strftime("%Y%m%dT%H%M%S%f")
    snapshot_path = history_dir / f"musaeus_pre_reset_{timestamp}Z.db"

    source = sqlite3.connect(str(db_path))
    try:
        dest = sqlite3.connect(str(snapshot_path))
        try:
            source.backup(dest)
        finally:
            dest.close()
    finally:
        source.close()

    return snapshot_path
