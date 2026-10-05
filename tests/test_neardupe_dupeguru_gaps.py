"""Three gaps dupeGuru showed in the near-duplicate finder (Grey, 2026-10-05).

Grey ran dupeGuru over the masters. Of the 202 pairs it listed, our finder did not stage:
  * 91 pairs filed under two artist credits, mostly "Stevie Ray Vaughan" beside
    "Stevie Ray Vaughan & Double Trouble" (the artist buckets never compared them);
  * 19 live pairs, 10 of them the same performance, kept apart by the live guard because the
    titles differed only by "-" against "/" or the lengths by 1.3 s, and "Copacabana (At The
    Copa)" was taken for a live recording because of "at the".
"""

from __future__ import annotations

import pytest

from musaeus.config import MusicConfig
from musaeus.context import RunContext
from musaeus.db import open_db, upsert_archive
from musaeus.stages.neardupe import (
    NearDupeStage,
    _has_live_marker,
    _primary_artist_key,
    _same_take,
)


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


def _add(ctx, path, artist, title, duration):
    upsert_archive(ctx.conn, {"file_path": path, "status": "CATALOGUED", "artist": artist,
                              "title": title, "duration": duration, "bitrate": 320000,
                              "size_bytes": 5_000_000})  # fmt: skip
    ctx.conn.commit()


@pytest.mark.parametrize(
    ("credit", "want"),
    [
        ("Stevie Ray Vaughan & Double Trouble", "stevie ray vaughan"),
        ("Janis Joplin, Big Brother & The Holding Company", "janis joplin"),
        ("Duke Ellington & His Orchestra", "duke ellington"),
        ("The Bangles, Susanna Hoffs", "bangles"),
        ("Kenny Rogers feat. Dolly Parton", "kenny rogers"),
        ("Stevie Ray Vaughan", ""),  # one artist: no primary to join
        ("Hall And Oates", "hall"),  # offered only if a plain "Hall" bucket exists
    ],
)
def test_the_first_artist_of_a_credit(credit, want):
    assert _primary_artist_key(credit) == want


def test_a_credit_with_a_collaborator_is_compared_with_the_plain_name(ctx, tmp_path):
    _add(ctx, str(tmp_path / "a.m4a"), "Stevie Ray Vaughan", "Testify", 201.6)
    _add(ctx, str(tmp_path / "b.m4a"), "Stevie Ray Vaughan & Double Trouble", "Testify", 201.6)
    assert NearDupeStage().execute(ctx).files_changed == 1


def test_the_two_credits_must_also_be_the_same_length(ctx, tmp_path):
    # Kenny Rogers "Ruby" 179.2 s with The First Edition, 167.4 s alone: two recordings
    _add(ctx, str(tmp_path / "a.m4a"), "Kenny Rogers", "Ruby, Don't Take Your Love To Town", 167.4)
    _add(ctx, str(tmp_path / "b.m4a"), "Kenny Rogers, The First Edition",
         "Ruby, Don't Take Your Love To Town", 179.2)  # fmt: skip
    assert NearDupeStage().execute(ctx).files_changed == 0


def test_a_credit_with_no_plain_bucket_is_left_alone(ctx, tmp_path):
    _add(ctx, str(tmp_path / "a.m4a"), "Simon & Garfunkel", "The Boxer", 308.0)
    _add(ctx, str(tmp_path / "b.m4a"), "Paul Simon", "The Boxer", 308.0)
    assert NearDupeStage().execute(ctx).files_changed == 0


def test_live_copies_titled_alike_but_for_punctuation_are_one_performance():
    a = {
        "title": "Parisienne Walkways (Live At Royal Albert Hall, London - 1993)",
        "duration": 407.7,
    }
    b = {
        "title": "Parisienne Walkways (Live At Royal Albert Hall, London / 1993)",
        "duration": 407.7,
    }
    assert _same_take(a, b)


def test_live_copies_1_3_seconds_apart_are_one_performance_and_8_seconds_are_not():
    assert _same_take({"title": "Crossroads (Live)", "duration": 258.5},
                      {"title": "Crossroads (Live)", "duration": 259.8})  # fmt: skip
    assert not _same_take({"title": "Do You Feel Like We Do (Live)", "duration": 836.7},
                          {"title": "Do You Feel Like We Do (Live)", "duration": 826.8})  # fmt: skip


def test_at_the_copa_is_not_a_live_marker():
    assert not _has_live_marker("Copacabana (At The Copa) (Long Version)")
    assert _has_live_marker("Seventh Son (Live At Whisky A Go-Go)")
    assert _has_live_marker("Crossroads (Live)")
