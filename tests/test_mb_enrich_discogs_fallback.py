"""When MusicBrainz has no confident match, Discogs gets a try.

Separate pass from the main MB loop on purpose (see mb_enrich.py's
_discogs_fallback docstring): the row selection here is "MB checked, MB
failed, Discogs never checked", genuinely different from the main loop's
"MB never checked", and keeping the two apart means neither loop's
caching can silently break the other's.

Two things this exists to protect specifically:

  - archive.mb_artist_id/mb_artist_name are consumed by identity_tag.py,
    which writes them into file tags as real MusicBrainz identifiers.
    A Discogs match must land in its OWN columns
    (discogs_artist_id/discogs_artist_name), never mb_artist_id -- writing
    a Discogs numeric ID into a field every MB-aware reader treats as an
    MBID would corrupt identity tagging for every row this fallback
    finds.

  - the same three-state discipline mb_enrich already enforces for MB:
    a Discogs transport failure must not be stamped the same way as a
    genuine "Discogs also has no such artist" -- the first should retry
    next run, the second should not retry forever.
"""

from __future__ import annotations

import sqlite3
from types import SimpleNamespace

import pytest

from musaeus import discogs
from musaeus.stages.mb_enrich import MBEnrichStage


class _Ctx:
    def __init__(self, conn, config, run_id="run_test"):
        self.conn = conn
        self.config = config
        self.run_id = run_id
        self.events: list[tuple] = []

    def log_event(self, event_type, **kw):
        self.events.append((event_type, kw))


class _Result:
    def __init__(self):
        self.notes: list[str] = []


@pytest.fixture
def conn() -> sqlite3.Connection:
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    c.execute(
        "CREATE TABLE archive (id INTEGER PRIMARY KEY, file_path TEXT, artist TEXT, "
        "status TEXT, mb_enriched_at TEXT, mb_artist_id TEXT, mb_artist_name TEXT)"
    )
    return c


def _row(conn, artist, mb_enriched=True, mb_found=False):
    conn.execute(
        "INSERT INTO archive (file_path, artist, status, mb_enriched_at, mb_artist_id) "
        "VALUES (?,?,?,?,?)",
        (
            f"/vault/{artist}.m4a",
            artist,
            "CATALOGUED",
            "2026-09-01T00:00:00" if mb_enriched else None,
            "mbid-123" if mb_found else None,
        ),
    )
    conn.commit()


def test_no_api_key_is_a_silent_no_op(conn) -> None:
    _row(conn, "Some Artist")
    ctx = _Ctx(conn, SimpleNamespace(discogs_consumer_key=None, discogs_consumer_secret=None))
    result = _Result()
    MBEnrichStage()._discogs_fallback(ctx, result, dry_run=False)
    assert result.notes == []
    row = conn.execute("SELECT * FROM archive").fetchone()
    assert dict(row).get("discogs_checked_at") is None or "discogs_checked_at" not in dict(row)


def test_only_mb_misses_are_attempted(conn, monkeypatch) -> None:
    """An artist MB already found must never reach Discogs -- it is not
    in the selection at all, so search_artist must not even be called
    for it."""
    _row(conn, "Found By MB", mb_found=True)
    _row(conn, "Missed By MB", mb_found=False)

    calls = []
    monkeypatch.setattr(
        discogs, "search_artist",
        lambda name, key, secret: calls.append(name) or None,
    )
    ctx = _Ctx(conn, SimpleNamespace(discogs_consumer_key="fake-key", discogs_consumer_secret="fake-secret"))
    MBEnrichStage()._discogs_fallback(ctx, _Result(), dry_run=False)

    assert calls == ["Missed By MB"]


def test_a_discogs_hit_writes_its_own_columns_not_mb_columns(conn, monkeypatch) -> None:
    """The whole point: never write into mb_artist_id/mb_artist_name."""
    _row(conn, "Jake Shimabukuro")
    monkeypatch.setattr(
        discogs, "search_artist",
        lambda name, key, secret: ("999888", "Jake Shimabukuro"),
    )
    ctx = _Ctx(conn, SimpleNamespace(discogs_consumer_key="fake-key", discogs_consumer_secret="fake-secret"))
    MBEnrichStage()._discogs_fallback(ctx, _Result(), dry_run=False)

    row = dict(conn.execute("SELECT * FROM archive").fetchone())
    assert row["discogs_artist_id"] == "999888"
    assert row["discogs_artist_name"] == "Jake Shimabukuro"
    assert row["discogs_checked_at"] is not None
    assert row["mb_artist_id"] is None, "must never write a Discogs ID into the MB column"


