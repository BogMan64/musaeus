"""
Tests for TributeQuarantineStage — detect + quarantine tribute-band/
karaoke/meditation content. Wired into Act 1 on 2026-09-01; see
test_tribute_quarantine_wiring.py for its position and verification.

Uses real files on disk (not mocked): this stage's whole purpose is a
real filesystem move, a real manifest, and a real restore script -- same
rationale as DupeResolverStage's own test suite.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from musaeus.config import MusicConfig
from musaeus.context import RunContext
from musaeus.db import open_db, upsert_archive
from musaeus.stages.tribute_quarantine import TributeQuarantineStage, is_junk

_TEST_BATCH_DATE = "2026-01-15"


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
    cfg.ensure_dirs()
    conn = open_db(cfg.db_path)
    c = RunContext.new(cfg, conn, dry_run=False)
    c.set("finalize_batch_date", _TEST_BATCH_DATE)
    return c


def _make_row(
    ctx: RunContext, relpath: str, artist: str, title: str = "Song", album: str = "Album"
) -> Path:
    path = ctx.alac_library / relpath
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"FAKE AUDIO")
    upsert_archive(
        ctx.conn,
        {
            "file_path": str(path),
            "status": "CATALOGUED",
            "artist": artist,
            "title": title,
            "album": album,
        },
    )
    ctx.conn.commit()
    return path


# ── is_junk pattern matching ──────────────────────────────────────────────────


class TestIsJunkDetection:
    def test_karaoke_artist_matches(self):
        matched, reason = is_junk("Some Karaoke Band", "Some Song", "")
        assert matched is True
        assert "artist_pattern" in reason

    def test_known_junk_artist_matches(self):
        matched, reason = is_junk("Michael Sealey", "Sleep Meditation", "")
        assert matched is True
        assert "known_junk_artist" in reason

    def test_title_pattern_matches(self):
        matched, _ = is_junk("Some Band", "Yesterday (Karaoke Version)", "")
        assert matched is True

    def test_album_pattern_matches(self):
        matched, _ = is_junk("Some Band", "Song", "Ultimate Tribute Album")
        assert matched is True

    def test_clean_metadata_does_not_match(self):
        matched, reason = is_junk("The Beatles", "Yesterday", "Help!")
        assert matched is False
        assert reason == ""

    def test_protected_artist_never_matches_even_with_tribute_in_album(self):
        """Neil Young appearing on a tribute compilation album must not be
        flagged -- he didn't make the tribute."""
        matched, _ = is_junk("Neil Young", "Old Man", "A Tribute to Neil Young")
        assert matched is False

    def test_protected_artist_beats_artist_pattern_too(self):
        matched, _ = is_junk("Sleep", "Dopesmoker", "Sleep's Holy Mountain")
        assert matched is False

    def test_protected_check_runs_before_known_junk_artist_check(self):
        """Protected status must win even against an exact known-junk-artist
        match, since is_junk() checks protected artists first."""
        matched, _ = is_junk("Spirit", "Randy California", "Twelve Dreams")
        assert matched is False


# ── Stage run ──────────────────────────────────────────────────────────────────


