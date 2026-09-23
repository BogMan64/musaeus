"""
A genre outside the closed vocabulary is corrected, not merely reported.

Library-vs-law conflicts stay report-only on purpose: the library holds
the owner's decision and the law follows it (scope §4.19). But a value the
vocabulary does not contain is not a disagreement — it is not a genre, and
there is no decision to protect.

Measured on a five-file test batch, 2026-08-25: "Pop, Rock" — drained to
zero and retired the day before — came back on the very first newly
ingested file. ScholarStage writes the file's own genre tag verbatim and
never consults GenreCanon, and this stage ran and left it, because it only
ever filled EMPTY genres. Retirement was a cleanup that had to be
repeated rather than a rule that held.
"""

from __future__ import annotations

import pytest

from musaeus.config import MusicConfig
from musaeus.context import RunContext
from musaeus.db import ensure_columns, open_db, upsert_archive
from musaeus.stages.genre_validate import GenreValidateStage


@pytest.fixture
def ctx(tmp_path) -> RunContext:
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
    (cfg.meta_dir / "Genre_Allowed.txt").write_text(
        "# vocabulary\nAlternative\nRock\nPop\nClassical\n", encoding="utf-8"
    )
    (cfg.meta_dir / "Genre_Canonical_Map.txt").write_text("Pop Rock => Pop\n", encoding="utf-8")
    (cfg.meta_dir / "MasterLaw.csv").write_bytes(
        b"artist,genre\r\nBarenaked Ladies,Alternative\r\nNobody In Law,Rock\r\n"
    )
    return RunContext.new(cfg, open_db(cfg.db_path), dry_run=False)


def _track(ctx, artist, genre, name="t.m4a"):
    p = ctx.config.alac_library / name
    p.parent.mkdir(parents=True, exist_ok=True)
    upsert_archive(
        ctx.conn,
        {
            "file_path": str(p),
            "status": "CATALOGUED",
            "artist": artist,
            "title": "T",
            "genre": genre,
        },
    )
    ctx.conn.commit()


def _genre(ctx):
    return ctx.conn.execute("SELECT genre FROM archive").fetchone()["genre"]


class TestARetiredGenreDoesNotSurvive:
    def test_it_is_replaced_using_the_law(self, ctx):
        _track(ctx, "Barenaked Ladies", "Pop, Rock")
        GenreValidateStage().run(ctx)
        assert _genre(ctx) == "Alternative"

    def test_the_correction_is_recorded(self, ctx):
        _track(ctx, "Barenaked Ladies", "Pop, Rock")
        GenreValidateStage().run(ctx)
        ev = ctx.conn.execute(
            "SELECT old_value, new_value FROM events WHERE event_type='GENRE_OUTSIDE_VOCABULARY'"
        ).fetchone()
        assert ev["old_value"] == "Pop, Rock"
        assert ev["new_value"] == "Alternative"

    def test_the_canon_map_is_used_when_the_law_cannot_help(self, ctx):
        # No law rule for this artist, but the canon knows the value.
        _track(ctx, "Someone Unknown", "Pop Rock")
        GenreValidateStage().run(ctx)
        assert _genre(ctx) == "Pop"

    def test_an_unresolvable_value_is_left_and_reported(self, ctx):
        # Nothing invents a genre. A value with no law rule and no canon
        # entry is surfaced for a ruling rather than guessed at.
        _track(ctx, "Someone Unknown", "Zzz Not A Genre")
        result = GenreValidateStage().run(ctx)
        assert _genre(ctx) == "Zzz Not A Genre"
        assert any("unresolvable" in n for n in result.notes)


class TestItDoesNotOverreach:
    def test_a_ruled_genre_that_disagrees_with_the_law_is_untouched(self, ctx):
        """§4.19, narrowed 2026-09-14: the library holds the owner's decision
        -- where the owner actually made one.

        genre_ruled_at set means this conflict has been seen and a side
        chosen. That IS a decision, and it stays report-only forever.
        """
        _track(ctx, "Barenaked Ladies", "Rock")
        # The stage adds this column lazily on its first live run; a row that
        # already carries a ruling has to exist before the stage sees it.
        ensure_columns(ctx.conn, (("genre_ruled_at", "TEXT"),))
        ctx.conn.execute("UPDATE archive SET genre_ruled_at = '2026-09-01T00:00:00'")
        ctx.conn.commit()

        result = GenreValidateStage().run(ctx)
        assert _genre(ctx) == "Rock"
        assert any("CONFLICTS (report only" in n for n in result.notes)

    def test_an_unruled_genre_that_disagrees_with_the_law_loses_to_the_law(self, ctx):
        """The other half of that narrowing, and the reason for it.

        ScholarStage writes the file's embedded genre tag verbatim and never
        consults MasterLaw, so a row nobody has reviewed holds whatever
        iTunes or the rip said. Treating that as "the owner's decision" made
        a stranger's tag outrank Grey's own law permanently, and left a
        conflict no one could resolve -- the same shape as the "Pop, Rock"
        failure in this module's docstring.
        """
        _track(ctx, "Barenaked Ladies", "Rock")
        ensure_columns(ctx.conn, (("genre_ruled_at", "TEXT"),))
        assert (
            ctx.conn.execute(
                "SELECT COUNT(*) FROM archive WHERE genre_ruled_at IS NOT NULL"
            ).fetchone()[0]
            == 0
        ), "a fresh row must carry no ruling"

        result = GenreValidateStage().run(ctx)
        assert _genre(ctx) == "Alternative", "MasterLaw wins over an unreviewed source tag"
        assert result.files_changed == 1
        # and the correction records itself, so the row is not re-corrected later
        assert ctx.conn.execute("SELECT genre_ruled_at FROM archive").fetchone()[0], (
            "the correction must stamp a ruling"
        )

    def test_dry_run_changes_nothing(self, ctx):
        _track(ctx, "Barenaked Ladies", "Pop, Rock")
        GenreValidateStage().dry_run(ctx)
        assert _genre(ctx) == "Pop, Rock"

    def test_a_vault_with_no_vocabulary_file_corrects_nothing(self, ctx):
        (ctx.config.meta_dir / "Genre_Allowed.txt").unlink()
        _track(ctx, "Barenaked Ladies", "Pop, Rock")
        GenreValidateStage().run(ctx)
        assert _genre(ctx) == "Pop, Rock"