def test_a_discogs_miss_is_stamped_so_it_is_not_retried_forever(conn, monkeypatch) -> None:
    _row(conn, "Nobody Anywhere Has Heard Of")
    monkeypatch.setattr(discogs, "search_artist", lambda name, key, secret: None)
    ctx = _Ctx(conn, SimpleNamespace(discogs_consumer_key="fake-key", discogs_consumer_secret="fake-secret"))
    MBEnrichStage()._discogs_fallback(ctx, _Result(), dry_run=False)

    row = dict(conn.execute("SELECT * FROM archive").fetchone())
    assert row["discogs_checked_at"] is not None
    assert row["discogs_artist_id"] is None


def test_a_transport_failure_is_not_stamped_and_retries_next_run(conn, monkeypatch) -> None:
    """The three-state contract, at the integration level this time.
    A LookupUnavailable must leave discogs_checked_at NULL so the row is
    picked up again next run -- stamping it would make a network wobble
    a permanent miss, exactly the bug test_mb_enrich_no_answer.py exists
    to pin for MusicBrainz."""

    def boom(name, key, secret):
        raise discogs.LookupUnavailable("timeout")

    monkeypatch.setattr(discogs, "search_artist", boom)
    _row(conn, "Transient Failure Artist")
    ctx = _Ctx(conn, SimpleNamespace(discogs_consumer_key="fake-key", discogs_consumer_secret="fake-secret"))
    MBEnrichStage()._discogs_fallback(ctx, _Result(), dry_run=False)

    row = dict(conn.execute("SELECT * FROM archive").fetchone())
    assert row["discogs_checked_at"] is None


def test_already_discogs_checked_rows_are_not_re_attempted(conn, monkeypatch) -> None:
    _row(conn, "Already Checked")
    conn.execute(
        "ALTER TABLE archive ADD COLUMN discogs_checked_at TEXT"
    )
    conn.execute("UPDATE archive SET discogs_checked_at = '2026-09-01T00:00:00'")
    conn.commit()

    calls = []
    monkeypatch.setattr(
        discogs, "search_artist",
        lambda name, key, secret: calls.append(name) or None,
    )
    ctx = _Ctx(conn, SimpleNamespace(discogs_consumer_key="fake-key", discogs_consumer_secret="fake-secret"))
    MBEnrichStage()._discogs_fallback(ctx, _Result(), dry_run=False)
    assert calls == []


def test_a_dry_run_makes_no_network_call_and_writes_nothing(conn, monkeypatch) -> None:
    _row(conn, "Would Be Tried")
    called = []
    monkeypatch.setattr(
        discogs, "search_artist",
        lambda name, key, secret: called.append(name) or None,
    )
    ctx = _Ctx(conn, SimpleNamespace(discogs_consumer_key="fake-key", discogs_consumer_secret="fake-secret"))
    result = _Result()
    MBEnrichStage()._discogs_fallback(ctx, result, dry_run=True)

    assert called == [], "dry run must not call the network"
    assert any("would be tried" in n.lower() for n in result.notes)


def test_multiple_rows_for_the_same_artist_all_get_the_result(conn, monkeypatch) -> None:
    """One Discogs query per unique artist, but every row for that artist
    (an artist with many tracks) must get the answer, not just one."""
    for i in range(3):
        conn.execute(
            "INSERT INTO archive (file_path, artist, status, mb_enriched_at, mb_artist_id) "
            "VALUES (?,?,?,?,?)",
            (f"/vault/track{i}.m4a", "Prolific Artist", "CATALOGUED", "2026-09-01T00:00:00", None),
        )
    conn.commit()

    calls = []
    monkeypatch.setattr(
        discogs, "search_artist",
        lambda name, key, secret: calls.append(name) or ("42", "Prolific Artist"),
    )
    ctx = _Ctx(conn, SimpleNamespace(discogs_consumer_key="fake-key", discogs_consumer_secret="fake-secret"))
    MBEnrichStage()._discogs_fallback(ctx, _Result(), dry_run=False)

    assert calls == ["Prolific Artist"], "must query once per unique artist, not per row"
    rows = conn.execute("SELECT discogs_artist_id FROM archive").fetchall()
    assert all(r["discogs_artist_id"] == "42" for r in rows)
