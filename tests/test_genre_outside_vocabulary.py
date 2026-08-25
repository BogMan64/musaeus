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
from musaeus.db import open_db, upsert_archive
from musaeus.stages.genre_validate import GenreValidateStage


@pytest.fixture
def ctx(tmp_path) -> RunContext:
    cfg = MusicConfig(
        vault_root=tmp_path, inbox=tmp_path / "INBOX", staging=tmp_path / "STAGING",
        quarantine=tmp_path / "QUARANTINE", runs_root=tmp_path / "RUNS",
        meta_dir=tmp_path / "MetaData", alac_library=tmp_path / "ALAC-Library",
        db_path=tmp_path / "musaeus.db",
    )
    cfg.meta_dir.mkdir(parents=True, exist_ok=True)
    (cfg.meta_dir / "Genre_Allowed.txt").write_text(
        "# vocabulary\nAlternative\nRock\nPop\nClassical\n", encoding="utf-8"
    )
    (cfg.meta_dir / "Genre_Canonical_Map.txt").write_text(
        "Pop Rock => Pop\n", encoding="utf-8"
    )
    (cfg.meta_dir / "MasterLaw.csv").write_bytes(
        b"artist,genre\r\nBarenaked Ladies,Alternative\r\nNobody In Law,Rock\r\n"
    )
    return RunContext.new(cfg, open_db(cfg.db_path), dry_run=False)


def _track(ctx, artist, genre, name="t.m4a"):
    p = ctx.config.alac_library / name
    p.parent.mkdir(parents=True, exist_ok=True)
    upsert_archive(ctx.conn, {"file_path": str(p), "status": "CATALOGUED",
                              "artist": artist, "title": "T", "genre": genre})
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
    def test_a_legal_genre_that_merely_disagrees_with_the_law_is_untouched(self, ctx):
        # §4.19: the library holds the owner's decision. "Rock" is in the
        # vocabulary, so a disagreement with the law stays report-only.
        _track(ctx, "Barenaked Ladies", "Rock")
        result = GenreValidateStage().run(ctx)
        assert _genre(ctx) == "Rock"
        assert any("CONFLICTS (report only)" in n for n in result.notes)

    def test_dry_run_changes_nothing(self, ctx):
        _track(ctx, "Barenaked Ladies", "Pop, Rock")
        GenreValidateStage().dry_run(ctx)
        assert _genre(ctx) == "Pop, Rock"

    def test_a_vault_with_no_vocabulary_file_corrects_nothing(self, ctx):
        (ctx.config.meta_dir / "Genre_Allowed.txt").unlink()
        _track(ctx, "Barenaked Ladies", "Pop, Rock")
        GenreValidateStage().run(ctx)
        assert _genre(ctx) == "Pop, Rock"
