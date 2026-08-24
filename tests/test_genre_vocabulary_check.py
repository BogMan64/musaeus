"""
The genre check that is not self-certifying.

`GenreLaw.genres` is `set(self._map.values())` — derived from MasterLaw's
own contents. So any value that reaches the law *becomes* vocabulary, and
`permits()` returns True for it forever. Every audit truthfully reported
"0 non-canonical spellings" while `Electronic/Dance` (64 tracks) and
`Classic Rock` (11) sat in the library, absent from Genre_Allowed.txt,
with Genre_Canonical_Map.txt already holding rules to fix both.

The entry point was ScholarStage, which writes the embedded genre tag
verbatim and never consults GenreCanon; EnrichStage then only fills
*empty* genres, so nothing ever revisited them.

Genre_Allowed.txt is hand-written and is what GenreCanon enforces, so it
can disagree with the library. That is precisely what makes it the only
link in the chain capable of failing.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from musaeus.config import MusicConfig
from musaeus.context import RunContext
from musaeus.db import open_db, upsert_archive
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
    return RunContext.new(cfg, open_db(cfg.db_path), dry_run=False)


def _law(ctx, pairs):
    (ctx.config.meta_dir / "MasterLaw.csv").write_bytes(
        ("artist,genre\r\n" + "".join(f"{a},{g}\r\n" for a, g in pairs)).encode()
    )


def _vocab(ctx, genres):
    (ctx.config.meta_dir / "Genre_Allowed.txt").write_text(
        "# vocabulary\n" + "\n".join(genres) + "\n", encoding="utf-8"
    )


def _track(ctx, artist, genre):
    upsert_archive(
        ctx.conn,
        {
            "file_path": f"/x/{artist}.m4a",
            "status": "CATALOGUED",
            "artist": artist,
            "genre": genre,
            "title": "t",
        },
    )
    ctx.conn.commit()


class TestVocabularyCheckCatchesWhatPermitsCannot:
    def test_a_genre_the_law_contains_but_the_vocabulary_does_not_is_reported(self, ctx):
        # The exact live case. The law blesses it because the law contains it.
        _law(ctx, [("Some Band", "Electronic/Dance")])
        _vocab(ctx, ["Disco/Electronic", "Rock"])
        _track(ctx, "Some Band", "Electronic/Dance")

        problems = GenreValidateStage().verify_effect(ctx, GenreValidateStage()._make_result())

        assert any("Genre_Allowed.txt" in p for p in problems)
        assert any("Electronic/Dance" in p for p in problems)

    def test_permits_alone_would_have_passed_it(self, ctx):
        # Proves the gap is real rather than hypothetical.
        from musaeus.canon.genre_law import GenreLaw

        _law(ctx, [("Some Band", "Electronic/Dance")])
        law = GenreLaw(ctx.config.meta_dir / "MasterLaw.csv")
        assert law.permits("Electronic/Dance") is True

    def test_a_listed_genre_is_not_reported(self, ctx):
        _law(ctx, [("Some Band", "Rock")])
        _vocab(ctx, ["Rock", "Disco/Electronic"])
        _track(ctx, "Some Band", "Rock")

        problems = GenreValidateStage().verify_effect(ctx, GenreValidateStage()._make_result())

        assert not any("Genre_Allowed.txt" in p for p in problems)

    def test_a_missing_vocabulary_file_disables_the_check_rather_than_failing_everything(
        self, ctx
    ):
        # A vault without the file must not have every genre reported as
        # unlisted — that would be a check that always fires, which is as
        # useless as one that never does.
        _law(ctx, [("Some Band", "Rock")])
        _track(ctx, "Some Band", "Rock")

        problems = GenreValidateStage().verify_effect(ctx, GenreValidateStage()._make_result())

        assert not any("Genre_Allowed.txt" in p for p in problems)

    def test_the_vocabulary_is_read_from_the_file_not_from_the_law(self, ctx):
        # If this ever starts deriving from MasterLaw it becomes
        # self-certifying again and silently stops working.
        _vocab(ctx, ["Rock", "Jazz"])
        assert GenreValidateStage._allowed_vocabulary(ctx) == {"Rock", "Jazz"}
