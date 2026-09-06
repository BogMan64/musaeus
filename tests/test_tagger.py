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
        """Only the albumartist decision.

        _compute_changes also performs the artist/sort-artist split (the tag
        moved to the natural form 2026-08-29, with `soar` carrying the sort
        form). That is a different rule with its own tests below; folding it
        in here would make every case in this class assert two policies at
        once and fail whenever either moved.
        """
        db_row = {"artist": db_artist, "album": album, "genre": genre}
        out = TaggerStage()._compute_changes(
            db_row,
            {"artist": db_artist, "albumartist": file_albumartist,
             "album": album, "genre": genre},
        )
        return {k: v for k, v in out.items() if k == "albumartist"}

    def test_the_natural_form_is_already_there_so_nothing_is_written(self):
        """This used to assert {"albumartist": "The Cranberries"} -- writing
        "The Cranberries" over an albumartist that already read exactly that.

        The predicate is right to say the albumartist should follow the
        artist; emitting the value anyway is what was wrong. Measured
        2026-09-06 on the live vault: 3,161 of 16,286 files were rewritten
        with their own contents on every tagger run and counted as changes,
        so two consecutive runs reported 3,997 then 3,161 and the stage never
        reached a steady state.
        """
        assert self._changes("Cranberries, The", "The Cranberries") == {}

    def test_a_case_variant_is_still_corrected(self):
        """The mirroring itself must survive the fix above: when the file
        holds a genuinely different spelling, the NATURAL form is written --
        artist moved to the natural form 2026-08-29, `soaa` carries sort."""
        assert self._changes("Cranberries, The", "THE CRANBERRIES") == {
            "albumartist": "The Cranberries"
        }

    def test_parenthetical_variant_is_corrected(self):
        assert self._changes("Ronettes, The", "Ronettes (the)") == {
            "albumartist": "The Ronettes"
        }

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
            "albumartist": "The Ad Libs"
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
        # The DB stores the sort form; the TAG carries the natural form and
        # `soar` carries the sort form. Changed 2026-08-29 -- "Beatles, The"
        # is a MUSAEUS-only string, and MusicBrainz has never heard of it.
        assert (tags.get("\xa9ART") or [None])[0] == "The Beatles"
        assert (tags.get("soar") or [None])[0] == "Beatles, The"
        assert (tags.get("\xa9nam") or [None])[0] == "Taxman"
        assert (tags.get("\xa9alb") or [None])[0] == "Revolver"
        assert (tags.get("\xa9gen") or [None])[0] == "Rock"

    def test_a_name_with_no_article_gets_no_redundant_sort_tag(self, ctx, tmp_path):
        """A sort tag identical to the artist is noise on most of the library."""
        track = tmp_path / "song2.m4a"
        self._make_m4a(track)
        upsert_archive(
            ctx.conn,
            {
                "file_path": str(track),
                "status": "CATALOGUED",
                "artist": "Dusty Springfield",
                "title": "Son of a Preacher Man",
            },
        )
        ctx.conn.commit()
        TaggerStage().run(ctx)

        tags = MP4(str(track)).tags or {}
        assert (tags.get("\xa9ART") or [None])[0] == "Dusty Springfield"
        assert not tags.get("soar"), "no article, so no sort tag"

    def test_a_stylized_name_is_not_rearranged_on_disk(self, ctx, tmp_path):
        """"De La Soul" -> "La Soul, De" was live corruption, 2026-08-16."""
        track = tmp_path / "song3.m4a"
        self._make_m4a(track)
        upsert_archive(
            ctx.conn,
            {
                "file_path": str(track),
                "status": "CATALOGUED",
                "artist": "De La Soul",
                "title": "Me Myself and I",
            },
        )
        ctx.conn.commit()
        TaggerStage().run(ctx)

        tags = MP4(str(track)).tags or {}
        assert (tags.get("\xa9ART") or [None])[0] == "De La Soul"
        assert not tags.get("soar")

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


