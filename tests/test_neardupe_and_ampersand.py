"""Act 2 learns that "&" and "and" are one word, and that a song filed with
and without its guest named is one song at one length (Grey, 2026-10-12:
"Jack & Diane"/"Jack And Diane", "Smooth (feat. Rob Thomas)"/"Smooth" and
"The Girl Is Mine (With Paul McCartney)"/"The Girl Is Mine" are all the
same song)."""

from __future__ import annotations

import pytest

from musaeus.config import MusicConfig
from musaeus.context import RunContext
from musaeus.db import open_db, upsert_archive
from musaeus.stages.neardupe import NearDupeStage


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


def _pair(ctx, artist, a, b, length_a, length_b):
    for path, title, length in (("/m/a.m4a", a, length_a), ("/m/b.m4a", b, length_b)):
        upsert_archive(ctx.conn, {"file_path": path, "status": "CATALOGUED", "artist": artist,
                                  "title": title, "duration": length, "bitrate": 320000,
                                  "size_bytes": 5_000_000, "audio_hash": "pcm:" + path})  # fmt: skip
    ctx.conn.commit()
    return NearDupeStage().execute(ctx).files_changed


@pytest.mark.parametrize(
    ("artist", "a", "b"),
    [
        ("John Mellencamp", "Jack & Diane", "Jack And Diane"),
        ("Question Mark & The Mysterians", "Shout (Part 1 & 2)", "Shout (Part 1 And 2)"),
        ("José Feliciano", "Chico & The Man", "Chico And The Man"),
    ],
)
def test_ampersand_and_and_are_one_word(ctx, artist, a, b):
    assert _pair(ctx, artist, a, b, 200.0, 200.5) == 1


@pytest.mark.parametrize(
    ("artist", "a", "b"),
    [
        ("Santana", "Smooth (feat. Rob Thomas)", "Smooth"),
        ("Michael Jackson", "The Girl Is Mine (With Paul McCartney)", "The Girl Is Mine"),
    ],
)
def test_a_song_with_and_without_its_guest_named_is_one_song(ctx, artist, a, b):
    assert _pair(ctx, artist, a, b, 240.0, 241.0) == 1


# Grey, 2026-11-09: one song whatever the brackets or guests -- a re-recording,
# another guest, a live take or an alternate take is a duplicate, and the keep
# rule picks the copy. Each of these was kept apart before (pre-merge review
# of #191).


def test_a_re_recording_at_another_length_is_the_same_song(ctx):
    # The orchestral re-recording, 5.8 s apart.
    assert _pair(ctx, "Rod Stewart", "Stay With Me (With The Royal Philharmonic Orchestra)",
                 "Stay With Me", 280.0, 285.8) == 1  # fmt: skip


def test_two_different_guests_are_the_same_song(ctx):
    assert (
        _pair(ctx, "Shakira", "Loca (feat. Dizzee Rascal)", "Loca (feat. El Cata)", 190.0, 191.0)
        == 1
    )


def test_a_live_take_is_the_same_song_at_any_length(ctx):
    """Stray Cats "Rev It Up & Go (Live)" against the studio "Rev It Up And
    Go", 83 s apart."""
    assert _pair(ctx, "Stray Cats", "Rev It Up & Go (Live)", "Rev It Up And Go", 231.2, 148.3) == 1


def test_a_guest_bracket_is_the_same_song_at_any_length(ctx):
    assert _pair(ctx, "Billie Holiday", "The Blues Are Brewin' (feat. John Simmons)",
                 "The Blues Are Brewin'", 182.2, 214.7) == 1  # fmt: skip


def test_a_reprise_is_not_the_song(ctx):
    assert _pair(ctx, "The Beatles", "Sgt. Pepper's Lonely Hearts Club Band (Reprise)",
                 "Sgt. Pepper's Lonely Hearts Club Band", 78.0, 122.0) == 0  # fmt: skip


def test_two_parts_are_not_one_song(ctx):
    assert _pair(ctx, "The Police", "Synchronicity I", "Synchronicity II (Live)", 203.0, 300.0) == 0


def test_a_piece_from_a_named_work_is_not_every_piece_of_that_name(ctx):
    """The live catalogue, 2026-11-09: one "Main Theme" is from Thelma And
    Louise, the other is not."""
    assert _pair(ctx, "The City Of Prague Philharmonic Orchestra",
                 "Main Theme (From Thelma And Louise)", "Main Theme", 180.0, 202.9) == 0  # fmt: skip
