"""Findings 4-7, 11 and 12 of the review of PR #87 (2026-10-07): the monthly
bit-rot check, which repairs masters from a backup on its own.

4. "Replaced on purpose" was decided by the catalogue's audio_hash, which is
   the audio as it ARRIVED. A transcoded master's audio never matches it, so
   a deliberate replacement of one was treated as rot and reverted.
5. A decode that timed out returned a whole-file hash in place of the audio
   hash: a healthy re-tagged master then read as rot, and a rebaseline stored
   the wrong kind of hash.
6. ffmpeg ran without -nostdin: run by hand in a terminal, a keypress could
   end the decode early.
7. The repair moved the master aside before the restored copy was in place
   and verified: a kill in between left no master at all. Any exception in a
   repair also ended the whole run and its report.
11. A master that could not be read at all still gave an all-clear.
12. After a rebaseline, verify_effect reported "nothing wrong" without
    looking at anything.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

from musaeus import hasher
from musaeus.config import MusicConfig
from musaeus.context import RunContext
from musaeus.db import open_db, upsert_archive
from musaeus.hasher import audio_hash, file_hash
from musaeus.stages import bitrot
from musaeus.stages.base import NO_VERIFICATION
from musaeus.stages.bitrot import BitRotStage

pytestmark = pytest.mark.skipif(not shutil.which("ffmpeg"), reason="requires ffmpeg")
REL = Path("Rock/America/Hearts/America - Sister Golden Hair.m4a")


def _song(path: Path, freq: int) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-f", "lavfi",
                    "-i", f"sine=frequency={freq}:duration=1", "-c:a", "alac", str(path)],
                   check=True, capture_output=True)  # fmt: skip
    return path


def _retag(path: Path) -> None:
    from mutagen.mp4 import MP4

    audio = MP4(str(path))
    if audio.tags is None:
        audio.add_tags()
    audio.tags["\xa9nam"] = ["Retagged"]
    audio.save()


@pytest.fixture
def vault(tmp_path, monkeypatch):
    archive = tmp_path / "Libraries" / "ALAC-Archival"
    cfg = MusicConfig(
        vault_root=tmp_path, inbox=tmp_path / "INBOX", staging=tmp_path / "STAGING",
        quarantine=tmp_path / "Q", runs_root=tmp_path / "RUNS", meta_dir=tmp_path / "MetaData",
        alac_library=tmp_path / "ALAC-Library", db_path=tmp_path / "musaeus.db", alac_archive=archive,
    )  # fmt: skip
    ctx = RunContext.new(cfg, open_db(cfg.db_path), dry_run=False)
    master = _song(archive / REL, 440)
    upsert_archive(ctx.conn, {"file_path": str(master), "status": "CATALOGUED", "artist": "America",
                              "title": "Sister Golden Hair", "audio_hash": audio_hash(master)})  # fmt: skip
    ctx.conn.commit()
    backups = tmp_path / "NUC"
    shutil.copytree(archive, backups / "2.-MUSAEUS_ALAC_Archive_20261006" / "ALAC-Archival")
    monkeypatch.setenv("MUSAEUS_BACKUP_ROOTS", str(backups))
    sent: list = []
    monkeypatch.setattr(bitrot, "_notify", lambda r, u: sent.append((r, u)), raising=False)
    ctx.set("bitrot_rebaseline", True)
    BitRotStage().execute(ctx)
    ctx.set("bitrot_rebaseline", False)
    return ctx, master, sent


def _damaged(ctx) -> list[Path]:
    return list((ctx.config.vault_root / "REVIEW" / "BITROT_DAMAGED").rglob("*.m4a"))


# ── 4 ───────────────────────────────────────────────────────────────────────


def test_a_transcoded_master_replaced_on_purpose_is_not_put_back(vault):
    ctx, master, sent = vault
    ctx.conn.execute("UPDATE archive_tier_hashes SET baselined_at = '2026-10-01 00:00:00'")
    _song(master, 880)  # the swap tool filed a better copy here, transcoded on the way in...
    ctx.conn.execute(
        "UPDATE archive SET audio_hash = 'the-arrival-audio-of-the-mp3', "
        "finalized_at = '2026-10-07 12:00:00' WHERE file_path = ?",
        (str(master),),
    )  # ...so the catalogue holds the audio as it arrived, not the master's
    ctx.conn.commit()
    new_audio = audio_hash(master)

    BitRotStage().execute(ctx)

    assert audio_hash(master) == new_audio, "a deliberate replacement was reverted"
    assert sent == [] and _damaged(ctx) == []


def test_rot_in_a_master_filed_before_its_baseline_is_still_repaired(vault):
    ctx, master, _ = vault
    ctx.conn.execute(
        "UPDATE archive SET finalized_at = '2026-09-01 00:00:00' WHERE file_path = ?",
        (str(master),),
    )
    ctx.conn.commit()
    good = audio_hash(master)
    _song(master, 880)

    BitRotStage().execute(ctx)

    assert audio_hash(master) == good


# ── 5 ───────────────────────────────────────────────────────────────────────


def test_a_decode_that_times_out_is_reported_not_repaired(vault, monkeypatch):
    ctx, master, sent = vault
    _retag(master)  # bytes changed, audio did not
    before = master.read_bytes()
    monkeypatch.setattr(hasher, "_audio_hash_timeout", lambda *a, **k: 0)

    result = BitRotStage().execute(ctx)

    assert master.read_bytes() == before, "a healthy master was replaced from a backup"
    assert _damaged(ctx) == [] and sent == []
    assert result.success is False
    assert any(n.startswith("COULD NOT CHECK") for n in result.notes)


def test_a_rebaseline_never_stores_a_whole_file_hash_as_audio(vault, monkeypatch):
    ctx, master, _ = vault
    good = audio_hash(master)
    monkeypatch.setattr(hasher, "_audio_hash_timeout", lambda *a, **k: 0)
    ctx.set("bitrot_rebaseline", True)

    BitRotStage().execute(ctx)

    stored = ctx.conn.execute(
        "SELECT audio_hash FROM archive_tier_hashes WHERE path = ?", (str(master),)
    ).fetchone()[0]
    assert stored != file_hash(master)
    assert stored == good, "the audio identity recorded before must be kept"


# ── 6 ───────────────────────────────────────────────────────────────────────


def test_ffmpeg_never_reads_the_terminal(monkeypatch, tmp_path):
    seen = {}

    class Stop(Exception):
        pass

    def popen(cmd, **kwargs):
        seen["cmd"], seen["kwargs"] = cmd, kwargs
        raise Stop

    song = _song(tmp_path / "a.m4a", 440)  # made before Popen is replaced
    monkeypatch.setattr(hasher.subprocess, "Popen", popen)
    with pytest.raises(Stop):
        audio_hash(song)
    assert "-nostdin" in seen["cmd"]
    assert seen["kwargs"].get("stdin") == subprocess.DEVNULL


# ── 7 ───────────────────────────────────────────────────────────────────────


def test_a_repair_killed_midway_never_leaves_the_master_path_empty(vault, monkeypatch):
    ctx, master, _ = vault
    _song(master, 880)  # rot
    damaged = master.read_bytes()

    def killed(*a, **k):
        raise RuntimeError("killed")

    monkeypatch.setattr(os, "replace", killed)
    result = BitRotStage().execute(ctx)
    monkeypatch.undo()

    assert master.is_file(), "the master's path was left empty"
    assert master.read_bytes() == damaged
    assert any("NOT repaired" in n for n in result.notes), "one failed repair ended the report"


def test_a_good_repair_keeps_the_damaged_file(vault):
    ctx, master, _ = vault
    good = audio_hash(master)
    _song(master, 880)

    BitRotStage().execute(ctx)

    assert audio_hash(master) == good
    kept = _damaged(ctx)
    assert len(kept) == 1 and audio_hash(kept[0]) != good


# ── 11 ──────────────────────────────────────────────────────────────────────


def test_an_unreadable_master_is_not_an_all_clear(vault, monkeypatch):
    ctx, master, _ = vault
    real = bitrot.file_hash

    def unreadable(path):
        if Path(path) == master:
            raise OSError(5, "Input/output error")
        return real(path)

    monkeypatch.setattr(bitrot, "file_hash", unreadable)
    result = BitRotStage().execute(ctx)

    assert result.success is False
    assert any(n.startswith("COULD NOT READ") for n in result.notes)
    wrapper = (Path(__file__).parents[1] / "scripts" / "bitrot_monthly.sh").read_text()
    assert "COULD NOT" in wrapper, "the monthly summary must carry it"


# ── 12 ──────────────────────────────────────────────────────────────────────


def test_after_a_rebaseline_verify_effect_checks_the_rows(vault):
    ctx, master, _ = vault
    ctx.set("bitrot_rebaseline", True)
    stage = BitRotStage()
    result = stage.execute(ctx)
    assert stage.verify_effect(ctx, result) == []

    ctx.conn.execute("DELETE FROM archive_tier_hashes")
    ctx.conn.commit()
    verdict = stage.verify_effect(ctx, result)
    assert verdict not in ([], NO_VERIFICATION), "claimed baselines that are not there"
