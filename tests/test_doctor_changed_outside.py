"""`musaeus doctor`: masters changed outside MUSAEUS (Grey, 2026-10-11).

About 1,840 masters had their tags changed on 2026-10-09/10 by a tag editor
with no MUSAEUS record behind it. Doctor now says so, read-only: each
master's file date against MUSAEUS's last recorded write, and the audio of a
sample re-hashed to tell "tags only" from "audio changed".
"""

from __future__ import annotations

import os
import shutil
import subprocess
import time
from pathlib import Path

import pytest

from musaeus import doctor
from musaeus.config import MusicConfig
from musaeus.db import ensure_columns, open_db, upsert_archive
from musaeus.hasher import audio_hash_safe

pytestmark = pytest.mark.skipif(not shutil.which("ffmpeg"), reason="requires ffmpeg")

DAY = 86400


@pytest.fixture
def cfg(tmp_path: Path) -> MusicConfig:
    c = MusicConfig(
        vault_root=tmp_path,
        inbox=tmp_path / "INBOX",
        staging=tmp_path / "STAGING",
        quarantine=tmp_path / "QUARANTINE",
        runs_root=tmp_path / "RUNS",
        meta_dir=tmp_path / "MetaData",
        alac_library=tmp_path / "Libraries" / "ALAC_Library",
        db_path=tmp_path / "musaeus.db",
    )
    c.ensure_dirs()
    return c


def _master(cfg, name: str, freq: int) -> Path:
    f = cfg.alac_archive / "Rock" / "A" / "B" / name
    f.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-f", "lavfi",
         "-i", f"sine=frequency={freq}:duration=1", "-c:a", "alac", str(f)],
        check=True, capture_output=True,
    )  # fmt: skip
    return f


def _catalogue(cfg, f: Path, written_days_ago: float) -> None:
    conn = open_db(cfg.db_path)
    stamp = time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(time.time() - written_days_ago * DAY))
    upsert_archive(
        conn, {"file_path": str(f), "status": "CATALOGUED", "audio_hash": audio_hash_safe(f)[0]}
    )
    conn.execute("UPDATE archive SET finalized_at = ? WHERE file_path = ?", (stamp, str(f)))
    conn.commit()
    conn.close()


def _finding(cfg):
    rep = doctor.Report()
    doctor._changed_outside_musaeus(cfg, rep)
    (f,) = [x for x in rep.findings if x.check == "changed outside MUSAEUS"]
    return f


def test_a_master_untouched_since_musaeus_wrote_it_is_fine(cfg):
    f = _master(cfg, "a.m4a", 440)
    _catalogue(cfg, f, written_days_ago=0)
    assert _finding(cfg).level == "ok"


def test_a_tag_change_outside_is_reported_as_tags_only(cfg):
    f = _master(cfg, "a.m4a", 440)
    _catalogue(cfg, f, written_days_ago=3)
    from mutagen.mp4 import MP4

    m = MP4(str(f))
    m["\xa9nam"] = ["Retitled by a tag editor"]
    m.save()  # the file is now newer than MUSAEUS's last write by days
    found = _finding(cfg)
    assert found.level == "warn" and found.count == 1
    assert "audio identical" in found.detail


def test_changed_audio_is_a_failure(cfg):
    f = _master(cfg, "a.m4a", 440)
    _catalogue(cfg, f, written_days_ago=3)
    other = _master(cfg, "b.m4a", 880)
    os.replace(other, f)  # different audio at the master's path
    os.utime(f, None)
    found = _finding(cfg)
    assert found.level == "fail"
    assert "AUDIO CHANGED" in found.detail


def test_doctor_stays_read_only(cfg):
    f = _master(cfg, "a.m4a", 440)
    _catalogue(cfg, f, written_days_ago=3)
    os.utime(f, None)
    before = (cfg.db_path.stat().st_mtime, f.stat().st_mtime, f.read_bytes())
    _finding(cfg)
    assert (cfg.db_path.stat().st_mtime, f.stat().st_mtime, f.read_bytes()) == before


def _event(cfg, etype, fp, ts_days_ago, new_value=None):
    conn = open_db(cfg.db_path)
    stamp = time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(time.time() - ts_days_ago * DAY))
    conn.execute(
        "INSERT INTO events (run_id, ts, event_type, file_path, new_value) VALUES ('r', ?, ?, ?, ?)",
        (stamp, etype, str(fp), new_value),
    )
    conn.commit()
    conn.close()


def test_a_write_then_a_move_is_musaeus_own(cfg):
    """Pre-merge review of #169: Tagger writes at A, Organize renames A to B
    (the date is kept): B read as changed outside."""
    f = _master(cfg, "a.m4a", 440)
    _catalogue(cfg, f, written_days_ago=3)
    os.utime(f, None)
    old = f.with_name("old name.m4a")
    _event(cfg, "TAGGER_WRITE", old, 0)
    _event(cfg, "ORGANIZE_RENAME", old, 0, new_value=str(f))
    assert _finding(cfg).level == "ok"


