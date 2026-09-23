"""A deletion removes the track it was asked to remove, and nothing else.

Grey's rule is "delete means delete, not quarantine" and "delete all copies",
so this tool deletes across all three tiers and writes the audio hash to the
deny list -- a deletion that does not reach denied_hashes is not permanent,
because the next ingest of the same audio walks straight back in.

THE BUG THIS FILE EXISTS FOR

Two archive rows can share ONE published file. A track and its "My playlist
X" duplicate carry the same car_export_path, because the car edition is keyed
on (artist, title) and they are the same recording. Deleting the duplicate
then removed the file the KEPT row named, and the kept row silently became a
phantom -- the catalogue pointing at nothing.

Measured live on 2026-09-16: of 82 deletions, 2 did exactly that, to Paul
McCartney's "With a Little Luck" and Rage Against the Machine's "Killing in
the Name". Both were recoverable only because the masters survived.
"""

from __future__ import annotations

import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "delete_reviewed_tracks.py"


@pytest.fixture
def vault(tmp_path, monkeypatch):
    libs = tmp_path / "Libraries"
    for tier in ("ALAC-Archival", "ALAC_Library", "CAR_Library"):
        (libs / tier).mkdir(parents=True)
    (tmp_path / "_db_backups").mkdir()
    (tmp_path / "MetaData").mkdir()
    db = tmp_path / "musaeus.db"
    conn = sqlite3.connect(db)
    conn.execute(
        "CREATE TABLE archive (id INTEGER PRIMARY KEY, artist TEXT, title TEXT, "
        "status TEXT, audio_hash TEXT, file_path TEXT, car_export_path TEXT)"
    )
    conn.execute(
        "CREATE TABLE events (id INTEGER PRIMARY KEY, run_id TEXT NOT NULL, ts TEXT, "
        "event_type TEXT, file_path TEXT, old_value TEXT, new_value TEXT, note TEXT)"
    )
    conn.commit()
    conn.close()
    led = tmp_path / "_db_backups" / "hash_index.db"
    lc = sqlite3.connect(led)
    lc.execute(
        "CREATE TABLE denied_hashes (audio_hash TEXT PRIMARY KEY, reason TEXT, "
        "source_path TEXT, denied_at TEXT DEFAULT CURRENT_TIMESTAMP)"
    )
    lc.commit()
    lc.close()
    monkeypatch.setenv("MUSAEUS_VAULT_ROOT", str(tmp_path))
    monkeypatch.setenv("MUSAEUS_DB", str(db))
    return tmp_path


def _add(vault, rid, artist, title, rel, car=None, h=None):
    libs = vault / "Libraries"
    for tier in ("ALAC-Archival", "ALAC_Library"):
        p = libs / tier / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(b"\0")
    if car:
        c = libs / "CAR_Library" / car
        c.parent.mkdir(parents=True, exist_ok=True)
        c.write_bytes(b"\0")
    conn = sqlite3.connect(vault / "musaeus.db")
    conn.execute(
        "INSERT INTO archive (id, artist, title, status, audio_hash, file_path, car_export_path) "
        "VALUES (?,?,?,'CATALOGUED',?,?,?)",
        (
            rid,
            artist,
            title,
            h or f"h{rid}",
            str(libs / "ALAC_Library" / rel),
            str(libs / "CAR_Library" / car) if car else None,
        ),
    )
    conn.commit()
    conn.close()


def _run(vault, ids, execute=True):
    f = vault / "ids.txt"
    f.write_text("\n".join(str(i) for i in ids))
    cmd = [sys.executable, str(SCRIPT), str(f), "--reason", "test"]
    if execute:
        cmd.append("--execute")
    return subprocess.run(
        cmd, capture_output=True, text=True, timeout=120, cwd=str(SCRIPT.parents[1])
    )


class TestItDeletesEveryCopy:
    def test_all_three_tiers_go(self, vault):
        _add(vault, 1, "Blur", "Song 2", "Blur/Parklife/x.m4a", car="Blur/Parklife/x.m4a")
        r = _run(vault, [1])
        assert r.returncode == 0, r.stderr
        libs = vault / "Libraries"
        for tier in ("ALAC-Archival", "ALAC_Library", "CAR_Library"):
            assert not (libs / tier / "Blur" / "Parklife" / "x.m4a").exists(), tier

    def test_the_row_goes_and_the_hash_is_denied(self, vault):
        _add(vault, 1, "Blur", "Song 2", "Blur/Parklife/x.m4a")
        _run(vault, [1])
        conn = sqlite3.connect(vault / "musaeus.db")
        assert conn.execute("SELECT COUNT(*) FROM archive WHERE id=1").fetchone()[0] == 0
        conn.close()
        lc = sqlite3.connect(vault / "_db_backups" / "hash_index.db")
        assert (
            lc.execute("SELECT COUNT(*) FROM denied_hashes WHERE audio_hash='h1'").fetchone()[0]
            == 1
        )
        lc.close()

    def test_a_dry_run_changes_nothing(self, vault):
        _add(vault, 1, "Blur", "Song 2", "Blur/Parklife/x.m4a")
        _run(vault, [1], execute=False)
        assert (vault / "Libraries" / "ALAC_Library" / "Blur" / "Parklife" / "x.m4a").exists()
        conn = sqlite3.connect(vault / "musaeus.db")
        assert conn.execute("SELECT COUNT(*) FROM archive WHERE id=1").fetchone()[0] == 1
        conn.close()