class TestVerifyEffectReadsTheFileBack:
    """Tagger is the same shape as AlbumArt, which on 2026-08-31 reported
    "changed=10549 ✓verified" while every embed failed. It wrote through a
    library, counted its own intentions, and nothing read the file back.

    So the check reads through ffprobe, a DIFFERENT reader from mutagen --
    confirming a write with the library that made it proves only that the
    library is self-consistent.
    """

    def _tagged_row(self, ctx, path: Path, artist: str, title: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"x")
        upsert_archive(ctx.conn, {
            "file_path": str(path), "status": "CATALOGUED",
            "artist": artist, "title": title,
        })
        ctx.log_event("TAGGER_WRITE", file_path=str(path), stage="tagger")
        ctx.conn.commit()

    def _probe_stub(self, artist, title):
        return {"format": {"tags": {"artist": artist, "title": title}},
                "streams": [{"codec_type": "audio", "codec_name": "alac"}]}

    def test_tags_that_landed_are_not_flagged(self, ctx, tmp_path, monkeypatch):
        f = tmp_path / "ok.m4a"
        self._tagged_row(ctx, f, "Bill Withers", "Ain't No Sunshine")
        monkeypatch.setattr("musaeus.stages.scholar._probe",
                            lambda _p: self._probe_stub("Bill Withers", "Ain't No Sunshine"))
        assert TaggerStage().verify_effect(ctx, MagicMock(files_changed=1)) == []

    def test_a_silent_save_failure_is_caught(self, ctx, tmp_path, monkeypatch):
        """The row says it was tagged; the file still carries the old value.
        This is exactly what a failed mutagen save() looks like from outside."""
        f = tmp_path / "stale.m4a"
        self._tagged_row(ctx, f, "Bill Withers", "Ain't No Sunshine")
        monkeypatch.setattr("musaeus.stages.scholar._probe",
                            lambda _p: self._probe_stub("Unknown Artist", "Track 01"))
        problems = TaggerStage().verify_effect(ctx, MagicMock(files_changed=1))
        assert problems, "a file whose tags never changed must not pass"
        assert any("artist" in p for p in problems)

    def test_case_differences_alone_are_not_a_failure(self, ctx, tmp_path, monkeypatch):
        """Tag readers differ on case; that is not a failed write."""
        f = tmp_path / "case.m4a"
        self._tagged_row(ctx, f, "Bill Withers", "Ain't No Sunshine")
        monkeypatch.setattr("musaeus.stages.scholar._probe",
                            lambda _p: self._probe_stub("BILL WITHERS", "ain't no sunshine"))
        assert TaggerStage().verify_effect(ctx, MagicMock(files_changed=1)) == []

    def test_a_vanished_file_is_reported(self, ctx, tmp_path):
        f = tmp_path / "gone.m4a"
        self._tagged_row(ctx, f, "A", "B")
        f.unlink()
        problems = TaggerStage().verify_effect(ctx, MagicMock(files_changed=1))
        assert any("gone" in p for p in problems)

    def test_no_rows_this_run_means_nothing_to_verify(self, ctx):
        assert TaggerStage().verify_effect(ctx, MagicMock(files_changed=0)) == []

    def test_an_unreadable_file_is_not_a_false_alarm(self, ctx, tmp_path, monkeypatch):
        """A probe that fails says nothing about whether tagging worked.
        Turning a read problem into a verification failure is the
        crying-wolf half of the same mistake."""
        from musaeus.stages.scholar import ProbeError
        f = tmp_path / "unreadable.m4a"
        self._tagged_row(ctx, f, "A", "B")

        def _boom(_p):
            raise ProbeError("ffprobe exited 1")

        monkeypatch.setattr("musaeus.stages.scholar._probe", _boom)
        from musaeus.stages.base import NO_VERIFICATION
        out = TaggerStage().verify_effect(ctx, MagicMock(files_changed=1))
        assert out is NO_VERIFICATION or out == []


class TestArtistTagDoesNotOscillate:
    """A correctly tagged article artist must produce NO change at all.

    Until 2026-09-06 `artist` sat in _compute_changes' generic field_map AND
    had its own rule below it, and the two disagreed by construction:

      generic : tag != row.artist            -> write row.artist (SORT form)
      specific: natural_form(row) != tag     -> write natural    (NATURAL)

    For a file already holding the natural form the generic test is true and
    the specific one is false, so the sort form won and a correct file was
    corrupted. The next run saw the sort form, the specific rule fired, and
    the natural form went back. 3,161 of 16,286 files alternated between the
    two spellings on every pass; four consecutive runs reported 3,997, 3,161,
    3,161, 3,161 changes.

    The cost was not only churn. On any run ending with the sort form
    written, those files carried the spelling artist_form.py exists to keep
    out of the artist tag -- 0 of 2,158 MusicBrainz hits were ever in it.
    """

    def _tags(self, **over):
        base = {
            "artist": "The Zombies", "albumartist": "The Zombies",
            "sort_artist": "Zombies, The", "sort_albumartist": "Zombies, The",
            "album": "Odessey", "title": "Time of the Season",
            "genre": "Psychedelic Rock", "year": "", "track": "",
        }
        base.update(over)
        return base

    def _row(self, **over):
        base = {"artist": "Zombies, The", "album": "Odessey",
                "title": "Time of the Season", "genre": "Psychedelic Rock"}
        base.update(over)
        return base

    def test_a_correct_file_needs_no_write(self):
        assert TaggerStage()._compute_changes(self._row(), self._tags()) == {}

    def test_the_sort_form_is_never_written_into_the_artist_tag(self):
        """The specific failure: whatever else changes, the artist tag must
        never be handed the row's sort form."""
        for tags in (self._tags(), self._tags(artist="Zombies, The"),
                     self._tags(artist="THE ZOMBIES"), self._tags(genre="Rock")):
            got = TaggerStage()._compute_changes(self._row(), tags).get("artist")
            assert got != "Zombies, The", f"sort form written into the artist tag from {tags['artist']!r}"

    def test_a_wrong_artist_tag_is_still_repaired_to_the_natural_form(self):
        """The fix must not make the rule inert."""
        got = TaggerStage()._compute_changes(self._row(), self._tags(artist="Zombies, The"))
        assert got.get("artist") == "The Zombies"

    def test_applying_the_changes_twice_reaches_a_fixed_point(self):
        """Convergence, stated directly: feed the output back in and the
        second pass must ask for nothing."""
        row, tags = self._row(), self._tags(artist="Zombies, The", genre="Wrong")
        first = TaggerStage()._compute_changes(row, tags)
        assert first, "sanity: this file really does need work"
        tags.update(first)
        assert TaggerStage()._compute_changes(row, tags) == {}

    def test_a_non_article_artist_still_round_trips(self):
        row = self._row(artist="Pink Floyd")
        tags = self._tags(artist="Pink Floyd", albumartist="Pink Floyd",
                          sort_artist="", sort_albumartist="")
        assert TaggerStage()._compute_changes(row, tags) == {}
