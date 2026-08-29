"""
Tests for TaggerStage — writes normalised metadata from DB back to file tags.

All mutagen interactions are mocked.
"""

import shutil
import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from mutagen.mp4 import MP4

from musaeus.config import MusicConfig
from musaeus.context import RunContext
from musaeus.db import open_db, upsert_archive
from musaeus.stages.tagger import TaggerStage


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
    conn = open_db(cfg.db_path)
    return RunContext.new(cfg, conn, dry_run=False)


@pytest.fixture
def ctx_dry(cfg: MusicConfig) -> RunContext:
    conn = open_db(cfg.db_path)
    return RunContext.new(cfg, conn, dry_run=True)


def _insert_catalogued(ctx: RunContext, file_path: str, **kwargs) -> None:
    defaults = {
        "file_path": file_path,
        "status": "CATALOGUED",
        "artist": "Canonical Artist",
        "album": "Canonical Album",
        "title": "Canonical Title",
        "genre": "Rock",
        "year": "2023",
        "track": 5,
    }
    defaults.update(kwargs)
    upsert_archive(ctx.conn, defaults)
    ctx.conn.commit()


# ── Validate ──────────────────────────────────────────────────────────────────


class TestTaggerValidate:
    def test_validate_with_mutagen(self, ctx):
        """Validate should pass if mutagen is importable."""
        with (
            patch("builtins.__import__", wraps=__import__),
            patch.dict("sys.modules", {"mutagen": MagicMock()}),
        ):
            TaggerStage().validate(ctx)

    def test_validate_without_mutagen(self, ctx):
        from musaeus.stages.base import StageError

        with (
            patch.dict("sys.modules", {"mutagen": None}),
            patch("builtins.__import__", side_effect=ImportError("no mutagen")),
            pytest.raises(StageError, match="mutagen"),
        ):
            TaggerStage().validate(ctx)


# ── _read_tags / _write_tags (mocked) ────────────────────────────────────────


class TestTagHelpers:
    @patch("musaeus.stages.tagger._read_tags")
    def test_compute_changes_detects_diff(self, mock_read, ctx, tmp_path):
        """If DB has different values than file tags, changes are detected."""
        track = tmp_path / "song.flac"
        track.write_bytes(b"AUDIO")

        stage = TaggerStage()
        db_row = {
            "file_path": str(track),
            "artist": "New Artist",
            "album": "New Album",
            "title": "New Title",
            "genre": "Jazz",
            "year": "2024",
            "track": "3",
        }
        file_tags = {
            "artist": "Old Artist",
            "album": "Old Album",
            "title": "Old Title",
            "genre": "Rock",
            "year": "2023",
            "track": "1",
        }
        changes = stage._compute_changes(db_row, file_tags)
        assert changes["artist"] == "New Artist"
        assert changes["album"] == "New Album"
        assert changes["title"] == "New Title"
        assert changes["genre"] == "Jazz"

    def test_compute_changes_no_diff(self, ctx):
        stage = TaggerStage()
        db_row = {
            "artist": "Same",
            "album": "Same",
            "title": "Same",
            "genre": "Rock",
            "year": "2023",
            "track": "5",
        }
        file_tags = {
            "artist": "Same",
            "album": "Same",
            "title": "Same",
            "genre": "Rock",
            "year": "2023",
            "track": "5",
        }
        changes = stage._compute_changes(db_row, file_tags)
        assert changes == {}

    def test_compute_changes_skips_empty_db_values(self, ctx):
        """If DB value is empty/None, don't write empty tags."""
        stage = TaggerStage()
        db_row = {
            "artist": None,
            "album": "",
            "title": "Title",
            "genre": "",
            "year": "",
            "track": "",
        }
        file_tags = {
            "artist": "Existing",
            "album": "Existing",
            "title": "Old",
            "genre": "R",
            "year": "2020",
            "track": "1",
        }
        changes = stage._compute_changes(db_row, file_tags)
        # Only title should change (it's non-empty and different)
        assert "title" in changes
        assert "artist" not in changes
        assert "album" not in changes


# ── TaggerStage dry_run ───────────────────────────────────────────────────────


