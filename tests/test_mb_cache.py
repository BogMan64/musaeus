"""Persistent MusicBrainz lookup cache.

musaeus.db is transient per-batch state, wiped between batches, so a cache
kept there is discarded and every batch re-asks MusicBrainz about the same
artists. At ~3.9 tracks per artist that is most of a run's wall clock: an
observed 10-file run spent 18+ minutes on HTTP 503s and repeated
5-second rate-limit backoffs, not on ffmpeg. The cache lives beside the
hash index for the same reason that does.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from musaeus.db import (
    mb_cache_get_artist,
    mb_cache_get_release,
    mb_cache_put_artist,
    mb_cache_put_release,
    open_mb_cache,
)


@pytest.fixture
def cache(tmp_path: Path) -> sqlite3.Connection:
    conn = open_mb_cache(tmp_path / "mb_cache.db")
    yield conn
    conn.close()


class TestThreeOutcomes:
    """ "never asked", "asked, found" and "asked, not found" are three
    different states. Collapsing the last two makes every negative answer
    cost a rate-limited request on every batch -- which is most of what
    this cache exists to avoid."""

    def test_never_looked_up_raises(self, cache):
        with pytest.raises(KeyError):
            mb_cache_get_artist(cache, "bob seger")

    def test_a_hit_returns_the_match(self, cache):
        mb_cache_put_artist(cache, "bob seger", ("mbid-1", "Bob Seger"))
        assert mb_cache_get_artist(cache, "bob seger") == ("mbid-1", "Bob Seger")

    def test_a_cached_miss_returns_none_rather_than_raising(self, cache):
        """The distinction that matters: a miss is an answer. Re-asking for
        it every batch is what the cache is for."""
        mb_cache_put_artist(cache, "not a real artist", None)
        assert mb_cache_get_artist(cache, "not a real artist") is None

    def test_releases_have_the_same_three_states(self, cache):
        with pytest.raises(KeyError):
            mb_cache_get_release(cache, "mbid-1", "night moves")
        mb_cache_put_release(cache, "mbid-1", "night moves", "rel-9")
        assert mb_cache_get_release(cache, "mbid-1", "night moves") == "rel-9"
        mb_cache_put_release(cache, "mbid-1", "unknown album", None)
        assert mb_cache_get_release(cache, "mbid-1", "unknown album") is None


class TestPersistence:
    def test_answers_survive_reopening(self, tmp_path):
        """The whole point: a batch must not re-ask what the last batch
        already learned."""
        path = tmp_path / "mb_cache.db"
        first = open_mb_cache(path)
        mb_cache_put_artist(first, "bob seger", ("mbid-1", "Bob Seger"))
        mb_cache_put_artist(first, "nobody", None)
        first.close()

        second = open_mb_cache(path)
        try:
            assert mb_cache_get_artist(second, "bob seger") == ("mbid-1", "Bob Seger")
            assert mb_cache_get_artist(second, "nobody") is None
        finally:
            second.close()

    def test_reopening_is_idempotent(self, tmp_path):
        path = tmp_path / "mb_cache.db"
        for _ in range(3):
            conn = open_mb_cache(path)
            conn.close()
        conn = open_mb_cache(path)
        try:
            mb_cache_put_artist(conn, "x", None)
            assert mb_cache_get_artist(conn, "x") is None
        finally:
            conn.close()

    def test_a_repeat_answer_overwrites_rather_than_duplicating(self, cache):
        mb_cache_put_artist(cache, "bob seger", None)
        mb_cache_put_artist(cache, "bob seger", ("mbid-1", "Bob Seger"))
        assert mb_cache_get_artist(cache, "bob seger") == ("mbid-1", "Bob Seger")
        n = cache.execute("SELECT COUNT(*) FROM mb_artist WHERE artist_key='bob seger'").fetchone()
        assert n[0] == 1


class TestConfigLocation:
    def test_the_cache_sits_where_the_hash_index_does(self, tmp_path):
        """Both must survive the per-batch wipe of musaeus.db, so both live
        outside it in the same place."""
        from musaeus.config import MusicConfig

        cfg = MusicConfig(
            vault_root=tmp_path,
            inbox=tmp_path / "INBOX",
            staging=tmp_path / "STAGING",
            quarantine=tmp_path / "QUARANTINE",
            runs_root=tmp_path / "RUNS",
            meta_dir=tmp_path / "MetaData",
            alac_library=tmp_path / "ALAC-Library",
            db_path=tmp_path / "musaeus.db",
        )
        assert cfg.mb_cache_path.parent == cfg.hash_index_path.parent
        assert cfg.mb_cache_path != cfg.db_path
        assert cfg.mb_cache_path.name == "mb_cache.db"
