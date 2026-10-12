"""Grey's code review, 2026-10-11, findings 3, 8 and 11: `musaeus review`.

3. A genre Grey approves in a sheet was written without genre_ruled_at, so
   the next Act 1 "corrected" it back to MasterLaw's as a source tag.
8. Every field but year, genre and album was replaced across the whole
   library: a hand-added title fix for "Yesterday" retitled every song
   called "Yesterday".
11. generate read Grey's marks, then truncated the sheet and wrote it in
    place: a crash or full disk mid-write lost the marks.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from musaeus import approval
from musaeus.approval import ReviewEntry, apply_approved_fixes, read_review_tsv, write_review_tsv
from musaeus.config import MusicConfig
from musaeus.context import RunContext
from musaeus.db import ensure_columns, open_db, upsert_archive
from musaeus.stages.genre_validate import GenreValidateStage


def _entry(**kw) -> ReviewEntry:
    base = {"file_path": "/x/1.m4a", "field_name": "genre", "current_value": "Rock",
            "suggested_value": "Pop", "source": "missing", "confidence": 1.0,
            "approve": "yes"}  # fmt: skip
    base.update(kw)
    return ReviewEntry(**base)


@pytest.fixture
def ctx(tmp_path: Path) -> RunContext:
    meta = tmp_path / "MetaData"
    meta.mkdir()
    (meta / "MasterLaw.csv").write_bytes(
        b"artist,genre\r\nJohn Lennon,Rock\r\nPhil Collins,Rock\r\n"
    )
    (meta / "Genre_Allowed.txt").write_text("Rock\nPop\n", encoding="utf-8")
    cfg = MusicConfig(
        vault_root=tmp_path,
        inbox=tmp_path / "INBOX",
        staging=tmp_path / "STAGING",
        quarantine=tmp_path / "QUARANTINE",
        runs_root=tmp_path / "RUNS",
        meta_dir=meta,
        alac_library=tmp_path / "ALAC-Library",
        db_path=tmp_path / "musaeus.db",
    )
    c = RunContext.new(cfg, open_db(cfg.db_path), dry_run=False)
    # As on the live vault, where GenreValidate has long since added it.
    ensure_columns(c.conn, (("genre_ruled_at", "TEXT"),))
    return c


def _row(ctx: RunContext, path: str, artist: str, genre: str, title: str = "t") -> None:
    upsert_archive(ctx.conn, {"file_path": path, "status": "CATALOGUED", "artist": artist,
                              "genre": genre, "title": title})  # fmt: skip
    ctx.conn.commit()


class TestAnApprovedGenreIsGreysRuling:
    @pytest.mark.parametrize("artist", ["John Lennon", "Phil Collins, Marilyn Martin"])
    def test_genre_validate_leaves_it_alone(self, ctx, tmp_path, artist):
        # The exact credit, and one that reaches the law through its lead.
        _row(ctx, "/x/1.m4a", artist, "Rock")
        sheet = tmp_path / "album_review.tsv"
        write_review_tsv(sheet, [_entry()])

        report = apply_approved_fixes(ctx.conn, sheet, "run1")
        ctx.conn.commit()
        assert report.applied == 1
        ruled = ctx.conn.execute("SELECT genre_ruled_at FROM archive").fetchone()[0]
        assert ruled, "an approved genre must be marked as Grey's ruling"

        GenreValidateStage().execute(ctx)

        assert ctx.conn.execute("SELECT genre FROM archive").fetchone()[0] == "Pop"
        corrected = ctx.conn.execute(
            "SELECT COUNT(*) FROM events WHERE event_type = 'GENRE_CORRECTED_FROM_LAW'"
        ).fetchone()[0]
        assert corrected == 0


class TestOnlyArtistRowsReachTheWholeLibrary:
    def test_a_hand_added_title_fix_changes_its_one_file(self, ctx, tmp_path):
        _row(ctx, "/x/1.m4a", "The Beatles", "Rock", title="Yesterday")
        _row(ctx, "/x/2.m4a", "Boyz II Men", "Pop", title="Yesterday")
        sheet = tmp_path / "album_review.tsv"
        write_review_tsv(sheet, [_entry(field_name="title", current_value="Yesterday",
                                        suggested_value="Yesterday (Remastered 2009)")])  # fmt: skip

        report = apply_approved_fixes(ctx.conn, sheet, "run1")

        titles = dict(ctx.conn.execute("SELECT file_path, title FROM archive").fetchall())
        assert titles == {"/x/1.m4a": "Yesterday (Remastered 2009)", "/x/2.m4a": "Yesterday"}
        assert report.applied == 1

    def test_an_artist_row_still_reaches_every_row(self, ctx, tmp_path):
        _row(ctx, "/x/1.m4a", "Beetles", "Rock")
        _row(ctx, "/x/2.m4a", "Beetles", "Rock")
        sheet = tmp_path / "artist_review.tsv"
        write_review_tsv(sheet, [_entry(field_name="artist", current_value="Beetles",
                                        suggested_value="The Beatles")])  # fmt: skip

        apply_approved_fixes(ctx.conn, sheet, "run1")

        assert {r[0] for r in ctx.conn.execute("SELECT artist FROM archive")} == {"The Beatles"}


class TestAWriteCutShortKeepsTheMarks:
    def test_the_old_sheet_survives_and_no_part_file_is_left(self, tmp_path, monkeypatch):
        sheet = tmp_path / "artist_review.tsv"
        write_review_tsv(sheet, [_entry(approve="yes")])
        before = sheet.read_bytes()

        class DiskFull(approval.csv.DictWriter):
            def writerow(self, row):
                raise OSError(28, "No space left on device")

        monkeypatch.setattr(approval.csv, "DictWriter", DiskFull)
        with pytest.raises(OSError):
            write_review_tsv(sheet, [_entry(approve=""), _entry(file_path="/x/2.m4a")])

        assert sheet.read_bytes() == before, "Grey's marks were lost"
        assert read_review_tsv(sheet)[0].approve == "yes"
        assert sorted(p.name for p in tmp_path.iterdir()) == ["artist_review.tsv"]

    def test_a_sheet_that_does_not_read_back_whole_is_not_used(self, tmp_path, monkeypatch):
        sheet = tmp_path / "artist_review.tsv"
        write_review_tsv(sheet, [_entry(approve="yes")])
        before = sheet.read_bytes()
        real = approval._write_rows
        monkeypatch.setattr(approval, "_write_rows", lambda p, e: real(p, e[:1]))

        with pytest.raises(OSError, match="did not read back whole"):
            write_review_tsv(sheet, [_entry(), _entry(file_path="/x/2.m4a")])

        assert sheet.read_bytes() == before


class TestAnApprovedGenreMustBeAGenre:
    """Pre-merge review of #180: a genre row approved blank or misspelled was
    stamped, and GenreValidate then wrote the law's genre under the stamp."""

    @pytest.mark.parametrize(
        ("typed", "why"), [("", "none typed"), ("Rokc", "not in Genre_Allowed")]
    )
    def test_refused_and_said_why(self, ctx, tmp_path, typed, why):
        _row(ctx, "/x/1.m4a", "John Lennon", "")
        sheet = tmp_path / "album_review.tsv"
        write_review_tsv(sheet, [_entry(current_value="", suggested_value=typed)])

        report = apply_approved_fixes(ctx.conn, sheet, "run1", allowed_genres={"Rock", "Pop"})

        assert report.applied == 0 and any(why in e for e in report.errors)
        row = ctx.conn.execute("SELECT genre, genre_ruled_at FROM archive").fetchone()
        assert tuple(row) == ("", None)

    def test_a_case_slip_is_written_as_listed(self, ctx, tmp_path):
        _row(ctx, "/x/1.m4a", "John Lennon", "Rock")
        sheet = tmp_path / "album_review.tsv"
        write_review_tsv(sheet, [_entry(suggested_value="pop")])

        report = apply_approved_fixes(ctx.conn, sheet, "run1", allowed_genres={"Rock", "Pop"})

        assert report.applied == 1
        assert ctx.conn.execute("SELECT genre FROM archive").fetchone()[0] == "Pop"
