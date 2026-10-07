"""MUSAEUS — the record of edition copies.

Which master each edition copy was made from, where the copy is, and what
the bake achieved. The Lossless edition in Libraries/ALAC_Library is built
from the masters and never changes a catalogue row (Grey, 2026-09-25: the
row points at its master for good), so the knowledge of what is in the
edition has to live somewhere else -- here.

Not in musaeus.db. That database is wiped between batches ("AUDIT PASSED:
safe to snapshot and wipe the DB"), and a row id does not survive a wipe.
This file sits beside the hash ledger in db_history_dir, which does, and a
copy is keyed by (edition, the master's audio hash): the one identity that
survives a wipe, a re-file and a rename.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

LEDGER_FILENAME = "editions.db"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS edition_copies (
    edition          TEXT NOT NULL,
    master_hash      TEXT NOT NULL,
    master_path      TEXT NOT NULL,
    master_mtime_ns  INTEGER,
    output_path      TEXT NOT NULL,
    built_at         TEXT NOT NULL,
    achieved_lufs    REAL,
    mode             TEXT,
    PRIMARY KEY (edition, master_hash)
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_edition_output
    ON edition_copies (edition, output_path);
CREATE TABLE IF NOT EXISTS copy_settings (
    edition      TEXT NOT NULL,
    master_hash  TEXT NOT NULL,
    settings     TEXT NOT NULL,
    PRIMARY KEY (edition, master_hash)
);
CREATE TABLE IF NOT EXISTS measurements (
    master_hash  TEXT NOT NULL,
    recipe       TEXT NOT NULL,
    measured     TEXT NOT NULL,
    measured_at  TEXT NOT NULL,
    PRIMARY KEY (master_hash, recipe)
);
"""


@dataclass(frozen=True)
class Copy:
    edition: str
    master_hash: str
    master_path: str
    master_mtime_ns: int | None
    output_path: str
    built_at: str
    achieved_lufs: float | None
    mode: str
    #: What the copy was made with (Kind.settings): a copy made another way
    #: is made again (Grey, 2026-09-29). Empty for the Lossless edition.
    settings: str = ""


def ledger_path(config: object) -> Path:
    return Path(config.db_history_dir) / LEDGER_FILENAME  # type: ignore[attr-defined]


def open_ledger(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), timeout=30)
    conn.row_factory = sqlite3.Row
    conn.executescript(_SCHEMA)
    conn.commit()
    return conn


def open_for_reading(path: Path) -> sqlite3.Connection:
    """The record opened read-only for a plan -- or, before the first build
    has made it, an empty one in memory: asking "what would a build do?"
    must not create the file (second review of #49: three callers each
    built this from _SCHEMA)."""
    if path.exists():
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    else:
        conn = sqlite3.connect(":memory:")
        conn.executescript(_SCHEMA)
    conn.row_factory = sqlite3.Row
    return conn


def car_copy_count(config: object, conn: sqlite3.Connection) -> int:
    """How many car copies there are, for the status screens.

    The car edition's copies live here since 2026-09-28; before that the old
    builder marked rows' car_export_path, so that counts when this record
    holds no car copy. One statement of it for `musaeus status` and the
    console (cloud review of #53: it was written in both).
    """
    return (
        len(recorded_copies(config, "car") or {})
        or conn.execute(
            "SELECT COUNT(*) FROM archive WHERE car_export_path IS NOT NULL"
        ).fetchone()[0]
    )


# ── Measurements ──────────────────────────────────────────────────────────
#
# Grey, 2026-09-28: a song is measured once. The first loudnorm pass is about
# half of every copy's time, and it is a fact about the audio: the same
# audio hash through the same filters to the same targets measures the same.
# The *recipe* names exactly that (edition_bake.*_recipe), so a measurement
# is reused only where it is the one the bake would take -- the car and the
# iPhone share one; the Lossless edition's is its own.


def measurements_of(conn: sqlite3.Connection, master_hash: str) -> dict[str, dict]:
    """Every kept measurement of *master_hash*, by recipe."""
    try:
        rows = conn.execute(
            "SELECT recipe, measured FROM measurements WHERE master_hash = ?", (master_hash,)
        ).fetchall()
    except sqlite3.OperationalError:
        return {}  # a record from before measurements, opened read-only
    return {r[0]: json.loads(r[1]) for r in rows}


