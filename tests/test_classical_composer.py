"""
Tests for ClassicalComposerStage.

Classical is filed under the composer (Grey's ruling, 2026-08-24), so a
Bach cantata sits with the other Bach rather than under whichever
ensemble recorded it.

What is pinned here is mostly the refusal path. A wrong composer is worse
than none: it is indistinguishable from a right one once written, and it
moves the file. All four of the names below are real credits in this
library, and every one of them would be a wrong answer.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from musaeus.config import MusicConfig
from musaeus.context import RunContext
from musaeus.db import open_db, upsert_archive
from musaeus.stages.classical_composer import (
    ClassicalComposerStage,
    composer_for,
    load_composer_canon,
)

CANON = {
    "johann sebastian bach": "Johann Sebastian Bach",
    "j.s. bach": "Johann Sebastian Bach",
    "handel": "George Frideric Handel",
    "vivaldi": "Antonio Vivaldi",
}


class TestCatalogueNumbersDecideAlone:
    """A thematic catalogue number names its composer unambiguously --
    that is what those systems are for, and it resolved 85 of 104 on the
    live library."""

    @pytest.mark.parametrize(
        ("title", "expected"),
        [
            ("Concerto for 2 Violins in D Minor, BWV 1043 - II. Largo", "Johann Sebastian Bach"),
            ("Guitar Concerto in D Major, RV 93 - II. Largo", "Antonio Vivaldi"),
            ("Messiah, HWV 56, Pt. 1", "George Frideric Handel"),
            ("Die Walküre, WWV 86B, Act III", "Richard Wagner"),
            ("Abdelazer, Z. 570 - Rondeau", "Henry Purcell"),
        ],
    )
    def test_the_number_wins_regardless_of_performer(self, title, expected):
        got, how = composer_for("Some Chamber Orchestra, A Soloist", title, CANON)
        assert got == expected
        assert how == "catalogue number"

    def test_kochel_is_not_used_because_it_is_ambiguous(self):
        # K. numbers Mozart; Kirkpatrick also numbers Scarlatti. This exact
        # title is Scarlatti, and it resolves through L. (Longo) instead.
        got, _ = composer_for("Balázs Szokolay", "Keyboard Sonata in D Minor, K. 1, L. 366", CANON)
        assert got == "Domenico Scarlatti"


class TestNamesThatLookLikeComposersButAreNot:
    """Every one of these is a real credit in this library."""

    @pytest.mark.parametrize(
        ("artist", "title"),
        [
            ("Franz Liszt Chamber Orchestra & János Rolla", "Concerto No. 6 in A Minor"),
            ("Josef Suk Chamber Orchestra", "Serenade"),
            ("The Four Seasons", "December, 1963"),
            ("Cornelis Vreeswijk", "Fiddler on the Roof - If I Were a Rich Man"),
        ],
    )
    def test_they_resolve_to_nothing(self, artist, title):
        assert composer_for(artist, title, CANON)[0] is None


class TestCanonMatching:
    def test_the_composer_is_found_wherever_it_sits_in_the_credit(self):
        # Position varies -- these two are real, and reversed.
        assert composer_for("Dubravka Tomsic, Johann Sebastian Bach", "Italian Concerto", CANON)[0] \
            == "Johann Sebastian Bach"
        assert composer_for("Johann Sebastian Bach, Dubravka Tomsic", "Italian Concerto", CANON)[0] \
            == "Johann Sebastian Bach"

    def test_a_title_prefix_resolves_it(self):
        got, how = composer_for("Academy of St Martin In the Fields", "Handel - Water Music", CANON)
        assert got == "George Frideric Handel"
        assert how == "title prefix"

    def test_a_missing_canon_file_yields_an_empty_map(self, tmp_path):
        assert load_composer_canon(tmp_path / "nope.tsv") == {}


@pytest.fixture
def ctx(tmp_path) -> RunContext:
    cfg = MusicConfig(
        vault_root=tmp_path, inbox=tmp_path / "INBOX", staging=tmp_path / "STAGING",
        quarantine=tmp_path / "QUARANTINE", runs_root=tmp_path / "RUNS",
        meta_dir=tmp_path / "MetaData", alac_library=tmp_path / "ALAC-Library",
        db_path=tmp_path / "musaeus.db",
    )
    cfg.meta_dir.mkdir(parents=True, exist_ok=True)
    (cfg.meta_dir / "Composer_Canon.tsv").write_text(
        "\n".join(f"{k}\t{v}" for k, v in CANON.items()), encoding="utf-8"
    )
    return RunContext.new(cfg, open_db(cfg.db_path), dry_run=False)


def _track(ctx, artist, title, genre="Classical"):
    p = ctx.config.alac_library / "2026-08-18" / artist / "Unsorted" / f"{artist} - {title}.m4a"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(b"audio")
    upsert_archive(ctx.conn, {"file_path": str(p), "status": "CATALOGUED",
                              "artist": artist, "title": title, "genre": genre})
    ctx.conn.commit()
    return p


class TestTheStage:
    def test_a_resolvable_track_moves_to_its_composer(self, ctx):
        src = _track(ctx, "Academy of St Martin In the Fields", "Concerto, BWV 1043")
        ClassicalComposerStage().run(ctx)
        row = ctx.conn.execute("SELECT artist, file_path FROM archive").fetchone()
        assert row["artist"] == "Johann Sebastian Bach"
        assert not src.exists()
        assert Path(row["file_path"]).exists()
        assert "Johann Sebastian Bach" in row["file_path"]

    def test_an_unresolvable_track_is_left_completely_alone(self, ctx):
        src = _track(ctx, "Vanessa-Mae", "Contradanza")
        ClassicalComposerStage().run(ctx)
        row = ctx.conn.execute("SELECT artist, file_path FROM archive").fetchone()
        assert row["artist"] == "Vanessa-Mae"
        assert src.exists()

    def test_non_classical_is_never_touched(self, ctx):
        # A rock track whose title happens to carry a catalogue-like string
        # must not be dragged into a composer folder.
        src = _track(ctx, "Deep Purple", "Concerto, BWV 1043", genre="Rock")
        ClassicalComposerStage().run(ctx)
        row = ctx.conn.execute("SELECT artist FROM archive").fetchone()
        assert row["artist"] == "Deep Purple"
        assert src.exists()

    def test_dry_run_writes_nothing(self, ctx):
        src = _track(ctx, "Academy of St Martin In the Fields", "Concerto, BWV 1043")
        result = ClassicalComposerStage().dry_run(ctx)
        assert result.files_changed == 1
        assert src.exists()
        assert ctx.conn.execute("SELECT artist FROM archive").fetchone()["artist"] \
            == "Academy of St Martin In the Fields"

    def test_verify_effect_reports_anything_still_resolvable(self, ctx):
        _track(ctx, "Academy of St Martin In the Fields", "Concerto, BWV 1043")
        stage = ClassicalComposerStage()
        problems = stage.verify_effect(ctx, stage._make_result())
        assert any("still filed under a performer" in p for p in problems)

    def test_verify_effect_is_clean_once_the_stage_has_run(self, ctx):
        _track(ctx, "Academy of St Martin In the Fields", "Concerto, BWV 1043")
        stage = ClassicalComposerStage()
        stage.run(ctx)
        assert stage.verify_effect(ctx, stage._make_result()) == []


def _inbox_track(ctx, artist, title, genre="Classical"):
    """A track staged FLAT in the INBOX, the way the batch campaign stages them.

    Not `_track`'s `<library>/<date>/<artist>/<album>/` layout. Measured on
    the live vault 2026-08-25: all 9,866 queued files and the 500 in INBOX
    sit at depth 1, with no artist or album directory at all. Every test
    above used the deep layout, which is the only reason the stage could
    move three tracks clean out of the vault without a test noticing.
    """
    ctx.config.inbox.mkdir(parents=True, exist_ok=True)
    p = ctx.config.inbox / f"{artist} - {title}.m4a"
    p.write_bytes(b"audio")
    upsert_archive(ctx.conn, {"file_path": str(p), "status": "CATALOGUED",
                              "artist": artist, "title": title, "genre": genre})
    ctx.conn.commit()
    return p


class TestAFlatInboxTrackStaysInTheVault:
    """The regression: deriving a destination from the source's grandparent.

    For a flat INBOX file `src.parent.parent` IS the vault root, so
    `.with_name(composer)` addressed a SIBLING of the vault and the track
    was moved outside every root the run knows about.
    """

    def test_the_file_never_leaves_the_vault(self, ctx):
        src = _inbox_track(ctx, "Academy of St Martin In the Fields", "Concerto, BWV 1043")
        ClassicalComposerStage().run(ctx)
        row = ctx.conn.execute("SELECT artist, file_path FROM archive").fetchone()
        final = Path(row["file_path"]).resolve()
        # The assertion that would have caught it: not "did it move" but
        # "is it still somewhere this run can reach".
        assert final.is_relative_to(ctx.config.vault_root.resolve()), final
        assert final.exists()
        assert src.exists(), "a pre-finalize track has no artist dir to rename"

    def test_the_artist_is_still_set_to_the_composer(self, ctx):
        # Not moving must not mean not doing the job: finalize files the
        # track by the artist on the row, so the row is the whole job here.
        _inbox_track(ctx, "Academy of St Martin In the Fields", "Concerto, BWV 1043")
        ClassicalComposerStage().run(ctx)
        row = ctx.conn.execute("SELECT artist FROM archive").fetchone()
        assert row["artist"] == "Johann Sebastian Bach"

    def test_the_vault_root_is_never_pruned(self, ctx):
        # The cleanup loop called rmdir() on `artist_dir`, which for a flat
        # INBOX file was the vault itself. It survived only because rmdir
        # refuses a non-empty directory.
        _inbox_track(ctx, "Academy of St Martin In the Fields", "Concerto, BWV 1043")
        ClassicalComposerStage().run(ctx)
        assert ctx.config.vault_root.exists()
        assert ctx.config.inbox.exists()

    def test_a_library_track_still_moves(self, ctx):
        # The deep layout keeps its old behaviour: there IS an artist
        # directory there, and renaming it is correct.
        src = _track(ctx, "Academy of St Martin In the Fields", "Concerto, BWV 1043")
        ClassicalComposerStage().run(ctx)
        row = ctx.conn.execute("SELECT artist, file_path FROM archive").fetchone()
        assert not src.exists()
        assert "Johann Sebastian Bach" in row["file_path"]
        assert Path(row["file_path"]).resolve().is_relative_to(
            ctx.config.alac_library.resolve()
        )
