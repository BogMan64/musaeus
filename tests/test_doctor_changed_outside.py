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
from musaeus.db import open_db, upsert_archive
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


def test_a_master_an_edition_build_read_since_is_fine(cfg):
    """MUSAEUS's own loudness-tag script logged nothing on 2026-10-06; the
    edition build that read those masters afterwards recorded their file
    times, so they are not "changed outside"."""
    from musaeus.edition_ledger import ledger_path, open_ledger

    f = _master(cfg, "a.m4a", 440)
    _catalogue(cfg, f, written_days_ago=3)
    os.utime(f, None)  # changed after the recorded write...
    led = open_ledger(ledger_path(cfg))
    led.execute(
        "INSERT INTO edition_copies (edition, master_hash, master_path, master_mtime_ns, "
        "output_path, built_at) VALUES ('car', 'h', ?, ?, '/car/a.m4a', 'now')",
        (str(f), f.stat().st_mtime_ns),  # ...but a build read it as it is now
    )
    led.commit()
    led.close()
    assert _finding(cfg).level == "ok"
