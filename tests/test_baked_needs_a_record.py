"""A copy is baked only if the catalogue says so; a copy with no album is not "the original".

Streamrip batches 1-6 (2026-10-29): the keep rule took any copy measuring
-18 LUFS (within 0.3) for an old baked copy. 21 originals lost that way to
44.1 kHz downloads, Grey's hand-kept 96 kHz "Suspicious Minds" among them.
Grey: "If it was baked already, there should be a ref in the metadata."

Two more lost because the download had no album yet at Act 2, so it read as
the original against Grey's copy on "Slugcology 101(remaster)" -- the same
remaster. Grey: "go with your suggestions": no album, no verdict on that step.
"""

from __future__ import annotations

import importlib.util
import sqlite3
from pathlib import Path

from musaeus.db import open_db, upsert_archive
from musaeus.keep_rule import decide
from musaeus.stages.dupe_resolver import _rank, _share_baked_mark

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "mark_baked_copies.py"


def _copy(**kw) -> dict:
    base = {"codec": "alac", "title": "Suspicious Minds", "album": "From Elvis In Memphis",
            "sample_rate": 44100, "duration": 263.0, "lufs": None}  # fmt: skip
    return {**base, **kw}


def test_a_master_measuring_minus_18_with_no_record_is_not_a_baked_copy():
    grey = _copy(sample_rate=96000, lufs=-17.73, kept_by_person_at="2026-10-21")
    download = _copy()

    assert decide(download, grey) == ("library", "quality")


def test_a_recorded_baked_copy_still_loses_to_the_original():
    baked = _copy(sample_rate=192000, lufs=-18.0, lufs_baked_at="2026-09-24 19:06:34")
    original = _copy()

    assert decide(original, baked) == ("review", "original/baked")


def test_a_copy_with_the_audio_of_a_recorded_baked_copy_is_baked_too():
    baked = _copy(sample_rate=192000, audio_hash="a", lufs=-18.0,
                  lufs_baked_at="2026-09-24 19:06:34", finalized_at="x")  # fmt: skip
    same_audio = _copy(sample_rate=192000, audio_hash="a")
    original = _copy(audio_hash="b")
    members = [baked, same_audio, original]

    _share_baked_mark(members)
    _rank(members)

    assert members[0] is original


def test_a_download_with_no_album_does_not_beat_a_remaster_as_the_original():
    grey = _copy(title="Don't Go Breaking My Heart", album="Rock Of The Westies (Remastered)",
                 sample_rate=192000)  # fmt: skip
    download = _copy(title="Don't Go Breaking My Heart", album=None)

    assert decide(download, grey) == ("library", "quality")


def test_the_resolver_ranks_the_same_way():
    grey = _copy(album="Slugcology 101(remaster)", sample_rate=192000, finalized_at="x")
    download = _copy(album="")
    members = [download, grey]

    _rank(members)

    assert members[0] is grey


def test_a_remaster_label_in_the_title_still_counts_without_an_album():
    original = _copy(album="From Elvis In Memphis")
    remaster = _copy(title="Suspicious Minds (2011 Remaster)", album=None, sample_rate=96000)

    assert decide(remaster, original) == ("library", "original/remaster")


def _script():
    spec = importlib.util.spec_from_file_location("mark_baked_copies", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _db(tmp_path: Path) -> sqlite3.Connection:
    conn = open_db(tmp_path / "musaeus.db")
    rows = (
        ("old_baked.m4a", -18.02, "2026-09-24 19:06:34"),
        ("old_loud.m4a", -9.5, "2026-09-24 19:06:34"),
        ("original.m4a", -17.73, "2026-09-26 16:43:52"),  # the originals' own run
    )
    for name, lufs, added in rows:
        upsert_archive(conn, {"file_path": str(tmp_path / name), "status": "CATALOGUED",
                              "artist": "A", "title": name})  # fmt: skip
        conn.execute(
            "UPDATE archive SET lufs = ?, date_added = ? WHERE file_path = ?",
            (lufs, added, str(tmp_path / name)),
        )
    conn.commit()
    conn.row_factory = sqlite3.Row
    return conn


def _marked(conn: sqlite3.Connection) -> list[str]:
    return [Path(r[0]).name for r in conn.execute(
        "SELECT file_path FROM archive WHERE lufs_baked_at IS NOT NULL ORDER BY id")]  # fmt: skip


def test_only_copies_catalogued_before_the_originals_came_in_are_marked(tmp_path):
    mod, conn = _script(), _db(tmp_path)

    mod.mark(conn, mod.candidates(conn), execute=True)

    assert _marked(conn) == ["old_baked.m4a"]
    ev = conn.execute("SELECT event_type, note FROM events WHERE stage = 'mark_baked'").fetchall()
    assert [e[0] for e in ev] == ["BAKED_COPY_MARKED"]
    assert mod.candidates(conn) == [], "a second run marks nothing again"


def test_a_dry_run_marks_nothing(tmp_path):
    mod, conn = _script(), _db(tmp_path)

    mod.mark(conn, mod.candidates(conn), execute=False)

    assert _marked(conn) == []
