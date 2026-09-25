"""What the 131-file batch of 2026-09-25 turned up (Grey said yes to each fix).

AC/DC      ArtistConsolidate's display table (copied from ORPHEUS on
           2026-08-08) spelled it "AC-DC". It predates Grey's 2026-08-22
           rule -- match MusicBrainz exactly -- and never learned it, so any
           batch holding two spellings renamed the band "AC-DC".
credits    The grouping key stripped guest credits, so "50 Cent, Nate Dogg"
           and "50 Cent" were one artist and the tag lost Nate Dogg. The
           tag keeps the full credit; only the FOLDER uses the lead artist
           (artist_form.folder_artist).
article    Merging "Band" with "The Band" wrote "Band, The". Normalize has
           stored the natural form since 2026-09-16, so the next run changed
           it back and every such artist was renamed twice.
folders    DupeResolver moved files out of the library and left their
           folders behind: 4 empty album folders in ALAC-Archival from one
           Act 2.
"""

from __future__ import annotations

from pathlib import Path

from musaeus.config import MusicConfig
from musaeus.context import RunContext
from musaeus.db import open_db, upsert_archive
from musaeus.stages.artist_consolidate import ArtistConsolidateStage
from musaeus.stages.dupe_resolver import DupeResolverStage
from musaeus.stages.normalize import NormalizeStage


def _ctx(tmp_path: Path) -> RunContext:
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
    return RunContext.new(cfg, open_db(cfg.db_path), dry_run=False)


def _rows(ctx: RunContext, *artists: str) -> None:
    for i, artist in enumerate(artists):
        p = ctx.inbox / f"{i}.m4a"
        p.write_bytes(b"x")
        upsert_archive(
            ctx.conn,
            {
                "file_path": str(p),
                "status": "CATALOGUED",
                "artist": artist,
                "title": f"Song {i}",
                "audio_hash": f"h{i}",
            },
        )
    ctx.conn.commit()


def _artists(ctx: RunContext) -> list[str]:
    return [r[0] for r in ctx.conn.execute("SELECT artist FROM archive ORDER BY file_path")]


def test_acdc_keeps_its_slash_whichever_spellings_arrive(tmp_path):
    ctx = _ctx(tmp_path)
    _rows(ctx, "AC/DC", "AC/DC", "Ac/dc", "AC-DC")
    ArtistConsolidateStage().run(ctx)
    assert set(_artists(ctx)) == {"AC/DC"}


def test_credits_a_guest_credit_is_not_merged_into_the_lead_artist(tmp_path):
    ctx = _ctx(tmp_path)
    credits = ["50 Cent", "50 Cent, Nate Dogg", "Gorillaz", "Gorillaz feat. Bobby Womack"]
    _rows(ctx, *credits)
    ArtistConsolidateStage().run(ctx)
    assert _artists(ctx) == credits


def test_credits_real_spelling_variants_are_still_merged(tmp_path):
    ctx = _ctx(tmp_path)
    _rows(ctx, "ABBA", "Abba", "Simon & Garfunkel", "Simon and Garfunkel")
    ArtistConsolidateStage().run(ctx)
    assert _artists(ctx) == ["ABBA", "ABBA", "Simon & Garfunkel", "Simon & Garfunkel"]


def test_article_merging_band_and_the_band_settles_on_the_stored_form(tmp_path):
    ctx = _ctx(tmp_path)
    _rows(ctx, "The Band", "The Band", "Band")
    NormalizeStage().run(ctx)
    ArtistConsolidateStage().run(ctx)
    assert set(_artists(ctx)) == {"The Band"}
    again = [
        NormalizeStage().run(ctx).files_changed,
        ArtistConsolidateStage().run(ctx).files_changed,
    ]
    assert again == [0, 0], "the next run renamed the artist again"


def test_folders_a_master_moved_to_review_leaves_no_empty_folder(tmp_path):
    ctx = _ctx(tmp_path)
    old = ctx.config.alac_archive / "Rock" / "Dion" / "Old" / "Dion - Song.m4a"
    new = ctx.inbox / "Dion - Song.m4a"
    for path, bitrate in ((old, 900_000), (new, 1_600_000)):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"x" * (bitrate // 1000))
        upsert_archive(
            ctx.conn,
            {
                "file_path": str(path),
                "status": "CATALOGUED",
                "artist": "Dion",
                "album": "Old",
                "title": "Song",
                "audio_hash": "same",
                "codec": "alac",
                "bitrate": bitrate,
                "size_bytes": bitrate // 1000,
            },
        )
    ctx.conn.commit()
    DupeResolverStage().run(ctx)
    assert not old.exists(), "the lower-bitrate master was expected to go to review"
    empty = [p for p in ctx.config.alac_archive.rglob("*") if p.is_dir() and not any(p.iterdir())]
    assert empty == [], f"left behind: {empty}"
    assert ctx.config.alac_archive.is_dir(), "the tier itself must never be removed"
