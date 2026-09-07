"""
Tests for non-artist credits other than "Various Artists".

"Soundtrack" is the same defect in different clothes: a genre this library
already holds, sitting in the artist column, with the real performer in the
filename. Found live 2026-08-23 on six Pulp Fiction rows that the Various
Artists sweep could not see because it matched only Various forms.

The risk being pinned here is over-reach, not under-reach: a predicate that
swallows The Soundtrack of Our Lives would move a real band's files.
"""

from __future__ import annotations

import pytest

from musaeus.stages.various_artists_fix import (
    extract_from_filename_segments,
    is_placeholder_credit,
    is_various,
    strip_leading_credit,
)


class TestPlaceholderCreditMatching:
    @pytest.mark.parametrize("name", ["Soundtrack", "soundtrack", "  SOUNDTRACK ", "Original Soundtrack"])
    def test_bare_soundtrack_labels_are_placeholders(self, name):
        assert is_placeholder_credit(name) is True

    @pytest.mark.parametrize(
        "name",
        [
            "The Soundtrack of Our Lives",
            "Soundtrack of Our Lives",
            "Soundtracks",
            "Soundtrack Society",
            "Movie Soundtrack Allstars",
        ],
    )
    def test_real_acts_containing_the_word_are_not_matched(self, name):
        # Exact-match only. Prefix matching here would be the "Various
        # Production" mistake a second time.
        assert is_placeholder_credit(name) is False

    def test_various_forms_still_match(self):
        assert is_placeholder_credit("Various Artists") is True
        assert is_placeholder_credit("Various Artists - The Eagles Tribute") is True

    def test_real_artists_still_pass_through(self):
        assert is_placeholder_credit("The Beatles") is False
        assert is_placeholder_credit("Various Production") is False

    def test_is_various_is_left_narrow(self):
        # The Various predicate keeps its own meaning; only the gate widened.
        assert is_various("Soundtrack") is False


class TestFilenameSegmentExtraction:
    def test_soundtrack_prefixed_filename_yields_the_performer(self):
        assert (
            extract_from_filename_segments("Soundtrack - Al Green - Let's Stay Together.m4a")
            == "Al Green"
        )

    def test_two_segment_filename_yields_nothing(self):
        # "Soundtrack - Title.m4a" has no performer to recover; the stage must
        # report it rather than invent one.
        assert extract_from_filename_segments("Soundtrack - Comanche.m4a") == ""


class TestTitleDeduplication:
    """_target_path builds the filename from the title, so a title still
    carrying the artist produced "Eric Carmen - Eric Carmen - All By
    Myself.m4a" on 2026-08-19."""

    def test_leading_artist_is_removed(self):
        assert (
            strip_leading_credit("Al Green - Let's Stay Together", "Al Green")
            == "Let's Stay Together"
        )

    def test_match_is_case_insensitive(self):
        assert strip_leading_credit("AL GREEN - Let's Stay Together", "Al Green") == (
            "Let's Stay Together"
        )

    @pytest.mark.parametrize(
        ("title", "artist"),
        [
            ("Let's Stay Together", "Al Green"),
            ("Green Onions", "Al Green"),
            ("Al Green Is Love", "Al Green"),  # no " - " separator: not a credit
        ],
    )
    def test_unrelated_titles_are_untouched(self, title, artist):
        assert strip_leading_credit(title, artist) == title

    def test_title_that_is_only_the_credit_is_kept(self):
        # Stripping would leave an empty title, which is worse than a doubled
        # one -- _target_path would fall back to "Unknown Title".
        assert strip_leading_credit("Al Green - ", "Al Green") == "Al Green -"

    def test_missing_values_are_safe(self):
        assert strip_leading_credit("", "Al Green") == ""
        assert strip_leading_credit("Let's Stay Together", "") == "Let's Stay Together"


class TestArtistIsStoredInTheLibrarysForm:
    """The artist recovered from a filename is in natural form ("The
    Revels"), but the library stores 361 artists as "Revels, The". Writing
    the natural form splits the artist in two — which is exactly what
    happened to The Revels and The Tornadoes on 2026-08-24, each ending up
    with its own row and its own track count.
    """

    @pytest.mark.parametrize(
        ("recovered", "stored"),
        [
            ("The Revels", "Revels, The"),
            ("The Tornadoes", "Tornadoes, The"),
            ("The Beatles", "Beatles, The"),
            ("A Tribe Called Quest", "Tribe Called Quest, A"),
        ],
    )
    def test_natural_form_is_folded_to_the_suffix_form(self, recovered, stored):
        from musaeus.stages.normalize import _move_article_to_suffix

        assert _move_article_to_suffix(recovered) == stored

    @pytest.mark.parametrize("name", ["Al Green", "Urge Overkill", "De La Soul", "Los Lobos"])
    def test_names_without_a_leading_article_are_untouched(self, name):
        from musaeus.stages.normalize import _move_article_to_suffix

        assert _move_article_to_suffix(name) == name

    def test_the_stage_folds_before_writing(self):
        # Pin the call site, not just the helper: the helper was already
        # imported into this module for the genre lookup and still wasn't
        # being applied to the artist being written.
        import inspect

        from musaeus.stages import various_artists_fix

        src = inspect.getsource(various_artists_fix.VariousArtistsFixStage.run)
        assert "_move_article_to_suffix(real_artist" in src


class TestTheMoveAndTheRowStayInStep:
    """A move is not transactional; a database write is.

    On 2026-08-24 a script did the move first and batched its commits. A
    constraint error rolled the database back while the filesystem kept
    all 86 moves, and two files were destroyed outright. This stage had
    the same ordering. Writing the row first, then moving, then
    committing means a failure leaves neither half applied.
    """

    def test_a_failed_move_leaves_the_row_untouched(self, tmp_path, monkeypatch):
        import shutil as _shutil

        from musaeus.config import MusicConfig
        from musaeus.context import RunContext
        from musaeus.db import open_db, upsert_archive
        from musaeus.stages.various_artists_fix import VariousArtistsFixStage

        cfg = MusicConfig(
            vault_root=tmp_path, inbox=tmp_path / "INBOX", staging=tmp_path / "STAGING",
            quarantine=tmp_path / "QUARANTINE", runs_root=tmp_path / "RUNS",
            meta_dir=tmp_path / "MetaData", alac_library=tmp_path / "ALAC-Library",
            db_path=tmp_path / "musaeus.db",
        )
        ctx = RunContext.new(cfg, open_db(cfg.db_path), dry_run=False)
        src = cfg.alac_library / "2026-08-18" / "Various Artists" / "Unsorted"
        src.mkdir(parents=True)
        f = src / "Various Artists - Al Green - Let's Stay Together.m4a"
        f.write_bytes(b"audio")
        upsert_archive(ctx.conn, {"file_path": str(f), "status": "CATALOGUED",
                                  "artist": "Various Artists",
                                  "title": "Al Green - Let's Stay Together"})
        ctx.conn.commit()

        def boom(*a, **k):
            raise OSError("disk full")

        monkeypatch.setattr(_shutil, "move", boom)
        monkeypatch.setattr(
            "musaeus.stages.various_artists_fix.shutil.move", boom, raising=False
        )

        VariousArtistsFixStage().run(ctx)

        row = ctx.conn.execute("SELECT artist, file_path FROM archive").fetchone()
        assert row["artist"] == "Various Artists", "row must not claim a move that failed"
        assert row["file_path"] == str(f)
        assert f.exists()
