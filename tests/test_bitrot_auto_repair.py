"""Rot found in a master is repaired from a backup copy on its own (Grey, 2026-10-06).

"If it did find corruption, can it automatically check the USB2 or NUC8TB for a better one and
copy it over on its own, with a notification at the end of the run saying what it did?"
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from musaeus import bitrot_repair
from musaeus.config import MusicConfig
from musaeus.context import RunContext
from musaeus.db import open_db, upsert_archive
from musaeus.hasher import audio_hash
from musaeus.stages import bitrot
from musaeus.stages.bitrot import BitRotStage

pytestmark = pytest.mark.skipif(not shutil.which("ffmpeg"), reason="requires ffmpeg")
REL = Path("Rock/America/Hearts/America - Sister Golden Hair.m4a")


def _song(path: Path, freq: int) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-f", "lavfi",
                    "-i", f"sine=frequency={freq}:duration=1", "-c:a", "alac", str(path)],
                   check=True, capture_output=True)  # fmt: skip
    return path


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
    monkeypatch.setattr(
        bitrot,
        "_notify",
        lambda repaired, unrepaired: sent.append((repaired, unrepaired)),
        raising=False,
    )
    ctx.set("bitrot_rebaseline", True)
    BitRotStage().execute(ctx)
    ctx.set("bitrot_rebaseline", False)
    return ctx, master, backups, sent


def _rot(master: Path) -> str:
    """The bytes under the song change: different audio, same path."""
    good = audio_hash(master)
    _song(master, 880)
    return good


def test_rot_is_repaired_from_the_backup_and_reported(vault):
    ctx, master, _, sent = vault
    good = _rot(master)
    BitRotStage().execute(ctx)
    assert audio_hash(master) == good
    damaged = list((ctx.config.vault_root / "REVIEW" / "BITROT_DAMAGED").rglob("*.m4a"))
    assert len(damaged) == 1 and audio_hash(damaged[0]) != good  # set aside, not deleted
    assert len(sent) == 1 and len(sent[0][0]) == 1 and not sent[0][1]
    ev = ctx.conn.execute(
        "SELECT event_type FROM events WHERE event_type LIKE 'BITROT_%REPAIRED'"
    ).fetchall()
    assert [e[0] for e in ev] == ["BITROT_REPAIRED"]


def test_a_repaired_master_then_verifies_clean(vault):
    ctx, master, _, _ = vault
    _rot(master)
    BitRotStage().execute(ctx)
    again = BitRotStage().execute(ctx)
    assert not any("MISMATCH" in n or "REPAIRED" in n for n in again.notes)


def test_a_master_musaeus_replaced_on_purpose_is_not_put_back(vault):
    ctx, master, _, sent = vault
    _rot(master)  # the swap tool put a better copy at this path...
    ctx.conn.execute(
        "UPDATE archive SET audio_hash = ? WHERE file_path = ?", (audio_hash(master), str(master))
    )
    ctx.conn.commit()  # ...and the catalogue knows its audio
    new_audio = audio_hash(master)
    BitRotStage().execute(ctx)
    assert audio_hash(master) == new_audio and sent == []


def test_a_backup_that_rotted_too_is_never_used(vault):
    ctx, master, backups, sent = vault
    good = _rot(master)
    damaged = audio_hash(master)
    _song(backups / "2.-MUSAEUS_ALAC_Archive_20261006" / "ALAC-Archival" / REL, 660)
    result = BitRotStage().execute(ctx)
    assert audio_hash(master) == damaged != good  # left as it is: no good copy to restore
    assert sent and sent[0][1] and not sent[0][0]
    assert result.success is False


def test_backup_copies_newest_first_and_an_unplugged_drive_is_skipped(tmp_path):
    for name in ("2.-MUSAEUS_ALAC_Archive_20260924", "2.-MUSAEUS_ALAC_Archive_20261006"):
        (tmp_path / "NUC" / name / "ALAC-Archival").mkdir(parents=True)
    (tmp_path / "USB2" / "ALAC-Archival").mkdir(parents=True)
    copies = bitrot_repair.backup_copies(
        [tmp_path / "NUC", tmp_path / "USB2", tmp_path / "unplugged"]
    )
    assert [c.parent.name for c in copies] == [
        "2.-MUSAEUS_ALAC_Archive_20261006", "2.-MUSAEUS_ALAC_Archive_20260924", "USB2"
    ]  # fmt: skip
