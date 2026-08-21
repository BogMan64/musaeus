"""
Tests for ArtistConsolidateStage / _preferred_name()'s "the"-article format.

Regression context (do not remove without understanding why this file
exists): the canonical "the"-article output format was fixed at least
twice and kept coming back wrong.

  - 2026-08-08 (47c3a36): normalize.py's _move_article_to_suffix() fixed
    to emit "Beatles, The" (suffix form), for correct alphabetical
    sorting. Tested with 7 cases, committed.
  - ~2026-08-12: a wrong "(The)" value was seen live, manually reversed.
    Never root-caused at the time.
  - 2026-08-14 (c559fa6): artist_consolidate.py's _preferred_name()
    touched for an unrelated comma-tail bug, but its own "the"-article
    branch still emitted the OLD "Name (The)" parenthetical format.
  - 2026-08-15: root cause found. ACT1_INTAKE_CORRECTION
    (musaeus/stages/__init__.py) runs NormalizeStage BEFORE
    ArtistConsolidateStage. Normalize correctly writes "Beatles, The" --
    then ArtistConsolidateStage runs immediately after, sees that value,
    and (via _preferred_name()'s with_the/without_the branch)
    regenerates the canonical form as "Beatles (The)", silently
    reintroducing the exact bug Normalize had just fixed. Not a second
    hidden code path or a stale cache -- two stages, same pipeline run,
    disagreeing conventions, second one wins.

A pure unit test of _preferred_name() alone is not sufficient to catch
this class of regression -- it would keep passing even if some other
function elsewhere in the same pipeline re-emitted the wrong format
downstream. TestArtistConsolidateStageLive below exercises the actual
live run() path (real DB, real INSERT, real UPDATE) specifically so a
future stage-ordering or formatting regression here fails a test instead
of being rediscovered live for a third time.
"""

from pathlib import Path

import pytest

from musaeus.config import MusicConfig
from musaeus.context import RunContext
from musaeus.db import open_db, upsert_archive
from musaeus.stages.artist_consolidate import ArtistConsolidateStage, _preferred_name


class TestPreferredNameArticleFormat:
    """Unit-level: _preferred_name() must emit ', The' (suffix), never
    '(The)' (parenthetical), when with/without-article variants exist."""

    def test_generic_with_and_without_the(self):
        assert _preferred_name([("Beatles", 500), ("Beatles, The", 10)]) == "Beatles, The"

    def test_leading_the_variant(self):
        assert _preferred_name([("Kinks", 50), ("The Kinks", 5)]) == "Kinks, The"

    def test_never_emits_parenthetical_form(self):
        result = _preferred_name([("Band", 10), ("Band, The", 90)])
        assert "(The)" not in result
        assert "(the)" not in result.lower()
        assert result == "Band, The"

    def test_chieftains_real_data_split(self):
        """Real counts from the c559fa6 commit: 'Chieftains' (76 tracks)
        and 'the Chieftains' (78 tracks) previously produced different
        normalize keys and were never even recognized as the same
        artist. Confirms both the grouping fix and the format fix."""
        assert _preferred_name([("Chieftains", 76), ("the Chieftains", 78)]) == "Chieftains, The"

    def test_hardcoded_canon_display_uses_suffix_form(self):
        assert (
            _preferred_name([("Andrews Sisters", 1), ("Andrews Sisters (The)", 1)])
            == "Andrews Sisters, The"
        )

    def test_single_variant_suffix_the_stays_capitalized(self):
        """2026-08-15 real-data find: a THIRD independent spot touching
        this convention. Single-variant path (no with/without-the pair
        to group) runs the name through _smart_title(), whose
        and/of/the connector-word lowercasing rule (for cases like
        "Lord of the Rings") was also lowercasing a genuine trailing
        ", The" article suffix. Found via real INBOX data: "Chieftains
        and Belfast Harp Orchestra, The" and "Bob Seger System, The"
        both came out with a lowercase trailing "the" before this was
        fixed in _smart_title()."""
        assert (
            _preferred_name([("Chieftains and Belfast Harp Orchestra, The", 1)])
            == "Chieftains & Belfast Harp Orchestra, The"
        )
        assert _preferred_name([("Bob Seger System, The", 1)]) == "Bob Seger System, The"


@pytest.fixture
def cfg(tmp_path: Path) -> MusicConfig:
    return MusicConfig(
        vault_root=tmp_path,
        inbox=tmp_path / "INBOX",
        staging=tmp_path / "STAGING",
        quarantine=tmp_path / "QUARANTINE",
        runs_root=tmp_path / "RUNS",
        meta_dir=tmp_path / "MetaData",
        alac_library=tmp_path / "ALAC-Library",
        db_path=tmp_path / "musaeus.db",
    )


@pytest.fixture
def ctx(cfg: MusicConfig) -> RunContext:
    cfg.inbox.mkdir(parents=True, exist_ok=True)
    conn = open_db(cfg.db_path)
    return RunContext.new(cfg, conn, dry_run=False)


def _insert_catalogued(conn, path: str, artist: str) -> None:
    upsert_archive(
        conn,
        {
            "file_path": path,
            "filename": Path(path).name,
            "ext": Path(path).suffix,
            "status": "CATALOGUED",
            "artist": artist,
        },
    )


class TestArtistConsolidateStageLive:
    """Stage-level: run ArtistConsolidateStage for real against a real DB
    and assert the value actually written back to archive.artist -- not
    just what the helper function returns in isolation. This is the test
    that would have caught the NormalizeStage-then-ArtistConsolidateStage
    ordering regression, since it exercises the same live UPDATE path the
    real pipeline uses."""

    def test_run_writes_suffix_form_not_parenthetical(self, ctx: RunContext):
        conn = ctx.conn
        # Simulates the real-world sequence: NormalizeStage already moved
        # one variant to suffix form; a second, uncorrected variant
        # still has the bare (no-article) form.
        _insert_catalogued(conn, "/inbox/a.flac", "Beatles, The")
        _insert_catalogued(conn, "/inbox/b.flac", "Beatles")
        conn.commit()

        stage = ArtistConsolidateStage()
        result = stage.run(ctx)

        assert result.success
        rows = conn.execute(
            "SELECT DISTINCT artist FROM archive WHERE status = 'CATALOGUED'"
        ).fetchall()
        artists = {row["artist"] for row in rows}

        assert artists == {"Beatles, The"}, (
            f"expected consolidation to suffix form only, got {artists!r} -- "
            "if this is failing with '(The)' anywhere in it, the "
            "NormalizeStage/ArtistConsolidateStage format regression is back"
        )

    def test_run_chieftains_real_world_shape(self, ctx: RunContext):
        conn = ctx.conn
        for i in range(76):
            _insert_catalogued(conn, f"/inbox/chieftains_{i}.flac", "Chieftains")
        for i in range(78):
            _insert_catalogued(conn, f"/inbox/the_chieftains_{i}.flac", "the Chieftains")
        conn.commit()

        stage = ArtistConsolidateStage()
        stage.run(ctx)

        rows = conn.execute(
            "SELECT DISTINCT artist FROM archive WHERE status = 'CATALOGUED'"
        ).fetchall()
        artists = {row["artist"] for row in rows}
        assert artists == {"Chieftains, The"}
