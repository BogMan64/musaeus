"""The cloud review of #39 (2026-09-25), the findings still open.

#39 kept guest credits in the tag and left the FOLDER to folder_artist. That
is only right if folder_artist finds the lead artist of every credit the old
grouping key used to strip -- and it did not: a bare "feat" split "Little
Feat", and "duet with", "vs." and one-word-lead comma credits made folders of
their own. Findings 4 and 5 (copies of the stored-form rule) were fixed with
the #38 review, by normalize.stored_artist.

Not changed, with reasons: finding 6 ("a-ha" is stored "A-ha" -- Grey's
every-word rule, like "K.D. Lang") and finding 8 (losers outside the four
roots are set-aside rows, which duplicate handling now skips entirely).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from musaeus.artist_form import folder_artist
from musaeus.config import MusicConfig
from musaeus.context import RunContext
from musaeus.db import open_db, upsert_archive
from musaeus.stages.dupe_resolver import DupeResolverStage
from musaeus.stages.organize import library_relpath
from musaeus.stages.various_artists_fix import VariousArtistsFixStage


@pytest.mark.parametrize(
    ("credit", "lead"),
    [
        # 1: "Feat" is part of a band's name; only "feat." is a credit.
        ("Little Feat & Emmylou Harris", "Little Feat"),
        ("Little Feat with Bonnie Raitt", "Little Feat"),
        # 2: every form the old grouping key stripped.
        ("Elton John duet with Kiki Dee", "Elton John"),
        ("Run-DMC vs. Jason Nevins", "Run-DMC"),
        ("Run-DMC versus Jason Nevins", "Run-DMC"),
        ("Ringo Starr and Friends", "Ringo Starr"),
        ("B.B. King special guest Eric Clapton", "B.B. King"),
        ("Eminem, Dido", "Eminem"),
        ("Aerosmith, Yungblud, Lainey Wilson", "Aerosmith"),
        ("Gorillaz feat. Bobby Womack", "Gorillaz"),
    ],
)
def test_1_2_a_credit_files_under_its_lead_artist(credit, lead):
    assert folder_artist(credit) == lead


@pytest.mark.parametrize(
    "band",
    [
        "Little Feat",
        "10,000 Maniacs",
        "Earth, Wind & Fire",
        "Emerson, Lake & Palmer",
        "Crosby, Stills & Nash",
        "Peter, Paul and Mary",
        "Blood, Sweat & Tears",
        "Band, The",
    ],
)
def test_1_2_a_band_whose_name_holds_a_comma_or_feat_stays_whole(band):
    assert folder_artist(band) == band


def test_1_the_file_name_uses_the_lead_too():
    rel = library_relpath("Little Feat & Emmylou Harris", None, "Rock", "Alb", "Song", ".m4a")
    assert rel.parts[1] == "Little Feat" and rel.name == "Little Feat - Song.m4a"


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


def test_3_the_various_artists_genre_lookup_finds_an_artist_with_an_article(tmp_path):
    ctx = _ctx(tmp_path)
    p = ctx.inbox / "x.m4a"
    p.write_bytes(b"x")
    upsert_archive(
        ctx.conn,
        {"file_path": str(p), "status": "CATALOGUED", "artist": "The Revels", "genre": "Surf"},
    )
    ctx.conn.commit()
    assert VariousArtistsFixStage()._genre_from_library(ctx, "The Revels") == "Surf"


def test_7_a_tier_nested_inside_another_is_never_removed(tmp_path):
    lib = tmp_path / "Libraries" / "ALAC_Library"
    ctx = _ctx(tmp_path, archive=lib / "Archival")
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
    assert not old.exists()
    assert ctx.config.alac_archive.is_dir(), "the inner tier itself was removed"
