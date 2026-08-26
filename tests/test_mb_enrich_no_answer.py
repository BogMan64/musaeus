"""A network failure is not an answer, and must not be recorded as one.

`_search_artist` returned None both when MusicBrainz said "no such artist"
and when the request never completed -- timeout, 503, DNS, policy refusal.
The caller could not tell the two apart, so on a transport failure it:

  1. stamped `mb_enriched_at`, permanently marking the row looked-up
     (selection is `mb_enriched_at IS NULL`, so it is never asked again), and
  2. wrote the same None into the PERSISTENT cache, carrying the mistake
     into every future run.

Measured in the 16:16 run on 2026-08-25: 32 transport failures against 3
successes. Every one of those 32 would have been marked done.

This is the three-state confusion the marker commit set out to fix, one
level down: "asked and told no" and "never got an answer" are different
facts, and only the first is a reason not to repeat the work.
"""

from __future__ import annotations

import sqlite3

import pytest

from musaeus.network_policy import NetworkPolicy, policy
from musaeus.stages import mb_enrich
from musaeus.stages.mb_enrich import LookupUnavailable, _search_artist


class TestTheHelperSeparatesTheTwoCases:
    @pytest.mark.parametrize(
        "boom",
        [
            TimeoutError("The read operation timed out"),
            OSError("HTTP Error 503: Service Temporarily Unavailable"),
            ConnectionError("dns failure"),
        ],
    )
    def test_no_answer_raises(self, monkeypatch, boom):
        def fake(path, params):
            raise boom

        monkeypatch.setattr(mb_enrich, "_mb_get", fake)
        with pytest.raises(LookupUnavailable):
            _search_artist("Pretenders, The")

    def test_a_real_miss_still_returns_none(self, monkeypatch):
        # MusicBrainz answered; it simply has nothing good enough. That IS
        # an answer and must stay distinguishable from the case above.
        monkeypatch.setattr(mb_enrich, "_mb_get", lambda path, params: {"artists": []})
        assert _search_artist("Not A Real Band At All") is None

    def test_a_hit_still_returns_the_pair(self, monkeypatch):
        monkeypatch.setattr(
            mb_enrich,
            "_mb_get",
            lambda path, params: {"artists": [{"id": "mbid-1", "name": "ABBA", "score": 100}]},
        )
        assert _search_artist("Abba") == ("mbid-1", "ABBA")


def _reachable(monkeypatch):
    """Satisfy the connectivity probe.

    mb_enrich opens a real HEAD to musicbrainz.org before doing any work and
    skips the whole stage if it fails (graceful degradation, by design).
    The suite blocks real sockets, so without this every test below passes
    vacuously with files_processed=0 -- exactly the "check that cannot fail"
    this project keeps finding.
    """
    monkeypatch.setattr(mb_enrich, "urlopen", lambda *a, **k: _FakeResp())


class _FakeResp:
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def read(self):
        return b"{}"


def _ctx(tmp_path):
    from musaeus.config import MusicConfig
    from musaeus.context import RunContext
    from musaeus.db import open_db, upsert_archive

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
    cfg.meta_dir.mkdir(parents=True, exist_ok=True)
    ctx = RunContext.new(cfg, open_db(cfg.db_path), dry_run=False)
    p = cfg.alac_library / "Pretenders, The - Brass in Pocket.m4a"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(b"audio")
    upsert_archive(
        ctx.conn,
        {
            "file_path": str(p),
            "status": "CATALOGUED",
            "artist": "Pretenders, The",
            "title": "Brass in Pocket",
            "album": "Pretenders",
        },
    )
    ctx.conn.commit()
    return ctx


class TestTheRowSurvivesAnOutage:
    def test_a_no_answer_leaves_the_row_unmarked(self, tmp_path, monkeypatch):
        ctx = _ctx(tmp_path)
        _reachable(monkeypatch)

        def down(path, params):
            raise TimeoutError("The read operation timed out")

        monkeypatch.setattr(mb_enrich, "_mb_get", down)
        with policy(NetworkPolicy.ALLOWED):
            mb_enrich.MBEnrichStage().run(ctx)

        row = ctx.conn.execute(
            "SELECT mb_enriched_at, mb_artist_id FROM archive"
        ).fetchone()
        # The assertion that would have caught this: not "did the stage
        # survive" but "is the row still going to be asked again".
        assert row["mb_enriched_at"] is None, "an outage must not mark a row done"
        assert row["mb_artist_id"] is None

    def test_a_real_miss_does_mark_the_row(self, tmp_path, monkeypatch):
        # The behaviour the marker was added for must survive this fix.
        ctx = _ctx(tmp_path)
        _reachable(monkeypatch)
        monkeypatch.setattr(mb_enrich, "_mb_get", lambda path, params: {"artists": []})
        with policy(NetworkPolicy.ALLOWED):
            mb_enrich.MBEnrichStage().run(ctx)

        row = ctx.conn.execute("SELECT mb_enriched_at FROM archive").fetchone()
        assert row["mb_enriched_at"] is not None, "a genuine miss must still settle"

    def test_an_outage_does_not_poison_the_persistent_cache(self, tmp_path, monkeypatch):
        ctx = _ctx(tmp_path)
        _reachable(monkeypatch)

        def down(path, params):
            raise TimeoutError("The read operation timed out")

        monkeypatch.setattr(mb_enrich, "_mb_get", down)
        with policy(NetworkPolicy.ALLOWED):
            mb_enrich.MBEnrichStage().run(ctx)

        cache_path = ctx.config.mb_cache_path
        if not cache_path.exists():
            return  # nothing written at all is the correct outcome
        con = sqlite3.connect(cache_path)
        names = [t[0] for t in con.execute("SELECT name FROM sqlite_master WHERE type='table'")]
        for t in names:
            n = con.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
            assert n == 0, f"an outage wrote {n} row(s) into cache table {t}"
