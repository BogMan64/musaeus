"""A title with the artist's own name stuck on the front is a near duplicate of the clean one.

Grey, 2026-10-05, reviewing the wanted list: "Toto Africa" (artist Toto), "Moody Blues Nights In
White Satin", "Kinks Lola", "Stevie Nicks Stop Draggin' My Heart" -- and the library holds 64
such titles, 14 with the clean-titled song beside them. The fuzzy score of "toto africa" against
"africa" is ~70, far below the threshold, so the pair was never staged for review.
"""

from __future__ import annotations

import pytest

from musaeus.config import MusicConfig
from musaeus.context import RunContext
from musaeus.db import open_db, upsert_archive
from musaeus.stages.neardupe import NearDupeStage, _without_artist_prefix


@pytest.fixture
def ctx(tmp_path):
    meta = tmp_path / "MetaData"
    meta.mkdir()
    (meta / "artist_canon.tsv").write_text("", encoding="utf-8")
    cfg = MusicConfig(
        vault_root=tmp_path, inbox=tmp_path / "INBOX", staging=tmp_path / "STAGING",
        quarantine=tmp_path / "Q", runs_root=tmp_path / "RUNS", meta_dir=meta,
        alac_library=tmp_path / "ALAC-Library", db_path=tmp_path / "musaeus.db",
    )  # fmt: skip
    return RunContext.new(cfg, open_db(cfg.db_path), dry_run=True)


def _add(ctx, path, artist, title):
    upsert_archive(ctx.conn, {"file_path": path, "status": "CATALOGUED", "artist": artist,
                              "title": title, "bitrate": 320000, "size_bytes": 5_000_000})  # fmt: skip
    ctx.conn.commit()


@pytest.mark.parametrize(
    ("artist", "title", "want"),
    [
        ("Toto", "Toto Africa", "africa"),
        ("The Kinks", "Kinks Lola, The", "lola the"),
        ("Stevie Nicks", "Stevie Nicks Stop Draggin' My Heart", "stop draggin my heart"),
        ("The Moody Blues", "Moody Blues Nights In White Satin", "nights in white satin"),
        ("Belinda Carlisle", "Belinda Carlisle - I Feel Free", "i feel free"),
        ("Toto", "Africa", "africa"),
        ("Toto", "Toto", "toto"),  # nothing left once the name is removed: left whole
        ("Heart", "Heart Of Glass", "of glass"),  # only ever used as ONE more way to compare
    ],
)
def test_the_artist_prefix_is_removed_for_comparison(artist, title, want):
    from musaeus.stages.neardupe import _normalise

    assert _without_artist_prefix(_normalise(title, True), _normalise(artist)) == want


@pytest.mark.parametrize(
    ("artist", "dirty", "clean"),
    [
        ("Toto", "Toto Africa", "Africa"),
        ("The Kinks", "Kinks Lola", "Lola"),
        ("Belinda Carlisle", "Belinda Carlisle I Feel Free", "I Feel Free"),
    ],
)
def test_a_prefixed_title_and_its_clean_twin_are_staged_as_near_duplicates(
    ctx, tmp_path, artist, dirty, clean
):
    _add(ctx, str(tmp_path / "a.flac"), artist, dirty)
    _add(ctx, str(tmp_path / "b.flac"), artist, clean)
    assert NearDupeStage().execute(ctx).files_changed >= 1


def test_two_different_songs_by_one_artist_are_still_different(ctx, tmp_path):
    _add(ctx, str(tmp_path / "a.flac"), "Toto", "Toto Africa")
    _add(ctx, str(tmp_path / "b.flac"), "Toto", "Rosanna")
    assert NearDupeStage().execute(ctx).files_changed == 0


def test_a_title_that_merely_starts_with_a_band_word_is_not_a_duplicate_of_something_else(
    ctx, tmp_path
):
    _add(ctx, str(tmp_path / "a.flac"), "Heart", "Heart Of Glass")
    _add(ctx, str(tmp_path / "b.flac"), "Heart", "Alone")
    assert NearDupeStage().execute(ctx).files_changed == 0