def test_a_database_only_event_does_not_hide_an_outside_edit(cfg):
    f = _master(cfg, "a.m4a", 440)
    _catalogue(cfg, f, written_days_ago=3)
    os.utime(f, None)
    _event(cfg, "MB_ARTIST_FOUND", f, 0)  # after the edit, but writes no file
    assert _finding(cfg).level == "warn"


def test_a_move_logged_under_its_destination_keeps_the_history(cfg):
    """VA fix, dupe moves and consolidate log (file_path=new, old_value=old)."""
    f = _master(cfg, "a.m4a", 440)
    _catalogue(cfg, f, written_days_ago=3)
    os.utime(f, None)
    old = f.with_name("old name.m4a")
    _event(cfg, "TAGGER_WRITE", old, 0)
    conn = open_db(cfg.db_path)
    conn.execute(
        "INSERT INTO events (run_id, event_type, file_path, old_value) "
        "VALUES ('r', 'VARIOUS_ARTISTS_FIXED', ?, ?)",
        (str(f), str(old)),
    )
    conn.commit()
    conn.close()
    assert _finding(cfg).level == "ok"


def test_a_bitrot_repair_is_musaeus_own(cfg):
    f = _master(cfg, "a.m4a", 440)
    _catalogue(cfg, f, written_days_ago=3)
    os.utime(f, None)
    _event(cfg, "BITROT_REPAIRED", f, 0)
    assert _finding(cfg).level == "ok"


# Grey's code review, 2026-10-11, finding 5.


@pytest.mark.parametrize("etype", ["ACOUSTIC_DUPE_FOUND", "DUPLICATE_FOUND"])
def test_a_duplicate_finding_does_not_hand_over_history(cfg, etype):
    """x and y are both still there: x's recent write says nothing of y."""
    y = _master(cfg, "y.m4a", 440)
    _catalogue(cfg, y, written_days_ago=3)
    os.utime(y, None)  # y edited outside, today
    x = y.with_name("x.m4a")
    _event(cfg, "TAGGER_WRITE", x, 0)
    _event(cfg, etype, x, 0, new_value=str(y))
    assert _finding(cfg).level == "warn"


def test_a_duplicate_found_in_its_live_shape_hands_over_nothing(cfg):
    """DUPLICATE_FOUND as it is logged: the library master in old_value, the
    arrival in file_path. Both files remain."""
    y = _master(cfg, "y.m4a", 440)
    _catalogue(cfg, y, written_days_ago=3)
    os.utime(y, None)  # y edited outside, today
    x = y.with_name("x.m4a")
    _event(cfg, "TAGGER_WRITE", x, 0)
    conn = open_db(cfg.db_path)
    conn.execute(
        "INSERT INTO events (run_id, event_type, file_path, old_value) "
        "VALUES ('r', 'DUPLICATE_FOUND', ?, ?)",
        (str(y), str(x)),
    )
    conn.commit()
    conn.close()
    assert _finding(cfg).level == "warn"


def test_a_look_is_not_a_write(cfg):
    """art_checked_at says MUSAEUS looked at the file, not that it wrote it."""
    f = _master(cfg, "a.m4a", 440)
    _catalogue(cfg, f, written_days_ago=3)
    os.utime(f, None)
    conn = open_db(cfg.db_path)
    ensure_columns(conn, (("art_checked_at", "TEXT"), ("title_checked_at", "TEXT")))
    conn.execute(
        "UPDATE archive SET art_checked_at = datetime('now'), title_checked_at = datetime('now')"
    )
    conn.commit()
    conn.close()
    assert _finding(cfg).level == "warn"


def test_a_master_filed_under_its_composer_is_followed(cfg):
    """ClassicalComposer moves masters within ALAC-Archival; its artist event
    carries names, so the move needs its own path-to-path event."""
    from musaeus.context import RunContext
    from musaeus.stages.classical_composer import ClassicalComposerStage

    f = _master(cfg, "Air.m4a", 440)
    _catalogue(cfg, f, written_days_ago=3)
    _event(cfg, "TAGGER_WRITE", f, 0)  # MUSAEUS wrote it today, then refiled it
    conn = open_db(cfg.db_path)
    conn.execute("UPDATE archive SET artist = 'Some Orchestra', title = 'Air, BWV 1068', "
                 "album = 'B', genre = 'Classical'")  # fmt: skip
    conn.commit()
    ctx = RunContext.new(cfg, conn, dry_run=False)
    ClassicalComposerStage().execute(ctx)
    (new,) = conn.execute("SELECT file_path FROM archive").fetchone()
    conn.close()
    assert new != str(f) and Path(new).exists(), "the stage did not refile the master"
    os.utime(new, None)
    assert _finding(cfg).level == "ok"