class TestTaggerDryRun:
    @patch("musaeus.stages.tagger.TaggerStage.validate")
    @patch("musaeus.stages.tagger._read_tags")
    def test_dry_run_reports_changes(self, mock_read, mock_validate, ctx_dry, tmp_path):
        track = tmp_path / "song.flac"
        track.write_bytes(b"AUDIO")
        _insert_catalogued(ctx_dry, str(track))

        # File has different tags
        mock_read.return_value = {
            "artist": "Old Artist",
            "album": "Old Album",
            "title": "Old Title",
            "genre": "Pop",
            "year": "2020",
            "track": "1",
        }

        result = TaggerStage().dry_run(ctx_dry)
        assert result.dry_run is True
        assert result.files_changed == 1
        assert any("DRY RUN" in n for n in result.notes)

    @patch("musaeus.stages.tagger.TaggerStage.validate")
    @patch("musaeus.stages.tagger._read_tags")
    def test_dry_run_no_changes_needed(self, mock_read, mock_validate, ctx_dry, tmp_path):
        track = tmp_path / "clean.flac"
        track.write_bytes(b"AUDIO")
        _insert_catalogued(ctx_dry, str(track))

        mock_read.return_value = {
            "artist": "Canonical Artist",
            "album": "Canonical Album",
            "title": "Canonical Title",
            "genre": "Rock",
            "year": "2023",
            "track": "5",
        }

        result = TaggerStage().dry_run(ctx_dry)
        assert result.files_changed == 0
        assert result.files_skipped == 1


# ── TaggerStage run ───────────────────────────────────────────────────────────


class TestTaggerRun:
    @patch("musaeus.stages.tagger.TaggerStage.validate")
    @patch("musaeus.stages.tagger._write_tags")
    @patch("musaeus.stages.tagger._read_tags")
    def test_run_writes_changed_tags(self, mock_read, mock_write, mock_validate, ctx, tmp_path):
        track = tmp_path / "song.flac"
        track.write_bytes(b"AUDIO")
        _insert_catalogued(ctx, str(track))

        mock_read.return_value = {
            "artist": "Old",
            "album": "Old",
            "title": "Old",
            "genre": "Pop",
            "year": "2020",
            "track": "1",
        }
        mock_write.return_value = True

        result = TaggerStage().run(ctx)
        assert result.success is True
        assert result.files_changed == 1
        mock_write.assert_called_once()

    @patch("musaeus.stages.tagger.TaggerStage.validate")
    @patch("musaeus.stages.tagger._write_tags")
    @patch("musaeus.stages.tagger._read_tags")
    def test_run_skips_clean_files(self, mock_read, mock_write, mock_validate, ctx, tmp_path):
        track = tmp_path / "clean.flac"
        track.write_bytes(b"AUDIO")
        _insert_catalogued(ctx, str(track))

        mock_read.return_value = {
            "artist": "Canonical Artist",
            "album": "Canonical Album",
            "title": "Canonical Title",
            "genre": "Rock",
            "year": "2023",
            "track": "5",
        }

        result = TaggerStage().run(ctx)
        assert result.files_changed == 0
        assert result.files_skipped == 1
        mock_write.assert_not_called()

    @patch("musaeus.stages.tagger.TaggerStage.validate")
    @patch("musaeus.stages.tagger._write_tags")
    @patch("musaeus.stages.tagger._read_tags")
    def test_run_handles_write_failure(self, mock_read, mock_write, mock_validate, ctx, tmp_path):
        track = tmp_path / "fail.flac"
        track.write_bytes(b"AUDIO")
        _insert_catalogued(ctx, str(track))

        mock_read.return_value = {
            "artist": "Old",
            "album": "X",
            "title": "X",
            "genre": "",
            "year": "",
            "track": "",
        }
        mock_write.return_value = False

        result = TaggerStage().run(ctx)
        assert result.files_errored == 1
        assert result.success is False

    @patch("musaeus.stages.tagger.TaggerStage.validate")
    @patch("musaeus.stages.tagger._write_tags")
    @patch("musaeus.stages.tagger._read_tags")
    def test_run_missing_file_skipped(self, mock_read, mock_write, mock_validate, ctx, tmp_path):
        _insert_catalogued(ctx, str(tmp_path / "gone.flac"))

        result = TaggerStage().run(ctx)
        assert result.files_skipped == 1
        mock_read.assert_not_called()

    @patch("musaeus.stages.tagger.TaggerStage.validate")
    @patch("musaeus.stages.tagger._write_tags")
    @patch("musaeus.stages.tagger._read_tags")
    def test_run_logs_events(self, mock_read, mock_write, mock_validate, ctx, tmp_path):
        track = tmp_path / "song.mp3"
        track.write_bytes(b"AUDIO")
        _insert_catalogued(ctx, str(track))

        mock_read.return_value = {
            "artist": "Old",
            "album": "Old",
            "title": "Old",
            "genre": "",
            "year": "",
            "track": "",
        }
        mock_write.return_value = True

        TaggerStage().run(ctx)

        events = ctx.conn.execute("SELECT * FROM events WHERE event_type='TAGGER_WRITE'").fetchall()
        assert len(events) == 1


