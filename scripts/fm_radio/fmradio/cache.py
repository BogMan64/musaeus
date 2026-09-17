"""A resumable cache, so a stopped run is not a wasted one.

Two independent reasons this is not optional:

  RATE LIMIT   Both sources allow one call per second. 2,644 artists is about
               45 minutes of pass 1 alone. A crash at minute 40 that loses
               everything is the difference between a tool and a demo.

  IDLE GATE    The whole point of the screensaver design is that the run stops
               the instant Grey touches the keyboard. It will therefore be
               interrupted constantly, by design, and must lose nothing.

SQLite rather than JSON: an interrupted write to a JSON file leaves invalid
JSON and the cache is gone. SQLite commits per row. ORPHEUS's version of this
feature cached results too -- which the wishlist notes as evidence it was slow.

The cache stores RESPONSES, not verdicts. Scoring is pure and free to re-run,
so re-deriving a ranking after a rule change costs nothing, while re-fetching
would cost another 45 minutes. Cache what is expensive, recompute what is not.
"""

from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS lb_artist (
    artist_mbid TEXT PRIMARY KEY,
    fetched_at  REAL NOT NULL,
    payload     TEXT NOT NULL     -- the filtered record list, as JSON
);
CREATE TABLE IF NOT EXISTS mb_song (
    artist      TEXT NOT NULL,
    title       TEXT NOT NULL,
    fetched_at  REAL NOT NULL,
    payload     TEXT NOT NULL,
    PRIMARY KEY (artist, title)
);
-- Failures are cached too, deliberately. Without this, an artist ListenBrainz
-- has no data for is re-fetched on every run for ever. WITH it, "asked and got
-- nothing" is a fact we remember -- which is also the distinction the whole
-- tool rests on: absence of an answer is not an answer of absence.
CREATE TABLE IF NOT EXISTS failures (
    scope       TEXT NOT NULL,    -- 'lb_artist' | 'mb_song'
    key         TEXT NOT NULL,
    failed_at   REAL NOT NULL,
    reason      TEXT NOT NULL,
    PRIMARY KEY (scope, key)
);
"""


class Cache:
    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(path))
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        self.conn.commit()

    # ── ListenBrainz, per artist ──────────────────────────────────────────────

    def get_artist(self, mbid: str, max_age_days: float | None = None) -> list[dict] | None:
        row = self.conn.execute(
            "SELECT fetched_at, payload FROM lb_artist WHERE artist_mbid = ?", (mbid,)
        ).fetchone()
        if row is None:
            return None
        if max_age_days is not None:
            if time.time() - row["fetched_at"] > max_age_days * 86400:
                return None
        return json.loads(row["payload"])

    def put_artist(self, mbid: str, records: list[dict]) -> None:
        self.conn.execute(
            "INSERT INTO lb_artist (artist_mbid, fetched_at, payload) VALUES (?,?,?) "
            "ON CONFLICT(artist_mbid) DO UPDATE SET fetched_at=excluded.fetched_at, "
            "payload=excluded.payload",
            (mbid, time.time(), json.dumps(records)),
        )
        self.conn.commit()  # per row: an interrupted run keeps everything before it

    # ── MusicBrainz, per song ─────────────────────────────────────────────────

    def get_song(self, artist: str, title: str) -> list[dict] | None:
        row = self.conn.execute(
            "SELECT payload FROM mb_song WHERE artist = ? AND title = ?", (artist, title)
        ).fetchone()
        return json.loads(row["payload"]) if row else None

    def put_song(self, artist: str, title: str, records: list[dict]) -> None:
        self.conn.execute(
            "INSERT INTO mb_song (artist, title, fetched_at, payload) VALUES (?,?,?,?) "
            "ON CONFLICT(artist, title) DO UPDATE SET fetched_at=excluded.fetched_at, "
            "payload=excluded.payload",
            (artist, title, time.time(), json.dumps(records)),
        )
        self.conn.commit()

    # ── Failures ──────────────────────────────────────────────────────────────

    def note_failure(self, scope: str, key: str, reason: str) -> None:
        self.conn.execute(
            "INSERT INTO failures (scope, key, failed_at, reason) VALUES (?,?,?,?) "
            "ON CONFLICT(scope, key) DO UPDATE SET failed_at=excluded.failed_at, "
            "reason=excluded.reason",
            (scope, key, time.time(), reason),
        )
        self.conn.commit()

    def failure(self, scope: str, key: str) -> str | None:
        row = self.conn.execute(
            "SELECT reason FROM failures WHERE scope = ? AND key = ?", (scope, key)
        ).fetchone()
        return row["reason"] if row else None

    def stats(self) -> dict[str, int]:
        q = lambda sql: self.conn.execute(sql).fetchone()[0]  # noqa: E731
        return {
            "artists_cached": q("SELECT COUNT(*) FROM lb_artist"),
            "songs_cached": q("SELECT COUNT(*) FROM mb_song"),
            "failures": q("SELECT COUNT(*) FROM failures"),
        }

    def close(self) -> None:
        self.conn.close()
