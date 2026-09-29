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


class TestItTidiesUpAfterItself:
    """224 removals on 2026-09-23 left ~3,600 empty folders across the tiers."""

    def test_the_folders_a_deletion_empties_go_and_the_tier_roots_stay(self, vault):
        _add(vault, 1, "Blur", "Song 2", "Rock/Blur/Parklife/x.m4a", car="Blur/Parklife/x.m4a")
        r = _run(vault, [1])
        assert r.returncode == 0, r.stderr
        libs = vault / "Libraries"
        for tier, gone in (
            ("ALAC_Library", "Rock"),
            ("ALAC-Archival", "Rock"),
            ("CAR_Library", "Blur"),
        ):
            assert not (libs / tier / gone).exists(), f"{tier}/{gone} left behind empty"
            assert (libs / tier).is_dir(), f"the {tier} root itself must stay"

    def test_a_folder_still_holding_anything_stays(self, vault):
        _add(vault, 1, "Blur", "Song 2", "Rock/Blur/Parklife/x.m4a")
        _add(vault, 2, "Blur", "Girls & Boys", "Rock/Blur/Parklife/y.m4a")
        art = vault / "Libraries" / "ALAC-Archival" / "Rock" / "Blur" / "Parklife" / "cover.jpg"
        art.write_bytes(b"jpg")
        _run(vault, [1])
        assert (
            vault / "Libraries" / "ALAC_Library" / "Rock" / "Blur" / "Parklife" / "y.m4a"
        ).exists()
        assert art.exists(), "a file the catalogue does not know keeps its folder"

    def test_the_car_copy_is_found_from_the_catalogue_not_a_mirrored_guess(self, vault):
        _add(vault, 1, "Blur", "Song 2", "Rock/Blur/Parklife/x.m4a")  # no car_export_path
        stray = vault / "Libraries" / "CAR_Library" / "Rock" / "Blur" / "Parklife" / "x.m4a"
        stray.parent.mkdir(parents=True)
        stray.write_bytes(b"\0")
        _run(vault, [1])
        assert stray.exists(), "the car tier has no genre level; a mirrored path is never its copy"


class TestTheLosslessEditionCopy:
    """Grey, 2026-09-27: deleting a track removes its -18 LUFS edition copy
    at once ("delete all copies"), not at the next edition build. Since
    2026-09-25 the row points at the MASTER in ALAC-Archival, and the copy
    in ALAC_Library is known only to the edition ledger, by audio hash."""

    REL = "Rock/Blur/Parklife/Blur - Song 2.m4a"

    def _track(self, vault, rid, h):
        libs = vault / "Libraries"
        master = libs / "ALAC-Archival" / self.REL
        master.parent.mkdir(parents=True, exist_ok=True)
        master.write_bytes(b"\0")
        conn = sqlite3.connect(vault / "musaeus.db")
        conn.execute(
            "INSERT INTO archive (id, artist, title, status, audio_hash, file_path) "
            "VALUES (?, 'Blur', 'Song 2', 'CATALOGUED', ?, ?)",
            (rid, h, str(master) if rid == 1 else str(master) + f".{rid}"),
        )
        conn.commit()
        conn.close()

    def _copy(self, vault, h):
        from musaeus.edition_ledger import Copy, open_ledger, record

        copy = _marked(vault / "Libraries" / "ALAC_Library" / self.REL, "lossless", h)
        conn = open_ledger(vault / "_db_backups" / "editions.db")
        record(conn, Copy("lossless", h, "m", 1, str(copy), "now", -18.0, "linear"))
        conn.close()
        return copy

    def _recorded(self, vault) -> int:
        conn = sqlite3.connect(vault / "_db_backups" / "editions.db")
        n = conn.execute("SELECT COUNT(*) FROM edition_copies").fetchone()[0]
        conn.close()
        return n

    def test_the_edition_copy_goes_with_the_master(self, vault):
        self._track(vault, 1, "hb")
        copy = self._copy(vault, "hb")
        r = _run(vault, [1])
        assert r.returncode == 0, r.stderr
        assert not (vault / "Libraries" / "ALAC-Archival" / self.REL).exists()
        assert not copy.exists(), "the -18 LUFS copy outlived its deleted master"
        assert self._recorded(vault) == 0

    def test_a_dry_run_leaves_the_copy(self, vault):
        self._track(vault, 1, "hb")
        copy = self._copy(vault, "hb")
        _run(vault, [1], execute=False)
        assert copy.exists() and self._recorded(vault) == 1

    def test_the_copy_stays_while_a_surviving_track_has_the_same_audio(self, vault):
        self._track(vault, 1, "hb")
        self._track(vault, 2, "hb")  # the same recording, kept
        copy = self._copy(vault, "hb")
        _run(vault, [1])
        assert copy.exists() and self._recorded(vault) == 1


