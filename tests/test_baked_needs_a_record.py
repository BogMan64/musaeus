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

import pytest

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


def test_a_song_named_original_is_not_an_original_label():
    # CodeRabbit, review of #262: "Original Sin" is the song's name.
    download = _copy(title="Original Sin - Mono", album=None)
    grey = _copy(title="Original Sin", album="The Swing (Remastered)", sample_rate=96000)

    assert decide(download, grey) == ("library", "quality")


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


def test_an_original_mix_label_is_judged_without_an_album():
    # CodeRabbit, review of #262: "(Original Mix)" is a label, not an unknown.
    original = _copy(title="Suspicious Minds (Original Mix)", album=None)
    remaster = _copy(album="From Elvis In Memphis (Remastered)", sample_rate=96000)

    assert decide(original, remaster) == ("review", "original/remaster")


def _main(mod, monkeypatch, tmp_path, *argv):
    from types import SimpleNamespace

    monkeypatch.setattr(
        mod.MusicConfig, "from_env", lambda: SimpleNamespace(db_path=tmp_path / "musaeus.db")
    )
    monkeypatch.setattr("sys.argv", ["mark_baked_copies.py", *argv])
    return mod.main()


def test_execute_needs_the_reviewed_ids(tmp_path, monkeypatch):
    mod, conn = _script(), _db(tmp_path)

    with pytest.raises(SystemExit):
        _main(mod, monkeypatch, tmp_path, "--execute")

    assert _marked(conn) == []


def _first(conn) -> int:
    return conn.execute("SELECT id FROM archive WHERE file_path LIKE '%old_baked.m4a'").fetchone()[
        0
    ]


def test_execute_marks_only_the_reviewed_rows(tmp_path, monkeypatch):
    mod, conn = _script(), _db(tmp_path)
    upsert_archive(conn, {"file_path": str(tmp_path / "old_baked_2.m4a"), "status": "CATALOGUED"})
    conn.execute("UPDATE archive SET lufs = -18.0, date_added = '2026-09-25 12:00:00' "
                 "WHERE file_path = ?", (str(tmp_path / "old_baked_2.m4a"),))  # fmt: skip
    conn.commit()
    _main(mod, monkeypatch, tmp_path, "--write-ids", str(tmp_path / "all.tsv"))
    listed = (tmp_path / "all.tsv").read_text().splitlines()
    assert len(listed) == 2, "the dry run lists every candidate"
    keep = next(x for x in listed if x.startswith(f"{_first(conn)}\t"))
    (tmp_path / "reviewed.tsv").write_text(keep + "\n")

    _main(mod, monkeypatch, tmp_path, "--execute", "--ids", str(tmp_path / "reviewed.tsv"))

    assert _marked(conn) == ["old_baked.m4a"]


def test_an_id_whose_row_now_has_another_path_is_not_marked(tmp_path, monkeypatch):
    # CodeRabbit, review of #262: an id from another or rebuilt catalogue.
    mod, conn = _script(), _db(tmp_path)
    (tmp_path / "reviewed.tsv").write_text(f"{_first(conn)}\t/elsewhere/old_baked.m4a\n")

    _main(mod, monkeypatch, tmp_path, "--execute", "--ids", str(tmp_path / "reviewed.tsv"))

    assert _marked(conn) == []


def test_the_candidate_list_cannot_be_written_and_used_in_one_run(tmp_path, monkeypatch):
    # CodeRabbit, review of #262: --write-ids X --ids X would mark every candidate.
    mod, conn = _script(), _db(tmp_path)
    ids = str(tmp_path / "x.tsv")

    with pytest.raises(SystemExit):
        _main(mod, monkeypatch, tmp_path, "--execute", "--write-ids", ids, "--ids", ids)

    assert _marked(conn) == []


def test_only_library_and_review_copies_are_candidates(tmp_path):
    # CodeRabbit, review of #262: a GHOST row has no file to be a copy of anything.
    mod, conn = _script(), _db(tmp_path)
    conn.execute("UPDATE archive SET status = 'GHOST' WHERE file_path LIKE '%old_baked.m4a'")
    conn.commit()

    assert mod.candidates(conn) == []


def test_the_dry_run_works_on_a_catalogue_without_the_bake_columns(tmp_path, monkeypatch):
    # CodeRabbit, review of #262: open_db adds the columns before they are read.
    # Rebuilt without the two columns: DROP COLUMN needs SQLite 3.35 (CodeRabbit).
    _db(tmp_path).close()
    db = sqlite3.connect(tmp_path / "musaeus.db")
    cols = [r[1] for r in db.execute("PRAGMA table_info(archive)")
            if r[1] not in ("lufs_baked_at", "lufs_baked_target")]  # fmt: skip
    db.execute(f"CREATE TABLE old_archive AS SELECT {', '.join(cols)} FROM archive")
    db.execute("DROP TABLE archive")
    db.execute("ALTER TABLE old_archive RENAME TO archive")
    db.commit()
    assert "lufs_baked_at" not in {r[1] for r in db.execute("PRAGMA table_info(archive)")}
    db.close()

    assert _main(_script(), monkeypatch, tmp_path) == 0


def test_a_copy_with_no_album_does_not_lift_a_remix_over_the_original():
    # Streamrip batch 7: a third copy with no album switched the step off for the
    # whole group, and "Rhinestone Cowboy (Remix)" beat the original on sample rate.
    original = _copy(title="Rhinestone Cowboy", album="Rhinestone Cowboy", id=80544,
                     finalized_at="x")  # fmt: skip
    remix = _copy(title="Rhinestone Cowboy (Remix)", album="Rhinestone Cowboy",
                  sample_rate=192000, id=23207, finalized_at="x")  # fmt: skip
    unknown = _copy(title="Rhinestone Cowboy", album=None, id=90001)
    members = [remix, unknown, original]

    _rank(members)

    assert members[0] is original
    assert members[-1] is remix
