"""What the 225-track batch of 2026-09-25 turned up, one test per finding.

names       Two Backstreet Boys files arrived with no tags at all and were
            catalogued with no artist and no title, though the file names
            said exactly what they were.
genres      Duet and comma credits ("Phil Collins, Marilyn Martin") had no
            MasterLaw entry of their own and were left without a genre,
            though the lead artist had a ruling.
review      A copy already set aside in REVIEW/DUPES_MOVED was flagged again
            by cross-dupe and "moved" onto itself, gaining " (2)". The guard
            matched on file path, which the move changed, so every Act 2
            would have done it again.
interrupt   Act 3 was interrupted after Finalize had filed 178 tracks. The
            run records are copied beside the library at the END of a run,
            which that run never reached.
label       "needs attention" was printed after every run, clean or not.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from musaeus import cli
from musaeus.config import MusicConfig
from musaeus.context import RunContext, StageResult
from musaeus.db import open_db, open_hash_index, record_finalized_hash, upsert_archive
from musaeus.stages.base import BaseStage
from musaeus.stages.cross_dupe import CrossDupeStage
from musaeus.stages.dupe_resolver import DupeResolverStage
from musaeus.stages.genre_validate import GenreValidateStage
from musaeus.stages.scholar import ScholarStage


def _cfg(tmp_path: Path) -> MusicConfig:
    cfg = MusicConfig(
        vault_root=tmp_path,
        inbox=tmp_path / "INBOX",
        staging=tmp_path / "STAGING",
        quarantine=tmp_path / "QUARANTINE",
        runs_root=tmp_path / "RUNS",
        meta_dir=tmp_path / "MetaData",
        alac_library=tmp_path / "Libraries" / "ALAC_Library",
        alac_archive=tmp_path / "Libraries" / "ALAC-Archival",
        db_path=tmp_path / "musaeus.db",
    )
    cfg.ensure_dirs()
    return cfg


def _ctx(tmp_path: Path) -> RunContext:
    cfg = _cfg(tmp_path)
    return RunContext.new(cfg, open_db(cfg.db_path), dry_run=False)


def _row(ctx: RunContext, path: Path, status: str, h: str, **fields) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"audio")
    upsert_archive(ctx.conn, {"file_path": str(path), "status": status, "audio_hash": h, **fields})
    ctx.conn.commit()


# ── names: the file name speaks when the tags do not ─────────────────────────

_NO_TAGS = {"format": {"duration": "200"}, "streams": [{"codec_type": "audio"}]}


def _scholar(ctx: RunContext, monkeypatch, filename: str, probe=_NO_TAGS) -> tuple:
    import musaeus.stages.scholar as scholar_mod

    monkeypatch.setattr(scholar_mod, "_probe", lambda _p: probe)
    _row(ctx, ctx.inbox / filename, "HASHED", "h-" + filename)
    ScholarStage().run(ctx)
    return ctx.conn.execute(
        "SELECT artist, title FROM archive WHERE file_path = ?", (str(ctx.inbox / filename),)
    ).fetchone()


@pytest.mark.parametrize(
    ("filename", "artist", "title"),
    [
        ("Backstreet Boys - I Want It That Way.m4a", "Backstreet Boys", "I Want It That Way"),
        # A dotted number is a playlist position, not part of the name.
        ("90. Paul, Paula - Hey Paula.m4a", "Paul, Paula", "Hey Paula"),
        (
            "573. Phil Collins, Marilyn Martin - Separate Lives (2016 Remaster).m4a",
            "Phil Collins, Marilyn Martin",
            "Separate Lives (2016 Remaster)",
        ),
        # A number with no dot IS the name. All of these are in the library,
        # and the last two are how 10,000 Maniacs and 98 Degrees actually
        # arrive (see artist_canon.tsv).
        ("50 Cent - In da Club.m4a", "50 Cent", "In da Club"),
        ("311 - Amber.m4a", "311", "Amber"),
        ("10,000 Maniacs - Because the Night.m4a", "10,000 Maniacs", "Because the Night"),
        ("10 - Candy Everybody Wants.m4a", "10", "Candy Everybody Wants"),
        ("98 - True to Your Heart.m4a", "98", "True to Your Heart"),
        # Split on the FIRST " - " only: the rest belongs to the title.
        ("Dion - Runaround Sue - Live.m4a", "Dion", "Runaround Sue - Live"),
        # A collision suffix is the pipeline's, not the song's.
        ("Dion - Runaround Sue (2).m4a", "Dion", "Runaround Sue"),
    ],
)
def test_names_an_untagged_file_is_named_from_its_file_name(
    tmp_path, monkeypatch, filename, artist, title
):
    ctx = _ctx(tmp_path)
    assert tuple(_scholar(ctx, monkeypatch, filename)) == (artist, title)


def test_names_the_pipelines_own_placeholder_is_never_read_as_a_name(tmp_path, monkeypatch):
    ctx = _ctx(tmp_path)
    got = _scholar(ctx, monkeypatch, "Unknown Artist - Unknown Title (3).m4a")
    assert tuple(got) == (None, None)


def test_names_a_file_with_no_separator_is_left_for_a_human(tmp_path, monkeypatch):
    ctx = _ctx(tmp_path)
    assert tuple(_scholar(ctx, monkeypatch, "Just A Title.m4a")) == (None, None)


def test_names_tags_win_over_the_file_name(tmp_path, monkeypatch):
    ctx = _ctx(tmp_path)
    probe = {
        "format": {"tags": {"artist": "Dion", "title": "The Wanderer"}},
        "streams": [{"codec_type": "audio"}],
    }
    got = _scholar(ctx, monkeypatch, "Someone Else - Another Song.m4a", probe)
    assert tuple(got) == ("Dion", "The Wanderer")


def test_names_one_tag_present_means_the_file_is_tagged(tmp_path, monkeypatch):
    # Only a file with NEITHER tag is untagged. A lone title is a real tag,
    # and half-filling from the file name would mix two sources in one row.
    ctx = _ctx(tmp_path)
    probe = {"format": {"tags": {"title": "The Wanderer"}}, "streams": [{"codec_type": "audio"}]}
    got = _scholar(ctx, monkeypatch, "Dion - Runaround Sue.m4a", probe)
    assert tuple(got) == (None, "The Wanderer")


def test_names_the_raw_probe_record_still_says_what_the_tags_said(tmp_path, monkeypatch):
    ctx = _ctx(tmp_path)
    _scholar(ctx, monkeypatch, "Backstreet Boys - Shape of My Heart.m4a")
    cache = ctx.conn.execute("SELECT artist, title FROM metadata_cache").fetchone()
    assert tuple(cache) == (None, None), "metadata_cache is the audit trail of the file's own tags"
    note = ctx.conn.execute(
        "SELECT note FROM events WHERE event_type = 'METADATA_EXTRACTED'"
    ).fetchone()[0]
    assert "file name" in note


# ── genres: a credit with no ruling of its own borrows its lead artist's ────


def _genre_ctx(tmp_path: Path, law: str) -> RunContext:
    ctx = _ctx(tmp_path)
    (ctx.config.meta_dir / "MasterLaw.csv").write_text("artist,genre\n" + law, encoding="utf-8")
    return ctx


def _genres(ctx: RunContext, *artists: str) -> tuple[StageResult, dict]:
    for i, a in enumerate(artists):
        _row(ctx, ctx.inbox / f"{i}.m4a", "CATALOGUED", f"g{i}", artist=a, title=f"T{i}")
    result = GenreValidateStage().run(ctx)
    got = dict(ctx.conn.execute("SELECT artist, genre FROM archive").fetchall())
    return result, got


def test_genres_a_duet_takes_its_lead_artists_genre(tmp_path):
    ctx = _genre_ctx(tmp_path, "Phil Collins,Rock\nGorillaz,Alternative\n")
    result, got = _genres(ctx, "Phil Collins, Marilyn Martin", "Gorillaz feat. Bobby Womack")
    assert got == {
        "Phil Collins, Marilyn Martin": "Rock",
        "Gorillaz feat. Bobby Womack": "Alternative",
    }
    assert result.files_changed == 2
    assert any("lead artist" in n and "2" in n for n in result.notes), result.notes


def test_genres_the_exact_credit_outranks_the_lead_artist(tmp_path):
    ctx = _genre_ctx(tmp_path, "Paul McCartney,Classic Pop\nPaul McCartney & Stevie Wonder,Rock\n")
    _result, got = _genres(ctx, "Paul McCartney & Stevie Wonder")
    assert got == {"Paul McCartney & Stevie Wonder": "Rock"}


def test_genres_a_band_whose_name_holds_a_comma_still_needs_a_ruling(tmp_path):
    # "Earth" has a ruling of its own here. Earth, Wind & Fire is not Earth
    # with guests, and must not borrow it.
    ctx = _genre_ctx(tmp_path, "Earth,Metal\n")
    result, got = _genres(ctx, "Earth, Wind & Fire")
    assert got == {"Earth, Wind & Fire": None}
    assert any("needs a ruling" in n for n in result.notes), result.notes


# ── review: a copy already set aside is not a new duplicate ──────────────────


def _badfinger(tmp_path: Path) -> tuple[RunContext, Path]:
    """The live shape: the kept master is filed, the older copy is in review."""
    ctx = _ctx(tmp_path)
    master = ctx.config.alac_archive / "Rock" / "Badfinger" / "B" / "Badfinger - Baby Blue.m4a"
    _row(ctx, master, "CATALOGUED", "H", artist="Badfinger", album="B", title="Baby Blue")
    ledger = open_hash_index(ctx.config.hash_index_path)
    record_finalized_hash(ledger, "H", str(master))
    ledger.commit()
    ledger.close()
    review = (
        ctx.config.dupes_review_dir / "2026-09-25" / "Badfinger" / "B" / "Badfinger - Baby Blue.m4a"
    )
    _row(ctx, review, "DUPE_REVIEW", "H", artist="Badfinger", album="B", title="Baby Blue")
    return ctx, review


def test_review_a_copy_in_review_is_left_where_it_is_on_every_run(tmp_path):
    ctx, review = _badfinger(tmp_path)
    for run in range(2):
        CrossDupeStage().run(ctx)
        DupeResolverStage().run(ctx)
        assert review.is_file(), f"review copy moved on run {run + 1}"
        (fp,) = ctx.conn.execute(
            "SELECT file_path FROM archive WHERE status = 'DUPE_REVIEW'"
        ).fetchone()
        assert fp == str(review)
    flagged = ctx.conn.execute("SELECT COUNT(*) FROM duplicates").fetchone()[0]
    assert flagged == 0, "a set-aside copy was flagged as a new duplicate"


@pytest.mark.parametrize("status", ["QUARANTINED", "TRIBUTE_REVIEW", "GHOST"])
def test_review_other_set_aside_rows_are_not_cross_batch_candidates(tmp_path, status):
    ctx, review = _badfinger(tmp_path)
    ctx.conn.execute("UPDATE archive SET status = ? WHERE file_path = ?", (status, str(review)))
    ctx.conn.commit()
    CrossDupeStage().run(ctx)
    assert ctx.conn.execute("SELECT COUNT(*) FROM duplicates").fetchone()[0] == 0


def test_review_the_resolver_never_moves_a_file_already_in_review(tmp_path):
    # Defence in depth: however a review copy comes to be flagged, the
    # resolver must not "move" it onto itself.
    ctx, review = _badfinger(tmp_path)
    ctx.set("finalize_batch_date", "2026-09-25")
    ctx.conn.execute(
        "INSERT INTO duplicates (group_id, file_path, duplicate_type, status, audio_hash) "
        "VALUES ('crossdupe_x', ?, 'CROSS_BATCH', 'pending', 'H')",
        (str(review),),
    )
    ctx.conn.commit()
    DupeResolverStage().run(ctx)
    assert review.is_file()
    assert not list(review.parent.glob("* (2).m4a"))
    pending = ctx.conn.execute(
        "SELECT COUNT(*) FROM duplicates WHERE status = 'pending'"
    ).fetchone()[0]
    assert pending == 0, "the group should be closed, not retried on every run"


# ── interrupt and label: the end-of-run bookkeeping ──────────────────────────


def _stage(name: str, *, changed: int = 0, fail: bool = False, interrupt: bool = False):
    class _Stage(BaseStage):
        NAME = name
        CLAIMS_EFFECT = False

        def validate(self, ctx: RunContext) -> None:
            return None

        def run(self, ctx: RunContext) -> StageResult:
            if interrupt:
                raise KeyboardInterrupt
            result = self._make_result(dry_run=False)
            result.files_changed = changed
            if fail:
                result.success = False
                result.errors.append("forced failure")
            ctx.record_stage(result)
            return result

        def dry_run(self, ctx: RunContext) -> StageResult:
            raise NotImplementedError

    _Stage.__name__ = f"Fake{name.title()}Stage"
    return _Stage


@pytest.fixture
def pipeline(tmp_path, monkeypatch):
    # Both patched, always: an unpatched run clears or writes Grey's real
    # ~/.config/musaeus/resume_state.json, and his next `musaeus run` would
    # skip stages because of a test.
    cfg = _cfg(tmp_path)
    monkeypatch.setattr(cli, "get_config", lambda: cfg)
    monkeypatch.setattr(cli, "_RESUME_FILE", tmp_path / "resume_state.json")
    monkeypatch.setattr(cli.sys.stdin, "isatty", lambda: False, raising=False)
    published = cfg.libraries / "ALAC_Library_Run_Logs"
    return lambda stages: cli._run_pipeline(stages, dry_run=False), published


def test_interrupt_a_run_that_filed_tracks_keeps_its_records_even_when_cut_short(pipeline):
    run, published = pipeline
    assert run([_stage("finalize", changed=178), _stage("forge", interrupt=True)]) == 1
    folders = list(published.glob("run_*")) if published.exists() else []
    assert len(folders) == 1, "the records of a run that added tracks were not kept"
    assert any(p.suffix == ".log" for p in folders[0].iterdir())


def test_interrupt_a_run_that_filed_nothing_publishes_nothing(pipeline):
    run, published = pipeline
    run([_stage("finalize", changed=0), _stage("forge", interrupt=True)])
    assert not (published.exists() and any(published.iterdir()))


def test_label_a_clean_run_does_not_ask_for_attention(pipeline, capsys):
    run, _ = pipeline
    assert run([_stage("health")]) == 0
    out = capsys.readouterr()
    assert "needs attention" not in out.out + out.err


def test_label_a_run_with_problems_does(pipeline, capsys):
    run, _ = pipeline
    run([_stage("health", fail=True)])
    out = capsys.readouterr()
    assert "needs attention" in out.out + out.err