def test_every_editions_copy_goes_with_the_master(vault):
    # Grey: "delete all copies" -- the car and iPhone copies too (2026-09-28).
    from musaeus.edition_ledger import Copy, open_ledger, record

    rel = "Rock/Blur/Parklife/Blur - Song 2.m4a"
    libs = vault / "Libraries"
    master = libs / "ALAC-Archival" / rel
    master.parent.mkdir(parents=True, exist_ok=True)
    master.write_bytes(b"\0")
    conn = sqlite3.connect(vault / "musaeus.db")
    conn.execute(
        "INSERT INTO archive (id, artist, title, status, audio_hash, file_path) "
        "VALUES (1, 'Blur', 'Song 2', 'CATALOGUED', 'hb', ?)",
        (str(master),),
    )
    conn.commit()
    conn.close()
    led = open_ledger(vault / "_db_backups" / "editions.db")
    made = []
    for edition, tier in (("car", "CAR_Library"), ("iphone", "iPHONE_Library")):
        p = _marked(libs / tier / "Blur" / "Parklife" / "Blur - Song 2.m4a", edition, "hb")
        record(led, Copy(edition, "hb", str(master), 1, str(p), "now", -14.0, "linear"))
        made.append(p)
    led.close()
    r = _run(vault, [1])
    assert r.returncode == 0, r.stderr
    assert not any(p.exists() for p in made), "a car or iPhone copy outlived its master"
    led = sqlite3.connect(vault / "_db_backups" / "editions.db")
    assert led.execute("SELECT COUNT(*) FROM edition_copies").fetchone()[0] == 0
    led.close()


def _marked(path: Path, edition: str, h: str) -> Path:
    """A real (tiny) m4a carrying the edition marker of master *h*."""
    from mutagen.mp4 import MP4, MP4FreeForm

    from musaeus.edition_bake import MARKER_KEY
    from musaeus.edition_build import KINDS, marker_for

    path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi",
         "-i", "anullsrc=r=44100:cl=stereo", "-t", "0.2", "-c:a", "aac", str(path)],
        check=True, stdin=subprocess.DEVNULL,
    )  # fmt: skip
    f = MP4(path)
    if f.tags is None:
        f.add_tags()
    f.tags[MARKER_KEY] = [MP4FreeForm(marker_for(h, KINDS[edition]).encode())]
    f.save()
    return path


def test_a_file_at_a_copys_place_that_is_not_the_copy_is_left(vault):
    # Second review of #49: the tool deleted whatever sat at the recorded
    # path. The build never deletes a file whose marker is not the copy's.
    from musaeus.edition_ledger import Copy, open_ledger, record

    rel = "Rock/Blur/Parklife/Blur - Song 2.m4a"
    libs = vault / "Libraries"
    master = libs / "ALAC-Archival" / rel
    master.parent.mkdir(parents=True, exist_ok=True)
    master.write_bytes(b"\0")
    conn = sqlite3.connect(vault / "musaeus.db")
    conn.execute(
        "INSERT INTO archive (id, artist, title, status, audio_hash, file_path) "
        "VALUES (1, 'Blur', 'Song 2', 'CATALOGUED', 'hb', ?)",
        (str(master),),
    )
    conn.commit()
    conn.close()
    stranger = _marked(
        libs / "CAR_Library" / "Blur" / "Parklife" / "Blur - Song 2.m4a", "car", "hx"
    )
    led = open_ledger(vault / "_db_backups" / "editions.db")
    record(led, Copy("car", "hb", str(master), 1, str(stranger), "now", -14.0, "linear"))
    led.close()
    r = _run(vault, [1])
    assert r.returncode == 0, r.stderr
    assert stranger.exists(), "another master's copy was deleted"
    led = sqlite3.connect(vault / "_db_backups" / "editions.db")
    assert led.execute("SELECT COUNT(*) FROM edition_copies").fetchone()[0] == 0, (
        "stale record kept"
    )
    led.close()


def test_deleting_the_last_iphone_copy_keeps_the_iphone_folder(vault):
    # Cloud review of #53 (extra list): the empty-folder sweep stopped at the
    # tiers it knew, and iPHONE_Library was not one -- the edition's own
    # folder went with its last copy.
    from musaeus.edition_ledger import Copy, open_ledger, record

    rel = "Rock/Blur/Parklife/Blur - Song 2.m4a"
    libs = vault / "Libraries"
    master = libs / "ALAC-Archival" / rel
    master.parent.mkdir(parents=True, exist_ok=True)
    master.write_bytes(b"\0")
    conn = sqlite3.connect(vault / "musaeus.db")
    conn.execute(
        "INSERT INTO archive (id, artist, title, status, audio_hash, file_path) "
        "VALUES (1, 'Blur', 'Song 2', 'CATALOGUED', 'hb', ?)",
        (str(master),),
    )
    conn.commit()
    conn.close()
    copy = _marked(
        libs / "iPHONE_Library" / "Blur" / "Parklife" / "Blur - Song 2.m4a", "iphone", "hb"
    )
    led = open_ledger(vault / "_db_backups" / "editions.db")
    record(led, Copy("iphone", "hb", str(master), 1, str(copy), "now", -14.0, "linear"))
    led.close()
    r = _run(vault, [1])
    assert r.returncode == 0, r.stderr
    assert not copy.exists()
    assert (libs / "iPHONE_Library").is_dir(), "the iPhone edition's folder was removed"