class TestItDoesNotTakeASurvivingRowsFile:
    """The 2026-09-16 defect, stated as a test."""

    def test_a_car_file_two_rows_share_is_kept(self, vault):
        shared = "Paul McCartney/Wings Greatest/luck.m4a"
        _add(
            vault,
            1,
            "Paul McCartney",
            "With a Little Luck",
            "Paul McCartney/Wings Greatest/luck.m4a",
            car=shared,
        )
        _add(
            vault,
            2,
            "Paul McCartney",
            "With A Little Luck",
            "Paul McCartney/My playlist W/luck.m4a",
            car=shared,
        )
        r = _run(vault, [2])  # delete only the playlist duplicate
        assert r.returncode == 0, r.stderr
        kept = vault / "Libraries" / "CAR_Library" / shared
        assert kept.is_file(), "the surviving row's car file was deleted"
        assert "KEPT" in r.stdout

    def test_the_surviving_row_is_not_left_a_phantom(self, vault):
        shared = "A/Al/x.m4a"
        _add(vault, 1, "A", "X", "A/Al/x.m4a", car=shared)
        _add(vault, 2, "A", "X dup", "A/Playlist/x.m4a", car=shared)
        _run(vault, [2])
        conn = sqlite3.connect(vault / "musaeus.db")
        p = conn.execute("SELECT car_export_path FROM archive WHERE id=1").fetchone()[0]
        conn.close()
        assert Path(p).is_file(), "row 1 now points at a file that is gone"

    def test_the_doomed_rows_own_unshared_files_still_go(self, vault):
        """The guard must not become an excuse to delete nothing."""
        _add(vault, 1, "A", "X", "A/Al/keep.m4a", car="A/Al/keep.m4a")
        _add(vault, 2, "B", "Y", "B/Bl/gone.m4a", car="B/Bl/gone.m4a")
        _run(vault, [2])
        libs = vault / "Libraries"
        assert not (libs / "CAR_Library" / "B" / "Bl" / "gone.m4a").exists()
        assert not (libs / "ALAC-Archival" / "B" / "Bl" / "gone.m4a").exists()
        assert (libs / "CAR_Library" / "A" / "Al" / "keep.m4a").is_file()


def _add_row_only(vault, rid, artist, title, file_path, h):
    """A row whose files are already gone: deleted by hand, outside MUSAEUS."""
    conn = sqlite3.connect(vault / "musaeus.db")
    conn.execute(
        "INSERT INTO archive (id, artist, title, status, audio_hash, file_path) "
        "VALUES (?,?,?,'DUPE_REVIEW',?,?)",
        (rid, artist, title, h, str(file_path)),
    )
    conn.commit()
    conn.close()


def _denied(vault, h):
    lc = sqlite3.connect(vault / "_db_backups" / "hash_index.db")
    n = lc.execute("SELECT COUNT(*) FROM denied_hashes WHERE audio_hash=?", (h,)).fetchone()[0]
    lc.close()
    return n == 1


class TestItDoesNotDenyASurvivingRowsAudio:
    """The 2026-09-23 defect, stated as a test.

    An exact duplicate shares its audio fingerprint with the copy that was
    kept. Denying the duplicate's fingerprint denies the kept track too: it is
    refused on the next rebuild. The file guard above stops a deletion taking
    a surviving row's FILE; this stops it taking a surviving row's AUDIO. Found
    while clearing a hand-deleted review queue: 252 of its 1,136 rows shared
    audio with tracks still in the library.
    """

    def test_audio_a_surviving_row_carries_is_not_denied(self, vault):
        _add(vault, 1, "ABC", "Poison Arrow", "ABC/Lexicon/x.m4a", h="shared")
        _add_row_only(vault, 2, "ABC", "Poison Arrow", vault / "REVIEW/gone.m4a", h="shared")
        r = _run(vault, [2])
        assert r.returncode == 0, r.stderr
        conn = sqlite3.connect(vault / "musaeus.db")
        assert conn.execute("SELECT COUNT(*) FROM archive WHERE id=2").fetchone()[0] == 0
        conn.close()
        assert not _denied(vault, "shared"), "the kept track's audio was denied"
        assert "NOT DENIED" in r.stdout

    def test_audio_only_doomed_rows_carry_is_still_denied(self, vault):
        """The guard must not become an excuse to deny nothing."""
        _add_row_only(vault, 1, "A", "X", vault / "REVIEW/a.m4a", h="solo")
        _add_row_only(vault, 2, "A", "X", vault / "REVIEW/b.m4a", h="solo")
        _run(vault, [1, 2])
        assert _denied(vault, "solo"), "every copy was deleted, so the audio must be denied"


class TestARowWithNoFileIsStillRecorded:
    """Events were written per deleted FILE. A row whose files were already
    gone left no event at all -- and once its audio is (rightly) not denied,
    no ledger entry either. It would vanish from every record."""

    def test_the_removal_leaves_an_event(self, vault):
        gone = vault / "REVIEW" / "gone.m4a"
        _add_row_only(vault, 1, "A", "X", gone, h="h1")
        _run(vault, [1])
        conn = sqlite3.connect(vault / "musaeus.db")
        n = conn.execute(
            "SELECT COUNT(*) FROM events WHERE event_type='DELETED_BY_REVIEW' AND file_path=?",
            (str(gone),),
        ).fetchone()[0]
        conn.close()
        assert n == 1, "a row was removed from the catalogue with nothing recorded"