class TestTributeQuarantineRun:
    def test_matched_file_moved_and_status_updated(self, ctx):
        path = _make_row(ctx, "Karaoke Channel/Unsorted/song.m4a", "Karaoke Channel, The")

        result = TributeQuarantineStage().run(ctx)

        assert result.success is True
        assert result.files_changed == 1
        assert not path.exists()

        row = ctx.conn.execute(
            "SELECT status, file_path FROM archive WHERE artist = ?",
            ("Karaoke Channel, The",),
        ).fetchone()
        assert row["status"] == "TRIBUTE_REVIEW"
        assert "TRIBUTE_REMOVED_FOR_REVIEW" in row["file_path"]
        assert Path(row["file_path"]).exists()

    def test_clean_file_untouched(self, ctx):
        path = _make_row(ctx, "Beatles/Help/yesterday.m4a", "The Beatles", "Yesterday", "Help!")

        result = TributeQuarantineStage().run(ctx)

        assert result.files_changed == 0
        assert path.exists()
        row = ctx.conn.execute("SELECT status FROM archive WHERE artist = 'The Beatles'").fetchone()
        assert row["status"] == "CATALOGUED"

    def test_protected_artist_untouched_despite_tribute_album(self, ctx):
        path = _make_row(
            ctx,
            "Neil Young/Tribute/old_man.m4a",
            "Neil Young",
            "Old Man",
            "A Tribute to Neil Young",
        )

        TributeQuarantineStage().run(ctx)

        assert path.exists()
        row = ctx.conn.execute("SELECT status FROM archive WHERE artist = 'Neil Young'").fetchone()
        assert row["status"] == "CATALOGUED"

    def test_manifest_and_restore_script_written(self, ctx):
        _make_row(ctx, "Karaoke Channel/Unsorted/song.m4a", "Karaoke Channel, The")

        result = TributeQuarantineStage().run(ctx)

        manifest_note = next(n for n in result.notes if n.startswith("manifest:"))
        manifest_path = Path(manifest_note.split("manifest: ", 1)[1])
        assert manifest_path.exists()
        content = manifest_path.read_text()
        assert "source" in content and "destination" in content and "reason" in content

        restore_note = next(n for n in result.notes if n.startswith("restore script:"))
        restore_path = Path(restore_note.split("restore script: ", 1)[1])
        assert restore_path.exists()
        assert os.access(restore_path, os.X_OK)

    def test_restore_script_actually_reverses_the_move(self, ctx):
        import subprocess

        path = _make_row(ctx, "Karaoke Channel/Unsorted/song.m4a", "Karaoke Channel, The")
        original_content = path.read_bytes()

        result = TributeQuarantineStage().run(ctx)
        assert not path.exists()

        restore_note = next(n for n in result.notes if n.startswith("restore script:"))
        restore_path = restore_note.split("restore script: ", 1)[1]
        subprocess.run(["bash", restore_path], check=True)

        assert path.exists()
        assert path.read_bytes() == original_content

    def test_missing_file_reported_not_crash(self, ctx):
        upsert_archive(
            ctx.conn,
            {
                "file_path": str(ctx.alac_library / "Karaoke Channel/gone.m4a"),
                "status": "CATALOGUED",
                "artist": "Karaoke Channel, The",
                "title": "Song",
            },
        )
        ctx.conn.commit()

        result = TributeQuarantineStage().run(ctx)

        assert result.files_errored == 1
        assert any("missing on disk" in e for e in result.errors)

    def test_already_quarantined_row_not_reprocessed(self, ctx):
        _make_row(ctx, "Karaoke Channel/Unsorted/song.m4a", "Karaoke Channel, The")

        first = TributeQuarantineStage().run(ctx)
        second = TributeQuarantineStage().run(ctx)

        assert first.files_changed == 1
        assert second.files_processed == 0

    def test_dry_run_makes_no_changes(self, ctx):
        path = _make_row(ctx, "Karaoke Channel/Unsorted/song.m4a", "Karaoke Channel, The")

        result = TributeQuarantineStage().dry_run(ctx)

        assert result.files_processed == 1
        assert path.exists()
        row = ctx.conn.execute(
            "SELECT status FROM archive WHERE artist = 'Karaoke Channel, The'"
        ).fetchone()
        assert row["status"] == "CATALOGUED"


# ── TuneMyMusic export ──────────────────────────────────────────────────────
#
# Built by hand once as a one-off (2026-09-02); automated the next day
# after Grey asked for it not to require asking again. Reuses
# musaeus.wanted_list rather than a fresh extraction attempt -- see that
# module's own docstring for why (a fresh attempt is exactly the shape
# this project keeps finding duplicated).


class TestWantedListExport:
    def test_a_credited_knockoff_produces_a_wanted_list(self, ctx, cfg) -> None:
        _make_row(
            ctx, "Steely Dan Tribute/song.m4a",
            artist="Karaoke Channel, The",
            title="Midnight Cruiser [In the Style of Steely Dan]",
        )
        result = TributeQuarantineStage().run(ctx)

        wanted_files = list((cfg.alac_archive / "TRIBUTE_REMOVED_FOR_REVIEW" / _TEST_BATCH_DATE).glob(
            "tunemymusic_*.csv"
        ))
        assert len(wanted_files) == 1
        assert wanted_files[0].read_text().strip() == "Steely Dan - Midnight Cruiser"
        assert any("wanted list" in n for n in result.notes)

    def test_an_already_owned_knockoff_writes_no_wanted_list(self, ctx, cfg) -> None:
        """The whole point of checking ownership before exporting: Grey
        should never be asked to re-acquire something he already has."""
        _make_row(ctx, "Real/song.m4a", artist="Steely Dan", title="Midnight Cruiser")
        _make_row(
            ctx, "Fake/song.m4a",
            artist="Karaoke Channel, The",
            title="Midnight Cruiser [In the Style of Steely Dan]",
        )
        TributeQuarantineStage().run(ctx)

        wanted_files = list((cfg.alac_archive / "TRIBUTE_REMOVED_FOR_REVIEW" / _TEST_BATCH_DATE).glob(
            "tunemymusic_*.csv"
        ))
        assert wanted_files == []

    def test_an_uncredited_knockoff_writes_no_wanted_list(self, ctx, cfg) -> None:
        _make_row(ctx, "Fake/song.m4a", artist="Karaoke Channel, The", title="What's Up")
        result = TributeQuarantineStage().run(ctx)

        wanted_files = list((cfg.alac_archive / "TRIBUTE_REMOVED_FOR_REVIEW" / _TEST_BATCH_DATE).glob(
            "tunemymusic_*.csv"
        ))
        assert wanted_files == []
        assert not any("wanted list" in n for n in result.notes)

    def test_the_manifest_still_writes_normally_alongside_it(self, ctx, cfg) -> None:
        """Guards against the exact regression hit while building this:
        moved dicts gained artist/title keys for the export, and
        csv.DictWriter raises on any row key outside its declared
        fieldnames unless told to ignore extras."""
        _make_row(
            ctx, "Steely Dan Tribute/song.m4a",
            artist="Karaoke Channel, The",
            title="Midnight Cruiser [In the Style of Steely Dan]",
        )
        result = TributeQuarantineStage().run(ctx)

        assert not result.errors
        manifest_files = list(
            (cfg.alac_archive / "TRIBUTE_REMOVED_FOR_REVIEW" / _TEST_BATCH_DATE).glob(
                "tribute_manifest_*.csv"
            )
        )
        assert len(manifest_files) == 1
        header = manifest_files[0].read_text().splitlines()[0]
        assert header == "source,destination,reason"

    def test_multiple_credited_tracks_all_appear(self, ctx, cfg) -> None:
        _make_row(
            ctx, "A/song.m4a",
            artist="Karaoke Channel, The",
            title="Song A [In the Style of Artist One]",
        )
        _make_row(
            ctx, "B/song.m4a",
            artist="Karaoke Channel, The",
            title="Song B [In the Style of Artist Two]",
        )
        TributeQuarantineStage().run(ctx)

        wanted_files = list((cfg.alac_archive / "TRIBUTE_REMOVED_FOR_REVIEW" / _TEST_BATCH_DATE).glob(
            "tunemymusic_*.csv"
        ))
        lines = wanted_files[0].read_text().strip().splitlines()
        assert set(lines) == {"Artist One - Song A", "Artist Two - Song B"}


