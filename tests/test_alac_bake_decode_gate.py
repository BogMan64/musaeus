"""A damaged master must not be baked into an edition.

The bake reads a master and writes a second file from it. If the master is
damaged inside the stream, the bake faithfully produces a damaged EDITION --
a second copy of the same fault, in the tier whose whole purpose is to be
the one you actually listen to.

Four such masters surfaced on 2026-09-06 only because the bake happened to
touch them: Billy Joel, Nina Simone, Bachman-Turner Overdrive and Tower of
Power, all reporting `[alac] Error` or a truncated mov atom. Nothing was
checking first. This gate is that check, and it went in on 2026-09-08 on
Grey's instruction to run the decode audit BEFORE the CAR build rather than
after -- the same reasoning, one tier up.

The gate decodes a never-checked row rather than waving it through. Skipping
unchecked rows would leave exactly the hole CorruptStage's bounded
NEW_ARRIVAL_DECODE_BUDGET leaves: a file nothing has got round to looking
at is indistinguishable from a file that passed.
"""

from __future__ import annotations

import importlib.util as _ilu
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from musaeus.config import MusicConfig
from musaeus.db import open_db, upsert_archive
from musaeus.deep_scan import ensure_columns

pytestmark = pytest.mark.skipif(
    not (shutil.which("ffmpeg") and shutil.which("ffprobe")),
    reason="ffmpeg/ffprobe not available",
)

_REPO_ROOT = Path(__file__).resolve().parent.parent
_SCRIPT = _REPO_ROOT / "scripts" / "alac_library" / "build_alac_library.py"

_spec = _ilu.spec_from_file_location("build_alac_library_gate", _SCRIPT)
assert _spec and _spec.loader
_bal = _ilu.module_from_spec(_spec)
sys.modules["build_alac_library_gate"] = _bal
_spec.loader.exec_module(_bal)


@pytest.fixture
def cfg(tmp_path: Path) -> MusicConfig:
    return MusicConfig(
        vault_root=tmp_path,
        inbox=tmp_path / "INBOX",
        staging=tmp_path / "STAGING",
        quarantine=tmp_path / "QUARANTINE",
        runs_root=tmp_path / "RUNS",
        meta_dir=tmp_path / "MetaData",
        alac_library=tmp_path / "ALAC-Library",
        db_path=tmp_path / "musaeus.db",
    )


@pytest.fixture
def conn(cfg: MusicConfig):
    c = open_db(cfg.db_path)
    ensure_columns(c)
    return c


def _tone(path: Path, seconds: int = 3) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [
            "ffmpeg",
            "-v",
            "error",
            "-f",
            "lavfi",
            "-i",
            f"sine=frequency=440:duration={seconds}",
            "-c:a",
            "alac",
            str(path),
            "-y",
        ],
        check=True,
        capture_output=True,
    )
    return path


def _row(conn, path: Path) -> int:
    upsert_archive(
        conn,
        {
            "file_path": str(path),
            "status": "CATALOGUED",
            "codec": "alac",
            "duration": 3.0,
            "title": path.stem,
        },
    )
    conn.commit()
    return conn.execute("SELECT id FROM archive WHERE file_path = ?", (str(path),)).fetchone()["id"]


def test_a_sound_master_passes_the_gate(conn, cfg) -> None:
    f = _tone(cfg.vault_root / "ALAC_Archive" / "A" / "good.m4a")
    assert _bal._decode_gate(conn, _row(conn, f), f, execute=True) == ""


def test_the_verdict_is_recorded_so_the_next_run_need_not_repeat_it(conn, cfg) -> None:
    f = _tone(cfg.vault_root / "ALAC_Archive" / "A" / "good.m4a")
    rid = _row(conn, f)
    _bal._decode_gate(conn, rid, f, execute=True)
    got = conn.execute(
        "SELECT decode_ok, decode_checked_at FROM archive WHERE id = ?", (rid,)
    ).fetchone()
    assert got["decode_ok"] == 1
    assert got["decode_checked_at"]


def test_a_dry_run_records_nothing(conn, cfg) -> None:
    """The rest of this script writes nothing without --execute; nor does this."""
    f = _tone(cfg.vault_root / "ALAC_Archive" / "A" / "good.m4a")
    rid = _row(conn, f)
    _bal._decode_gate(conn, rid, f, execute=False)
    got = conn.execute("SELECT decode_checked_at FROM archive WHERE id = ?", (rid,)).fetchone()
    assert got["decode_checked_at"] is None


def test_a_damaged_master_is_refused(conn, cfg) -> None:
    """Zeroed bytes mid-file: same size, same header, genuinely undecodable.

    Deliberately NOT a truncation -- a truncated file is already caught by
    the size-against-duration heuristic, so it would not prove the gate is
    doing anything the existing checks did not already do.
    """
    f = _tone(cfg.vault_root / "ALAC_Archive" / "A" / "broken.m4a", seconds=30)
    data = bytearray(f.read_bytes())
    for i in range(int(len(data) * 0.4), int(len(data) * 0.6)):
        data[i] = 0
    f.write_bytes(bytes(data))
    verdict = _bal._decode_gate(conn, _row(conn, f), f, execute=True)
    assert verdict.startswith("SKIP")
    assert "fails to decode" in verdict


def test_a_row_already_known_bad_is_refused_without_decoding_again(conn, cfg, monkeypatch) -> None:
    f = cfg.vault_root / "ALAC_Archive" / "A" / "known_bad.m4a"
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_bytes(b"not real audio -- decode_ok is pre-set, so nothing should read it")
    rid = _row(conn, f)
    conn.execute(
        "UPDATE archive SET decode_ok = 0, decode_checked_at = ? WHERE id = ?",
        ("2026-09-06 00:00:00", rid),
    )
    conn.commit()

    def _explode(*a, **k):
        raise AssertionError("a settled verdict must not be re-decoded")

    monkeypatch.setattr(_bal, "ffmpeg_decode_check", _explode)
    verdict = _bal._decode_gate(conn, rid, f, execute=True)
    assert verdict.startswith("SKIP")
    assert "--recheck-failures" in verdict, "the refusal must say how to appeal it"


def test_a_never_checked_row_is_decoded_rather_than_waved_through(conn, cfg) -> None:
    """The hole this closes: unchecked must not be treated as checked-and-fine."""
    f = _tone(cfg.vault_root / "ALAC_Archive" / "A" / "fresh.m4a")
    rid = _row(conn, f)
    assert (
        conn.execute("SELECT decode_checked_at FROM archive WHERE id = ?", (rid,)).fetchone()[
            "decode_checked_at"
        ]
        is None
    )
    calls: list[Path] = []
    real = _bal.ffmpeg_decode_check
    try:
        _bal.ffmpeg_decode_check = lambda p, **k: (calls.append(p), real(p, **k))[1]
        _bal._decode_gate(conn, rid, f, execute=True)
    finally:
        _bal.ffmpeg_decode_check = real
    assert calls == [f]
