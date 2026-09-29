"""A build stopped by SIGTERM or SIGHUP leaves no half-made copy behind
(cloud review of #53, finding 14).

Copies are encoded as *.edition_tmp inside the edition folder. Only Ctrl-C
cleaned them up: a shutdown (SIGTERM) or a closed terminal (SIGHUP) left a
partial file there -- and its ffmpeg kept encoding -- which the USB and
iPhone transfers then copied.
"""

from __future__ import annotations

import os
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

from musaeus.db import open_db, upsert_archive
from musaeus.edition_build import TMP_SUFFIX

ROOT = Path(__file__).resolve().parents[1]

pytestmark = pytest.mark.skipif(not shutil.which("ffmpeg"), reason="ffmpeg not available")


def _ffmpegs_writing(tmp_path: Path) -> list[int]:
    found = []
    for d in Path("/proc").glob("[0-9]*"):
        try:
            argv = (d / "cmdline").read_bytes().decode("utf-8", "replace").split("\0")
        except OSError:
            continue
        # By program name: a bare text search also matches the shell asking.
        if argv and os.path.basename(argv[0]) == "ffmpeg" and str(tmp_path) in " ".join(argv):
            found.append(int(d.name))
    return found


@pytest.mark.parametrize("sig", [signal.SIGTERM, signal.SIGHUP])
def test_a_signalled_build_cleans_up(tmp_path, sig):
    master = tmp_path / "Libraries" / "ALAC-Archival" / "Rock" / "Band" / "Al" / "Band - Long.m4a"
    master.parent.mkdir(parents=True)
    subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
         "-f", "lavfi", "-i", "sine=frequency=440:duration=300:sample_rate=44100",
         "-f", "lavfi", "-i", "sine=frequency=660:duration=300:sample_rate=44100",
         "-filter_complex", "[0]volume=-4dB[a];[1]volume=-9dB[b];[a][b]concat=n=2:v=0:a=1",
         "-ac", "2", "-c:a", "alac", str(master)],
        check=True,
    )  # fmt: skip
    conn = open_db(tmp_path / "musaeus.db")
    upsert_archive(conn, {"file_path": str(master), "status": "CATALOGUED",
                          "audio_hash": "h1", "codec": "alac"})  # fmt: skip
    conn.commit()
    conn.close()
    env = {**os.environ, "MUSAEUS_VAULT_ROOT": str(tmp_path),
           "MUSAEUS_DB_PATH": str(tmp_path / "musaeus.db"),
           "MUSAEUS_NO_SLEEP_INHIBIT": "1", "MUSAEUS_NO_IDLE_THROTTLE": "1"}  # fmt: skip
    proc = subprocess.Popen(
        [sys.executable, "-m", "musaeus.cli", "edition-build", "car"],
        cwd=ROOT, env=env, stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )  # fmt: skip
    car = tmp_path / "Libraries" / "CAR_Library"
    deadline = time.monotonic() + 90
    while not list(car.rglob(f"*{TMP_SUFFIX}")):
        assert proc.poll() is None, "the build ended before encoding"
        assert time.monotonic() < deadline, "no temporary copy appeared"
        time.sleep(0.2)
    time.sleep(1.0)  # well into the encode
    proc.send_signal(sig)  # the build alone, not its process group
    proc.wait(timeout=60)
    time.sleep(0.5)
    assert not list(car.rglob(f"*{TMP_SUFFIX}")), "a half-made copy was left"
    assert not _ffmpegs_writing(tmp_path), "its ffmpeg kept encoding"


def test_the_transfers_never_copy_a_half_made_copy():
    usb = (ROOT / "scripts" / "usb_transfer" / "transfer_to_usb.py").read_text()
    phone = (ROOT / "scripts" / "iphone_transfer.py").read_text()
    assert "edition_tmp" in usb and "edition_tmp" in phone