class TestTributeInTheTitle:
    """A knock-off that names itself only in the TITLE.

    `tribute` was in JUNK_ARTIST_PATTERNS and JUNK_ALBUM_PATTERNS but not
    in JUNK_TITLE_PATTERNS, so a product whose artist name looks innocent
    walked straight into the library. All three of these were CATALOGUED on
    2026-09-06 and Grey found them by eye, not by any check:

        artist "Classic Blues Tones"  title "... George Thorogood and The
                                             Destroyers Tribute"
        artist "Scott D. Davis"       title "In the End (Piano Tribute to
                                             Linkin Park)"
        artist "Led Zepagain"         title "Whole Lotta Love - a Tribute
                                             to Led Zeppelin"
    """

    def test_a_tribute_named_only_in_the_title_is_junk(self):
        for artist, title in (
            ("Classic Blues Tones", "Bad To The Bone - George Thorogood and The Destroyers Tribute"),
            ("Scott D. Davis", "In the End (Piano Tribute to Linkin Park)"),
            ("Led Zepagain", "Whole Lotta Love - a Tribute to Led Zeppelin"),
        ):
            caught, reason = is_junk(artist, title, "")
            assert caught, "%s / %s was not caught" % (artist, title)

    def test_a_piano_cover_product_is_junk(self):
        assert is_junk("Piano Covers", "Nirvana - Heart Shaped Box - Piano Cover", "")[0]
        assert is_junk("Piano Covers", "Queen - I Want To Break Free", "")[0]

    def test_a_real_artist_performing_at_a_tribute_is_protected(self):
        """The cost of the rule above, and the reason PROTECTED_ARTISTS
        exists. Springsteen's recording is a genuine performance by six
        named artists; the Three Tenors' is a real concert album."""
        assert not is_junk(
            "Bruce Springsteen",
            "(Your Love Keeps Lifting Me) Higher and Higher [With Darlene Love, "
            "John Fogerty, Sam Moore, Billy Joel And Tom Morello] "
            "[A Tribute To Jackie Wilson]", "")[0]
        assert not is_junk(
            "Three Tenors, Los Angeles Music Center Opera Chorus, The",
            "A Tribute to Hollywood - Singin' in the Rain (Arr. Schifrin) [Live]", "")[0]

    def test_spirits_own_song_title_is_protected(self):
        """'Tribute To The Rain Woods' is Spirit's own composition."""
        assert not is_junk("Spirit", "Tribute To The Rain Woods", "")[0]

    def test_a_real_artist_covering_a_song_is_not_junk(self):
        """Deliberately NOT matching a bare 'cover'. Measured 2026-09-06:
        'cover version' alone would have quarantined these two, and the
        library is full of covers by real artists."""
        assert not is_junk(
            "Morse/portnoy/george",
            "Feelin' Stronger Everyday (2020 Remastered cover version)", "")[0]
        assert not is_junk("Johnny Cash", "Hurt (cover version)", "")[0]
