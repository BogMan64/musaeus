"""The console's Lossless build (Grey, 2026-09-27).

"Add it to menu item 6 like the iPhone one: preview first, then type BUILD."
The plan is shown in-process and writes nothing; only the literal word BUILD
starts `musaeus edition-build lossless`, as a separate process. Asserted
against the subprocess call itself, not printed text.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from musaeus import edition_build
from musaeus.config import MusicConfig
from musaeus.console import Console
from musaeus.db import open_db, upsert_archive


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
    conn = open_db(c.db_path)
    for i in range(3):
        master = c.alac_archive / "Rock" / "A" / "Al" / f"A - T{i}.m4a"
        master.parent.mkdir(parents=True, exist_ok=True)
        master.write_bytes(b"x")
        upsert_archive(
            conn,
            {
                "file_path": str(master),
                "status": "CATALOGUED",
                "artist": "A",
                "album": "Al",
                "title": f"T{i}",
                "genre": "Rock",
                "audio_hash": f"h{i}",
                "codec": "alac",
                "size_bytes": 30_000_000,
            },
        )
    conn.commit()
    conn.close()
    return c


def _files(cfg) -> list[str]:
    # SQLite's -wal/-shm appear when the catalogue is merely opened; they are
    # housekeeping, not something written.
    return sorted(
        str(p) for p in Path(cfg.vault_root).rglob("*") if not str(p).endswith(("-wal", "-shm"))
    )


def _run(cfg, monkeypatch, answers: list[str], *, running: list[int] | None = None):
    con = Console()
    con._config = cfg
    responses = iter(answers)
    monkeypatch.setattr("builtins.input", lambda *a, **k: next(responses))
    monkeypatch.setattr(edition_build, "pipeline_pids", lambda: running or [])
    launched: list[list[str]] = []

    def _fake_run(cmd, *a, **k):
        launched.append(list(cmd))
        return subprocess.CompletedProcess(cmd, 0)

    monkeypatch.setattr(subprocess, "run", _fake_run)
    before = _files(cfg)
    con._edition_menu()
    after = _files(cfg)
    return launched, before == after


def test_the_label_names_the_vault_not_home_music(cfg, monkeypatch, capsys):
    _run(cfg, monkeypatch, ["4"])  # Back
    out = capsys.readouterr().out
    assert "vault Libraries/ALAC_Library" in out
    assert "/home/grey/Music" not in out


@pytest.mark.parametrize("answer", ["", "y", "yes", "build", "no"])
def test_the_plan_is_shown_and_only_build_starts_it(cfg, monkeypatch, capsys, answer):
    launched, untouched = _run(cfg, monkeypatch, ["1", answer])
    out = capsys.readouterr().out
    assert "To bake       : 3 track(s)" in out, out
    assert launched == []
    assert untouched, "showing the plan must not create or remove anything"


def test_typing_build_runs_the_lossless_builder(cfg, monkeypatch):
    launched, _ = _run(cfg, monkeypatch, ["1", "BUILD"])
    assert len(launched) == 1
    assert launched[0][-3:] == ["musaeus.cli", "edition-build", "lossless"][-3:]
    assert "edition-build" in launched[0] and "lossless" in launched[0]


def test_no_build_is_offered_while_a_pipeline_run_is_going(cfg, monkeypatch, capsys):
    launched, _ = _run(cfg, monkeypatch, ["1", "BUILD"], running=[4242])
    assert launched == []
    assert "is running" in capsys.readouterr().out


def test_the_real_destination_is_shown_before_build_is_asked(cfg, monkeypatch, capsys):
    # Second review of #49: the label is a fixed string (Grey's choice), but
    # the build writes to the configured folder, which an environment
    # variable can re-point. The screen that asks for BUILD must name it.
    prompts: list[str] = []
    monkeypatch.setattr(
        "musaeus.console._prompt", lambda text, *a, **k: prompts.append(text) or "no"
    )
    _run(cfg, monkeypatch, ["1"])
    out = capsys.readouterr().out
    assert prompts, "BUILD was never asked"
    assert str(cfg.alac_library) in out, out
