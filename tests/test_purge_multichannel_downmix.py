"""scripts/musaeus_purge_multichannel.py --downmix (Grey, 2026-10-24).

Surround is downmixed to stereo and the song is kept. Run as it was, --downmix
dropped the cover art, emptied the recording's identity (audio_hash), turned
AAC into ALAC, and DELETED the song when a downmix failed.
"""

from __future__ import annotations

import importlib.util
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from musaeus.config import MusicConfig
from musaeus.db import open_db, upsert_archive

pytestmark = pytest.mark.skipif(
    not (shutil.which("ffmpeg") and shutil.which("ffprobe")), reason="ffmpeg/ffprobe needed"
)

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "musaeus_purge_multichannel.py"


def _load():
    spec = importlib.util.spec_from_file_location("musaeus_purge_multichannel", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


def _probe(path: Path, entries: str) -> str:
    return subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", entries, "-of", "csv=p=0", str(path)],
        capture_output=True, text=True, check=True,
    ).stdout  # fmt: skip


@pytest.fixture
def vault(tmp_path, monkeypatch):
    cfg = MusicConfig(
        vault_root=tmp_path, inbox=tmp_path / "INBOX", staging=tmp_path / "STAGING",
        quarantine=tmp_path / "QUARANTINE", runs_root=tmp_path / "RUNS",
        meta_dir=tmp_path / "MetaData", alac_library=tmp_path / "ALAC-Library",
        db_path=tmp_path / "musaeus.db",
    )  # fmt: skip
    cfg.ensure_dirs()
    song = tmp_path / "Libraries" / "Beck - The Paisley Experience.m4a"
    song.parent.mkdir(parents=True)
    subprocess.run(
        ["ffmpeg", "-y", "-f", "lavfi", "-i", "sine=frequency=440:duration=2", "-f", "lavfi",
         "-i", "color=c=red:s=32x32:d=1", "-map", "0:a", "-map", "1:v", "-ac", "6", "-c:a", "alac",
         "-c:v", "mjpeg", "-frames:v", "1", "-disposition:v", "attached_pic",
         "-metadata", "title=The Paisley Experience", str(song)],
        capture_output=True, check=True,
    )  # fmt: skip
    conn = open_db(cfg.db_path)
    upsert_archive(conn, {"file_path": str(song), "status": "CATALOGUED", "artist": "Beck",
                          "title": "The Paisley Experience", "codec": "alac", "channels": 6,
                          "duration": 2.0, "audio_hash": "as-it-arrived"})  # fmt: skip
    conn.execute("INSERT INTO archive_tier_hashes (path, sha256) VALUES (?, 'old')", (str(song),))
    conn.commit()
    conn.close()
    mod = _load()
    monkeypatch.setattr(mod, "get_config", lambda: cfg)
    monkeypatch.setattr(sys, "argv", ["prog", "--apply", "--downmix"])
    return mod, cfg, song


def _row(cfg):
    conn = open_db(cfg.db_path)
    try:
        return conn.execute("SELECT * FROM archive").fetchone()
    finally:
        conn.close()


def test_downmix_keeps_the_song_its_art_tags_and_identity(vault):
    mod, cfg, song = vault
    assert mod.main() == 0
    assert song.exists()
    assert _probe(song, "stream=channels").split()[0].strip(",") == "2"
    assert "mjpeg" in _probe(song, "stream=codec_name"), "the cover art is kept"
    assert "The Paisley Experience" in _probe(song, "format_tags=title")
    row = _row(cfg)
    assert row["channels"] == 2 and row["audio_hash"] == "as-it-arrived" and row["full_hash"]
    conn = open_db(cfg.db_path)
    assert conn.execute("SELECT COUNT(*) FROM archive_tier_hashes").fetchone()[0] == 0
    conn.close()
    assert not cfg.tunemymusic_csv_path.exists()


def test_a_failed_downmix_keeps_the_song_as_it_is(vault, monkeypatch):
    mod, cfg, song = vault
    from musaeus.stages import canonicalize as canon

    def _fail(src, dest):
        raise canon.CanonicalizeError("ffmpeg failed")

    monkeypatch.setattr(canon, "_convert_to_alac", _fail)
    mod.main()
    assert song.exists() and _row(cfg) is not None and _row(cfg)["channels"] == 6


def _gen(path: Path, channels: int, codec: str, seconds: int) -> None:
    subprocess.run(
        ["ffmpeg", "-y", "-f", "lavfi", "-i", f"sine=frequency=440:duration={seconds}", "-ac", str(channels),
         "-c:a", codec, "-f", "mp4", str(path)],
        capture_output=True, check=True,
    )  # fmt: skip


def test_the_file_decides_the_codec_so_lossless_stays_lossless(vault):
    """Codex review of #237 (reproduced): a row saying "aac" for 6-channel ALAC made
    the song lossy AAC."""
    mod, cfg, song = vault
    conn = open_db(cfg.db_path)
    conn.execute("UPDATE archive SET codec = 'aac'")
    conn.commit()
    conn.close()
    mod.main()
    assert "alac" in _probe(song, "stream=codec_name") and _row(cfg)["channels"] == 2


def test_a_shorter_copy_never_replaces_the_song(vault, monkeypatch):
    """Codex review of #237 (reproduced): Canonicalize's 2-second tolerance let a
    1-second copy replace a 2-second song in place."""
    mod, cfg, song = vault
    from musaeus.stages import canonicalize as canon

    monkeypatch.setattr(canon, "_convert_to_alac", lambda src, dest: _gen(dest, 2, "alac", 1))
    mod.main()
    assert _probe(song, "stream=channels").split()[0].strip(",") == "6", "the song is kept as it is"


def test_the_preview_of_a_downmix_says_downmix(vault, monkeypatch, capsys):
    """Codex review of #237 (reproduced): `--check --downmix` printed "would remove"."""
    mod, cfg, song = vault
    monkeypatch.setattr(sys, "argv", ["prog", "--check", "--downmix"])
    mod.main()
    out = capsys.readouterr().out
    assert "would downmix" in out and "would remove" not in out
