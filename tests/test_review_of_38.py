"""The cloud review of #38 (2026-09-25), the findings still open after #39.

The review ran on #38's head. #39 had since made every writer store the
natural article form ("The Revels", not "Revels, The"), so that half of
findings 1 and 2 is already fixed and not tested again here. What stays open
has one root: each writer applied a PART of Normalize's rule -- the
every-word capitals -- and not the rest (protected spellings, the ALL-CAPS
repair), so writer and Normalize still undid each other. Every writer now
calls normalize.stored_artist / stored_title, Normalize's own rule.

Not changed, with reasons: finding 8 (a case-insensitive disk -- the vault is
ext4) and finding 12 ("with" and "x" as joiners -- the folder rule already
ignores their capitals, and in a song title "with" is an ordinary word).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from musaeus.approval import generate_artist_review
from musaeus.config import MusicConfig
from musaeus.context import RunContext, StageResult
from musaeus.db import open_db, upsert_archive
from musaeus.filing import FilingError, folder_for
from musaeus.filing import load as filing_load
from musaeus.stages.artist_consolidate import CANON_ARTIST_DISPLAY, ArtistConsolidateStage
from musaeus.stages.classical_composer import ClassicalComposerStage
from musaeus.stages.normalize import NormalizeStage
from musaeus.stages.organize import OrganizeStage, library_relpath
from musaeus.stages.various_artists_fix import VariousArtistsFixStage
from musaeus.title_case import every_word_capitalised


def _ctx(tmp_path: Path, archive: Path | None = None) -> RunContext:
    cfg = MusicConfig(
        vault_root=tmp_path,
        inbox=tmp_path / "INBOX",
        staging=tmp_path / "STAGING",
        quarantine=tmp_path / "QUARANTINE",
        runs_root=tmp_path / "RUNS",
        meta_dir=tmp_path / "MetaData",
        alac_library=tmp_path / "Libraries" / "ALAC_Library",
        alac_archive=archive or tmp_path / "Libraries" / "ALAC-Archival",
        db_path=tmp_path / "musaeus.db",
    )
    cfg.ensure_dirs()
    return RunContext.new(cfg, open_db(cfg.db_path), dry_run=False)


def _row(ctx: RunContext, path: Path, artist, title, genre="Rock", **extra) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"x")
    upsert_archive(
        ctx.conn,
        {
            "file_path": str(path),
            "status": "CATALOGUED",
            "artist": artist,
            "title": title,
            "album": "Album",
            "genre": genre,
            "audio_hash": path.name,
            **extra,
        },
    )
    ctx.conn.commit()


def _one(ctx: RunContext) -> tuple:
    return tuple(ctx.conn.execute("SELECT artist, title FROM archive").fetchone())


# ── 1: the Various Artists fix writes what Normalize would ───────────────────


def test_1_an_all_caps_various_artists_track_is_stored_settled(tmp_path):
    ctx = _ctx(tmp_path)
    ctx.set("various_artists_no_mb", True)
    _row(
        ctx,
        ctx.inbox / "Various Artists - DEAD OR ALIVE - LOVER COME BACK TO ME.m4a",
        "Various Artists",
        "DEAD OR ALIVE - LOVER COME BACK TO ME",
    )
    VariousArtistsFixStage().run(ctx)
    assert _one(ctx) == ("Dead Or Alive", "Lover Come Back To Me")
    assert NormalizeStage().run(ctx).files_changed == 0, "the next Normalize renamed it"


# ── 2 and 11: the canon pass and the composer step ───────────────────────────


def test_2_a_canon_target_meets_normalizes_protected_spelling(tmp_path):
    ctx = _ctx(tmp_path)
    (ctx.config.meta_dir / "artist_canon.tsv").write_text(
        "walk the moon band\tWalk the Moon\n", encoding="utf-8"
    )
    _row(ctx, ctx.inbox / "a.m4a", "walk the moon band", "Shut Up And Dance")
    ArtistConsolidateStage().run(ctx)
    assert _one(ctx)[0] == "WALK THE MOON"
    assert NormalizeStage().run(ctx).files_changed == 0


def test_11_an_all_caps_composer_is_not_refiled_on_every_run(tmp_path):
    ctx = _ctx(tmp_path)
    (ctx.config.meta_dir / "Composer_Canon.tsv").write_text("bach\tBACH\n", encoding="utf-8")
    _row(ctx, ctx.inbox / "a.m4a", "Bach", "Cello Suite No. 1", genre="Classical")
    ClassicalComposerStage().run(ctx)
    NormalizeStage().run(ctx)
    plan, _ = ClassicalComposerStage()._plan(ctx)
    assert plan == [], "the composer step and Normalize disagree about the name"


def test_the_shared_form_leaves_todays_repairs_alone():
    from musaeus.stages.normalize import stored_artist

    for name in ("AC/DC", "ABBA", "98°", "KC & The Sunshine Band", "2 Live Crew"):
        assert stored_artist(name) == name
    for name in CANON_ARTIST_DISPLAY.values():
        once = stored_artist(name)
        assert stored_artist(once) == once, name


# ── 3: the review sheet compares in the stored form ──────────────────────────


def test_3_the_review_sheet_does_not_propose_undoing_the_capitals(tmp_path):
    ctx = _ctx(tmp_path)
    canon = ctx.config.meta_dir / "artist_canon.tsv"
    canon.write_text("peter, paul and mary\tPeter, Paul and Mary\n", encoding="utf-8")
    _row(ctx, ctx.inbox / "a.m4a", "Peter, Paul And Mary", "Leaving On A Jet Plane")
    out = tmp_path / "artist_review.tsv"
    report = generate_artist_review(ctx.conn, canon, out)
    assert report.generated == 0, out.read_text() if out.exists() else report


# ── 4, 6, 9: one filing lookup, whatever the capitals ────────────────────────


def _filing(tmp_path: Path, text: str) -> dict:
    meta = tmp_path / "MetaData"
    meta.mkdir(parents=True, exist_ok=True)
    (meta / "artist_filing.tsv").write_text(text, encoding="utf-8")
    return filing_load(meta)


def test_4_the_car_refile_and_the_library_agree_on_a_ruled_folder(tmp_path):
    filing = _filing(tmp_path, "Glenn Miller and His Orchestra\tGlenn Miller\n")
    assert folder_for("Glenn Miller And His Orchestra", filing) == "Glenn Miller"


def test_6_a_ruled_credit_and_the_same_artists_other_tracks_share_one_folder(tmp_path):
    filing = _filing(tmp_path, "Hootie & the Blowfish feat. X\tHootie & the Blowfish\n")
    ruled = library_relpath("Hootie & the Blowfish feat. X", None, "Rock", "A", "T", ".m4a", filing)
    plain = library_relpath("Hootie & The Blowfish", None, "Rock", "A", "T", ".m4a", filing)
    assert ruled.parent == plain.parent


def test_9_two_rulings_that_differ_only_in_capitals_are_refused(tmp_path):
    with pytest.raises(FilingError):
        _filing(tmp_path, "X and Y\tA\nX And Y\tB\n")


# ── 5: dotted initials ───────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("given", "want"),
    [
        ("k.d. lang", "K.D. Lang"),
        ("K.d. Lang", "K.D. Lang"),
        ("The Notorious B.I.G", "The Notorious B.I.G"),
    ],
)
def test_5_dotted_initials_are_all_capitals(given, want):
    assert every_word_capitalised(given) == want


# ── 7: the emptied-folder cleanup through a symlinked root ───────────────────


def test_7_the_old_folder_is_removed_when_the_tier_is_reached_by_a_symlink(tmp_path):
    real = tmp_path / "disk" / "ALAC-Archival"
    real.mkdir(parents=True)
    link = tmp_path / "Libraries" / "ALAC-Archival"
    link.parent.mkdir(parents=True)
    link.symlink_to(real)
    ctx = _ctx(tmp_path, archive=link)
    old = real / "Rock" / "Dead or Alive" / "Album" / "Dead or Alive - Song.m4a"
    _row(ctx, old, "Dead Or Alive", "Song")
    ctx.conn.execute(
        "UPDATE archive SET canonicalized_at = datetime('now'), canon_action = 'PASSTHROUGH', "
        "finalized_at = datetime('now')"
    )
    ctx.conn.commit()
    OrganizeStage().run(ctx)
    assert (real / "Rock" / "Dead Or Alive" / "Album" / "Dead Or Alive - Song.m4a").is_file()
    assert not (real / "Rock" / "Dead or Alive").exists(), "the old folder was left behind"


# ── 10: Normalize checks the titles it now rewrites ──────────────────────────


def test_10_normalize_checks_titles_too(tmp_path):
    ctx = _ctx(tmp_path)
    _row(ctx, ctx.inbox / "a.m4a", "Dion", "Early In The Morning")
    stage = NormalizeStage()
    result = stage.run(ctx)
    # Something written around Normalize after it ran -- the case the check exists for.
    ctx.conn.execute("UPDATE archive SET title = 'early in the morning'")
    problems = stage.verify_effect(ctx, result if isinstance(result, StageResult) else result)
    assert any("title" in p for p in problems), problems
