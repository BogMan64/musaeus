"""Grey, 2026-10-19: two iPhone copies were refused by the click check at
every bandwidth -- Muddy Waters' "You Need Love" (+3.4 dB at 64.3 s) and
Smokey Robinson & The Miracles' "Going To A Go Go" (+5.6 dB at 15.2 s).
Their masters are squashed flat (Smokey's peaks sit at -10.2 dBFS right
through), and the encoder's overshoot on those flat tops is what the check
flags. Lowering the loudness or the limiter changed nothing. Grey listened
to the full-band copies, heard no click, and said "yes allow".

MetaData/click_allowed.tsv lists such masters by audio hash: their
full-band copy is kept, flagged or not. Every other song is still refused.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from musaeus import edition_bake as bake
from musaeus import edition_build as eb
from musaeus.config import MusicConfig
from musaeus.db import open_db, upsert_archive

H = "abcdef0123456789" * 4
SONG = "Blues/A/Album/A - Song.m4a"

needs_encoders = pytest.mark.skipif(
    not (shutil.which("ffmpeg") and shutil.which("fdkaac")), reason="needs ffmpeg and fdkaac"
)

# Run in the command's process before musaeus is used: every copy "clicks".
_ALWAYS_CLICKS = (
    "import musaeus.edition_bake as b\nb._click_in = lambda went_in, came_out: (10, 5.6)\n"
)


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


def _catalogue_one_master(cfg: MusicConfig) -> None:
    master = cfg.alac_archive / SONG
    master.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi",
         "-i", "sine=frequency=440:duration=6", "-c:a", "alac", "-sample_fmt", "s16p",
         str(master)],
        check=True, stdin=subprocess.DEVNULL,
    )  # fmt: skip
    conn = open_db(cfg.db_path)
    upsert_archive(conn, {"file_path": str(master), "status": "CATALOGUED", "audio_hash": H,
                          "codec": "alac", "size_bytes": master.stat().st_size})  # fmt: skip
    conn.commit()
    conn.close()


def _build_iphone(cfg: MusicConfig, tmp_path: Path) -> subprocess.CompletedProcess[str]:
    hook = tmp_path / "hook"
    hook.mkdir(exist_ok=True)
    (hook / "sitecustomize.py").write_text(_ALWAYS_CLICKS)
    repo = Path(__file__).resolve().parents[1]
    env = {**os.environ, "MUSAEUS_VAULT_ROOT": str(cfg.vault_root),
           "MUSAEUS_DB_PATH": str(cfg.db_path), "PYTHONPATH": f"{hook}{os.pathsep}{repo}",
           "MUSAEUS_NO_SLEEP_INHIBIT": "1", "MUSAEUS_NO_IDLE_THROTTLE": "1"}  # fmt: skip
    return subprocess.run(
        [sys.executable, "-m", "musaeus.cli", "edition-build", "iphone"],
        capture_output=True, text=True, env=env, stdin=subprocess.DEVNULL,
        cwd=repo, timeout=180,
    )  # fmt: skip


def _copies(cfg: MusicConfig) -> list[Path]:
    return sorted(p for p in cfg.iphone_library.rglob("*.m4a") if p.is_file())


@needs_encoders
def test_a_listed_song_gets_its_iphone_copy_though_the_check_flags_it(cfg, tmp_path):
    _catalogue_one_master(cfg)
    (cfg.meta_dir / "click_allowed.tsv").write_text(
        "# audio hash<TAB>song: Grey listened, no click\n"
        f"{H}\tA - Song (no click heard, 2026-10-19)\n"
    )

    r = _build_iphone(cfg, tmp_path)

    assert "Baked 1" in r.stdout, r.stdout + r.stderr
    assert len(_copies(cfg)) == 1, r.stdout


@needs_encoders
def test_a_song_not_on_the_list_is_still_refused(cfg, tmp_path):
    _catalogue_one_master(cfg)
    (cfg.meta_dir / "click_allowed.tsv").write_text(f"{'0' * 64}\tsomeone else\n")

    r = _build_iphone(cfg, tmp_path)

    assert "Baked 0" in r.stdout and "1 failed" in r.stdout, r.stdout + r.stderr
    logs = "".join(p.read_text() for p in (cfg.runs_root / "LOGS").glob("edition_iphone_*"))
    assert "click" in logs, logs
    assert _copies(cfg) == []


@needs_encoders
def test_a_listed_song_keeps_the_full_band_copy_grey_heard(tmp_path, monkeypatch):
    # Grey listened to the 20 kHz copy: that one is kept, not a narrower
    # re-encode nobody heard.
    widths: list[int] = []
    real_pipe = bake._run_pipeline

    def spy(first, second, timeout):
        widths.append(int(second[second.index("-w") + 1]))
        return real_pipe(first, second, timeout)

    monkeypatch.setattr(bake, "_run_pipeline", spy)
    monkeypatch.setattr(bake, "_click_in", lambda went_in, came_out: (10, 5.6))
    master = tmp_path / "m.m4a"
    subprocess.run(
        ["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi",
         "-i", "sine=frequency=440:duration=6", "-c:a", "alac", "-sample_fmt", "s16p",
         str(master)],
        check=True, stdin=subprocess.DEVNULL,
    )  # fmt: skip

    bake.bake_aac(master, tmp_path / "phone.m4a", noise=False, allow_click=True)

    assert widths == [bake.AAC_BANDWIDTH_HZ], widths


def test_the_list_reads_the_first_column_and_skips_notes(tmp_path):
    f = tmp_path / "click_allowed.tsv"
    f.write_text(f"# a note\n\n{H}\tA - Song\n  {'1' * 64}  \n")
    assert eb.load_click_allowed(f) == frozenset({H, "1" * 64})
    assert eb.load_click_allowed(tmp_path / "none.tsv") == frozenset()


def test_the_lossless_bake_is_called_as_before(tmp_path, monkeypatch):
    # Stand-ins for a bake take (src, tmp, known) only: the flag is passed
    # only when it is set, and never for the Lossless edition.
    calls: list[tuple] = []

    def stand_in(src, tmp, known):
        calls.append((src, tmp, known))
        tmp.write_bytes(b"x")
        return bake.BakeResult(-18.0, "linear", "r", {})

    kind = eb.Kind(
        "lossless", bake.TARGET_I, "alac_library", eb._mirror, stand_in, include_lossy=False
    )
    monkeypatch.setattr(bake, "copy_tags", lambda *a: None)
    m = eb.Master(tmp_path / "m.m4a", H, "alac", None, None, 1, None, decode_ok=1)
    eb._bake_one(m, tmp_path / "out" / "m.m4a", kind, {}, False)
    assert len(calls) == 1
