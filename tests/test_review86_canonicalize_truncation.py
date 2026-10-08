"""Review of #86, finding 15 (2026-10-07): canonicalize's truncation check
compared the recorded duration with the stream duration ffprobe reports -- and
in an MP4 both come from the moov atom, written before the audio. A file cut to
a third still claims its full length. Only a decode knows; the sampled
conversions are now decoded (duration.decodes_cleanly).
"""

from __future__ import annotations

import shutil
import subprocess

import pytest

from musaeus.config import MusicConfig
from musaeus.context import RunContext
from musaeus.db import open_db, upsert_archive
from musaeus.stages.canonicalize import CanonicalizeStage

pytestmark = pytest.mark.skipif(not shutil.which("ffmpeg"), reason="requires ffmpeg")


def test_a_truncated_conversion_is_caught(tmp_path):
    cfg = MusicConfig(
        vault_root=tmp_path, inbox=tmp_path / "INBOX", staging=tmp_path / "STAGING",
        quarantine=tmp_path / "Q", runs_root=tmp_path / "RUNS", meta_dir=tmp_path / "MetaData",
        alac_library=tmp_path / "ALAC-Library", db_path=tmp_path / "musaeus.db",
    )  # fmt: skip
    cfg.ensure_dirs()
    ctx = RunContext.new(cfg, open_db(cfg.db_path), dry_run=False)
    song = cfg.staging / "song.m4a"
    subprocess.run(["ffmpeg", "-nostdin", "-y", "-v", "error", "-f", "lavfi", "-i",
                    "sine=f=440:d=30", "-c:a", "alac", "-movflags", "+faststart", str(song)],
                   check=True)  # fmt: skip
    data = song.read_bytes()
    song.write_bytes(data[: len(data) // 3])  # the header stays, two thirds of the audio go
    upsert_archive(ctx.conn, {"file_path": str(song), "status": "CATALOGUED", "duration": 30.0,
                              "audio_hash": "h", "codec": "alac"})  # fmt: skip
    ctx.conn.execute(
        "UPDATE archive SET canon_action = 'CONVERTED' WHERE file_path = ?", (str(song),)
    )
    ctx.log_event("CANONICALIZE", file_path=str(song), stage=CanonicalizeStage.NAME)
    ctx.conn.commit()
    stage = CanonicalizeStage()
    result = stage._make_result(dry_run=False)
    result.files_changed = 1

    problems = stage.verify_effect(ctx, result)

    assert any("song.m4a" in p for p in problems), "a truncated file passed the check"