# ── albumartist repair ───────────────────────────────────────────────────────
#
# albumartist has no archive column and was never written by this stage, so it
# kept whatever spelling the source file arrived with. Measured on the live
# library 2026-08-29: 1,735 of 10,588 files (16.4%) disagreed with artist.
#
# It is repaired, not mirrored. Album context is the discriminator, per Grey's
# ruling 2026-08-29: a collaboration credit on a real album IS the album's
# artist and survives; on a loose single it is a leftover the canon already
# resolved. Compilations, classical performers, and pure casing differences
# are all left alone -- see albumartist_should_follow for why each.


class TestAlbumArtistRepair:
    def _changes(self, db_artist, file_albumartist, album="", genre=""):
        db_row = {"artist": db_artist, "album": album, "genre": genre}
        return TaggerStage()._compute_changes(
            db_row,
            {"artist": db_artist, "albumartist": file_albumartist,
             "album": album, "genre": genre},
        )

    def test_leading_the_variant_is_corrected(self):
        assert self._changes("Cranberries, The", "The Cranberries") == {
            "albumartist": "Cranberries, The"
        }

    def test_parenthetical_variant_is_corrected(self):
        assert self._changes("Ronettes, The", "Ronettes (the)") == {"albumartist": "Ronettes, The"}

    def test_already_canonical_is_left_alone(self):
        assert self._changes("Beatles, The", "Beatles, The") == {}

    def test_various_artists_is_preserved(self):
        # A compilation's albumartist is genuinely not the track artist.
        assert self._changes("Beatles, The", "Various Artists") == {}

    def test_split_credit_on_a_real_album_is_preserved(self):
        # On an album the credit IS the album's artist. Live example:
        # "Art Blakey & The Jazz Messengers" on "Moanin'".
        assert self._changes(
            "Johnny Cash", "Johnny Cash, The Tennessee Two", album="At Folsom Prison"
        ) == {}

    def test_split_credit_on_a_loose_single_follows_the_artist(self):
        # No album: the credit is a leftover, and the canon already collapsed
        # artist to the solo name. 549 files in the library on 2026-08-29.
        assert self._changes("Johnny Cash", "Johnny Cash, The Tennessee Two") == {
            "albumartist": "Johnny Cash"
        }

    def test_classical_performer_is_never_overwritten(self):
        # Classical is filed under composer by policy; albumartist holds the
        # performer, and that is the only place the information exists.
        assert self._changes("Antonio Vivaldi", "Anne-Sophie Mutter", genre="Classical") == {}

    def test_an_ensemble_is_protected_even_with_no_genre_set(self):
        assert self._changes("Antonio Vivaldi", "Salzburg Chamber Orchestra") == {}

    def test_classical_guard_holds_where_the_credit_rule_would_otherwise_fire(self):
        """The case the guard actually exists for.

        "Antonio Vivaldi; Itzhak Perlman, Israel Philharmonic Orchestra" leads
        with the composer, so the collaboration-credit rule matches it and --
        with no album, as these have -- would mirror, erasing the performer.
        A guard whose every test also passes without it is not a guard, so
        this asserts the one input that separates them. Real row, 2026-08-29.
        """
        credit = "Antonio Vivaldi; Itzhak Perlman, Israel Philharmonic Orchestra"
        assert self._changes("Antonio Vivaldi", credit, genre="Classical") == {}
        # and again with genre unset, so the ensemble word carries it alone
        assert self._changes("Antonio Vivaldi", credit) == {}

    def test_compilation_guard_holds_where_the_spelling_rule_would_fire(self):
        """Same shape: "Various Artists, The" folds onto "Various Artists".

        Without the compilation guard the spelling rule sees one name in two
        spellings and mirrors. A compilation marker is never a track artist.
        """
        assert self._changes("Various Artists", "Various Artists, The") == {}

    def test_a_pure_casing_difference_is_refused(self):
        # The artist field is the damaged one here -- `_smart_title()` made
        # "Tlc" out of "TLC". Mirroring would destroy the last correct copy.
        assert self._changes("Tlc", "TLC") == {}
        assert self._changes("Abba", "ABBA") == {}
        assert self._changes("Paul Mccartney", "Paul McCartney") == {}

    def test_a_fold_equal_pair_with_no_article_is_refused(self):
        """Folding equal is not enough -- only the article convention mirrors.

        "A*Teens" and "1910 Fruitgum Co." fold onto the artist field's
        "ATeens" and "1910 Fruitgum Co", but mirroring drops a character the
        albumartist still carries. Nothing reconstructs it afterwards.
        """
        assert self._changes("ATeens", "A*Teens") == {}
        assert self._changes("1910 Fruitgum Co", "1910 Fruitgum Co.") == {}
        assert self._changes("Adam & The Ants", "Adam and the Ants") == {}

    def test_a_credit_whose_lead_outspells_the_artist_is_refused(self):
        """The casing trap one level down, inside a collaboration credit.

        "24kGoldn, iann dior" leads with the correct spelling while the
        artist field holds "24kgoldn". Mirroring writes the damage into the
        last field that had it right. Real row, 2026-08-29.
        """
        assert self._changes("24kgoldn", "24kGoldn, iann dior") == {}
        # ... but an exactly-matching lead still mirrors
        assert self._changes("50 Cent", "50 Cent, Nate Dogg") == {
            "albumartist": "50 Cent"
        }

    def test_article_convention_still_applies_despite_the_casing_guard(self):
        # Differs by more than case, so it is convention, not damage.
        assert self._changes("Ad Libs, The", "THE AD LIBS") == {
            "albumartist": "Ad Libs, The"
        }

    def test_unrelated_albumartist_is_preserved(self):
        assert self._changes("Beatles, The", "Rolling Stones, The") == {}

    def test_empty_albumartist_is_not_invented(self):
        # Nothing to repair, and no DB column to source a value from.
        assert self._changes("Beatles, The", "") == {}

    def test_protected_stylized_name_not_touched(self):
        assert self._changes("De La Soul", "De La Soul") == {}