def keep_measurement(
    conn: sqlite3.Connection, master_hash: str, recipe: str, measured: dict
) -> None:
    """Keep one measurement, and commit."""
    conn.execute(
        "INSERT OR REPLACE INTO measurements (master_hash, recipe, measured, measured_at) "
        "VALUES (?, ?, ?, ?)",
        (master_hash, recipe, json.dumps(measured, sort_keys=True),
         datetime.now(timezone.utc).isoformat(timespec="seconds")),
    )  # fmt: skip
    conn.commit()


def measured_hashes(conn: sqlite3.Connection, family: str) -> set[str]:
    """Audio hashes with a kept measurement whose recipe starts with *family*."""
    try:
        rows = conn.execute(
            "SELECT DISTINCT master_hash FROM measurements WHERE substr(recipe, 1, ?) = ?",
            (len(family), family),
        ).fetchall()
    except sqlite3.OperationalError:
        return set()  # a record from before measurements, opened read-only
    return {r[0] for r in rows}


def copies(conn: sqlite3.Connection, edition: str) -> dict[str, Copy]:
    """Every recorded copy of *edition*, by master hash."""
    try:
        settings = dict(
            conn.execute(
                "SELECT master_hash, settings FROM copy_settings WHERE edition = ?", (edition,)
            ).fetchall()
        )
    except sqlite3.OperationalError:
        settings = {}  # a record from before settings, opened read-only
    return {
        r["master_hash"]: Copy(**dict(r), settings=settings.get(r["master_hash"], ""))
        for r in conn.execute("SELECT * FROM edition_copies WHERE edition = ?", (edition,))
    }


def record(conn: sqlite3.Connection, copy: Copy) -> None:
    """Record (or replace the record of) one copy, and commit.

    Committed per copy: the build is hours long and interruptible, and a
    copy that is on disk but not recorded is exactly what the next run has
    to work out again.

    The copy is recorded only once it is in place and carries its marker,
    so it owns its path: any other master's record still naming that path
    is stale, and is dropped rather than tripping the UNIQUE index (cloud
    review of #49: that raised mid-build).
    """
    conn.execute(
        "DELETE FROM edition_copies WHERE edition = ? AND output_path = ? AND master_hash != ?",
        (copy.edition, copy.output_path, copy.master_hash),
    )
    conn.execute(
        """
        INSERT INTO edition_copies
            (edition, master_hash, master_path, master_mtime_ns, output_path,
             built_at, achieved_lufs, mode)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT (edition, master_hash) DO UPDATE SET
            master_path = excluded.master_path,
            master_mtime_ns = excluded.master_mtime_ns,
            output_path = excluded.output_path,
            built_at = excluded.built_at,
            achieved_lufs = excluded.achieved_lufs,
            mode = excluded.mode
        """,
        (
            copy.edition,
            copy.master_hash,
            copy.master_path,
            copy.master_mtime_ns,
            copy.output_path,
            copy.built_at,
            copy.achieved_lufs,
            copy.mode,
        ),
    )
    conn.execute(
        "DELETE FROM copy_settings WHERE edition = ? AND master_hash = ?",
        (copy.edition, copy.master_hash),
    )
    if copy.settings:
        conn.execute(
            "INSERT INTO copy_settings (edition, master_hash, settings) VALUES (?, ?, ?)",
            (copy.edition, copy.master_hash, copy.settings),
        )
    conn.commit()


def forget(conn: sqlite3.Connection, edition: str, master_hash: str) -> None:
    conn.execute(
        "DELETE FROM edition_copies WHERE edition = ? AND master_hash = ?", (edition, master_hash)
    )
    conn.execute(
        "DELETE FROM copy_settings WHERE edition = ? AND master_hash = ?", (edition, master_hash)
    )
    conn.commit()


def recorded_copies(config: object, edition: str) -> dict[str, str] | None:
    """{output path: master hash} recorded for *edition*, or None when there
    is no ledger (or no place for one in *config*).

    For the audit and the doctor, which must tell an edition copy from a
    stray file without creating a ledger just by looking.
    """
    if getattr(config, "db_history_dir", None) is None:
        return None
    path = ledger_path(config)
    if not path.exists():
        return None
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        return {
            r[0]: r[1]
            for r in conn.execute(
                "SELECT output_path, master_hash FROM edition_copies WHERE edition = ?",
                (edition,),
            )
        }
    except sqlite3.Error:
        return None
    finally:
        conn.close()


def recorded_outputs(config: object, edition: str) -> set[str] | None:
    """Output paths recorded for *edition*, or None when there is no ledger."""
    found = recorded_copies(config, edition)
    return None if found is None else set(found)
