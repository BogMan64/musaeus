"""GenreValidate's self-check judges this run's own writes, by its own rules.

Grey's code review, 2026-10-11, finding 1. The check judged the whole
library against MasterLaw's genre set, so a genre the stage deliberately
keeps -- a Genre_Allowed genre for an act MasterLaw does not know, or a
stamped ruling -- failed it. Since #166 a failed check fails the run: on a
copy of the live catalogue GenreValidate changed 506 rows, every one of them
correctly, and then failed on 'Classic Rock' (twenty John Lennon masters,
eighteen of them stamped before MasterLaw moved him to Rock).

These run the stage through BaseStage.execute, the way a pipeline does, so
"failed" means what it means in a real run.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from musaeus.config import MusicConfig
from musaeus.context import RunContext
from musaeus.db import ensure_columns, open_db, upsert_archive
from musaeus.stages.genre_validate import GenreValidateStage


@pytest.fixture
def ctx(tmp_path: Path) -> RunContext:
    meta = tmp_path / "MetaData"
    meta.mkdir()
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
    (meta / "MasterLaw.csv").write_bytes(
        b"artist,genre\r\nJohn Lennon,Rock\r\nThe Beatles,Rock\r\nCarole King,Pop\r\n"
    )
    (meta / "Genre_Allowed.txt").write_text(
        "# vocabulary\nRock\nPop\nClassic Rock\nJazz\n", encoding="utf-8"
    )
    c = RunContext.new(cfg, open_db(cfg.db_path), dry_run=False)
    ensure_columns(c.conn, (("genre_ruled_at", "TEXT"),))
    return c


def _track(ctx: RunContext, name: str, artist: str, genre: str, ruled: str | None = None) -> None:
    upsert_archive(
        ctx.conn,
        {
            "file_path": f"/lib/{name}.m4a",
            "status": "CATALOGUED",
            "artist": artist,
            "genre": genre,
            "title": name,
        },
    )
    if ruled:
        ctx.conn.execute(
            "UPDATE archive SET genre_ruled_at = ? WHERE file_path = ?", (ruled, f"/lib/{name}.m4a")
        )
    ctx.conn.commit()


def _genre(ctx: RunContext, name: str) -> str:
    return ctx.conn.execute(
        "SELECT genre FROM archive WHERE file_path = ?", (f"/lib/{name}.m4a",)
    ).fetchone()[0]


class TestWhatTheStageKeepsOnPurposeIsNotAFailure:
    def test_an_allowed_genre_for_an_act_the_law_does_not_know(self, ctx):
        # The review's second case: a new master by an act MasterLaw does not
        # know, tagged Classic Rock (allowed, but no MasterLaw line uses it),
        # failed the stage on a run where it changed one row.
        _track(ctx, "new", "Some New Act", "Classic Rock")
        _track(ctx, "fix", "Carole King", "Rock")  # the one row this run changes

        result = GenreValidateStage().execute(ctx)

        assert result.files_changed == 1
        assert _genre(ctx, "fix") == "Pop"
        assert _genre(ctx, "new") == "Classic Rock"
        assert result.success, result.errors
        assert result.verified is True, result.verify_notes

    def test_a_stamped_ruling_in_a_genre_the_law_never_uses(self, ctx):
        # The live John Lennon case: a ruling stamp from before MasterLaw
        # moved him to Rock. The stage leaves it (report only); the check
        # must not then fail the run for it.
        _track(ctx, "imagine", "John Lennon", "Classic Rock", ruled="2026-10-02T14:28:13Z")
        _track(ctx, "mother", "John Lennon", "Pop")  # unstamped: corrected to Rock

        result = GenreValidateStage().execute(ctx)

        assert _genre(ctx, "imagine") == "Classic Rock"
        assert _genre(ctx, "mother") == "Rock"
        assert result.success, result.errors
        assert result.verified is True, result.verify_notes
        assert any("ruling" in n and "John Lennon" in n for n in result.notes)


class TestTheCheckStillCatchesAWriteThatDidNotLand:
    def test_a_partial_write(self, ctx):
        # 900 rows consolidated and 3 missed: the defect the check exists for.
        for i in range(5):
            _track(ctx, f"t{i}", "The Beatles", "Pop")
        stage = GenreValidateStage()
        result = stage.execute(ctx)
        assert result.success and result.verified is True

        # Undo one write behind the stage's back, then ask again.
        ctx.conn.execute("UPDATE archive SET genre='Pop' WHERE file_path='/lib/t3.m4a'")
        problems = stage.verify_effect(ctx, result)

        assert len(problems) == 1
        assert "1 row(s) still need" in problems[0]
        assert "The Beatles" in problems[0] and "'Pop' -> 'Rock'" in problems[0]

    def test_a_run_whose_writes_never_landed(self, ctx, monkeypatch):
        _track(ctx, "a", "The Beatles", "Pop")
        _track(ctx, "b", "Carole King", "")
        stage = GenreValidateStage()
        real = stage._check

        def writes_vanish(c, dry_run):
            result = real(c, dry_run)
            c.conn.execute("UPDATE archive SET genre='Pop' WHERE file_path='/lib/a.m4a'")
            c.conn.execute("UPDATE archive SET genre='' WHERE file_path='/lib/b.m4a'")
            return result

        monkeypatch.setattr(stage, "_check", writes_vanish)
        result = stage.execute(ctx)

        assert result.files_changed == 2
        assert result.success is False
        assert result.verified is False
        assert any("2 row(s) still need" in n for n in result.verify_notes)

    def test_a_genre_written_outside_the_vocabulary(self, ctx):
        _track(ctx, "a", "The Beatles", "Pop")
        stage = GenreValidateStage()
        result = stage.execute(ctx)
        stage._written_genres = frozenset({"Rock", "Bogus"})

        problems = stage.verify_effect(ctx, result)

        assert problems and "'Bogus'" in problems[0] and "Genre_Allowed.txt" in problems[0]


class TestALawGenreOutsideTheVocabularyIsNotFilledIn:
    def test_the_blank_stays_blank_and_masterlaw_is_named(self, ctx):
        # Writing it would put an unlisted genre into the library, and the
        # next run would "correct" the stage's own fill.
        (ctx.config.meta_dir / "MasterLaw.csv").write_bytes(
            b"artist,genre\r\nSome Band,Electronic/Dance\r\nCarole King,Pop\r\n"
        )
        _track(ctx, "a", "Some Band", "")
        _track(ctx, "b", "Carole King", "")

        result = GenreValidateStage().execute(ctx)

        assert _genre(ctx, "a") == ""
        assert _genre(ctx, "b") == "Pop"
        assert result.success and result.verified is True
        assert any("'Electronic/Dance'" in n for n in result.notes)


class TestConsolidateIsCheckedByItsOwnRule:
    def test_a_tie_left_alone_is_not_a_failure(self, ctx):
        _track(ctx, "a", "Unknown Act", "Rock")
        _track(ctx, "b", "Unknown Act", "Pop")  # a tie: left for a human
        _track(ctx, "c", "Other Act", "Rock")
        _track(ctx, "d", "Other Act", "Rock")
        _track(ctx, "e", "Other Act", "Pop")  # majority Rock: consolidated
        ctx.set("genre_consolidate", True)

        result = GenreValidateStage().execute(ctx)

        assert _genre(ctx, "e") == "Rock"
        assert result.success, result.errors
        assert result.verified is True, result.verify_notes


class TestOneRunIsEnough:
    """The self-check runs the stage's rules again, so the rules must settle
    every row in one run: a second run that still writes means the first
    failed its own check (pre-merge review of #178)."""

    @pytest.mark.parametrize("credit", ["Carole King, Friend", "Carole King feat. Friend"])
    def test_a_credit_with_an_unlisted_genre_takes_its_leads_genre_at_once(self, ctx, credit):
        (ctx.config.meta_dir / "Genre_Canonical_Map.txt").write_text(
            "pop, rock => Rock\n", encoding="utf-8"
        )
        _track(ctx, "a", credit, "pop")  # a case variant: resolves to 'Pop'
        _track(ctx, "b", credit, "Pop, Rock")  # a mapped value: resolves to 'Rock'

        first = GenreValidateStage().execute(ctx)
        second = GenreValidateStage().execute(ctx)

        assert first.success and first.verified is True, first.verify_notes
        assert (_genre(ctx, "a"), _genre(ctx, "b")) == ("Pop", "Pop")  # Carole King: Pop
        assert second.files_changed == 0


class TestConsolidateKeepsItsFindings:
    def test_an_unlisted_majority_winner_fails_and_the_notes_are_kept(self, ctx):
        _track(ctx, "a", "Unknown Act", "Pop, Rock")
        _track(ctx, "b", "Unknown Act", "Pop, Rock")
        _track(ctx, "c", "Unknown Act", "Rock")
        ctx.set("genre_consolidate", True)

        result = GenreValidateStage().execute(ctx)

        assert _genre(ctx, "c") == "Pop, Rock"
        assert result.verified is False
        assert any("'Pop, Rock'" in v and "Genre_Allowed" in v for v in result.verify_notes)
        assert any("absent from Genre_Allowed.txt" in n for n in result.notes)