@pytest.mark.skipif(not shutil.which("ffmpeg"), reason="ffmpeg not available")
class TestTaggerActuallyWritesToDisk:
    """Round-trip tests: run Tagger unmocked and read the tag back.

    Everything above this asserts that _write_tags was CALLED. That is the
    exact shape of assertion that let Forge report success for 12,279 files
    while writing nothing: it assigned to a dotted key mutagen accepts as a
    dict key but cannot serialise, so save() succeeded and the writer
    returned True. A mock returning True is indistinguishable from that bug.

    These tests mock nothing below the stage and ask the only question that
    matters: is the tag on the file afterwards?
    """

    def _make_m4a(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(
            [
                "ffmpeg",
                "-y",
                "-f",
                "lavfi",
                "-i",
                "sine=frequency=440:duration=1",
                "-c:a",
                "alac",
                str(path),
            ],
            capture_output=True,
            check=True,
        )

    def test_artist_and_title_are_readable_back_off_disk(self, ctx, tmp_path):
        track = tmp_path / "song.m4a"
        self._make_m4a(track)
        upsert_archive(
            ctx.conn,
            {
                "file_path": str(track),
                "status": "CATALOGUED",
                "artist": "Beatles, The",
                "album": "Revolver",
                "title": "Taxman",
                "genre": "Rock",
                "year": "1966",
                "track": 1,
            },
        )
        ctx.conn.commit()

        result = TaggerStage().run(ctx)
        assert result.files_changed >= 1

        tags = MP4(str(track)).tags or {}
        assert (tags.get("\xa9ART") or [None])[0] == "Beatles, The"
        assert (tags.get("\xa9nam") or [None])[0] == "Taxman"
        assert (tags.get("\xa9alb") or [None])[0] == "Revolver"
        assert (tags.get("\xa9gen") or [None])[0] == "Rock"

    def test_a_slash_in_a_genre_survives_the_round_trip(self):
        """R&B/Funk/Soul must come back exactly as written.

        Sanitize used to strip "/" out of stored genres, inventing names
        that matched no canon. Now that metadata keeps the real string, the
        tag writer has to carry it intact.
        """
        import tempfile

        with tempfile.TemporaryDirectory() as d:
            track = Path(d) / "s.m4a"
            self._make_m4a(track)
            audio = MP4(str(track))
            audio["\xa9gen"] = ["R&B/Funk/Soul"]
            audio.save()
            assert (MP4(str(track)).tags["\xa9gen"] or [None])[0] == "R&B/Funk/Soul"

    def test_stage_reports_verified_when_the_tag_is_really_there(self, ctx, tmp_path):
        """The effect-verification seal must reflect reality, not optimism."""
        track = tmp_path / "verified.m4a"
        self._make_m4a(track)
        upsert_archive(
            ctx.conn,
            {
                "file_path": str(track),
                "status": "CATALOGUED",
                "artist": "Queen",
                "title": "Bicycle Race",
                "album": "Jazz",
            },
        )
        ctx.conn.commit()
        result = TaggerStage().execute(ctx)
        assert result.files_changed >= 1
        assert result.verified is not False, result.verify_notes
