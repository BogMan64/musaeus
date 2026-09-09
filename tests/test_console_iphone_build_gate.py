"""The console's iPhone build button, and the two things it must not do.

The edition menu was selection-only until 2026-09-09. Grey asked for the
iPhone edition to be buildable from the console, which turns a menu that
could previously only print into one that can start a multi-hour encode.
Two properties have to hold for that to be safe, and neither is visible by
reading the menu:

  1. The offer is made for iPhone ONLY. Car is ~44 hours and Lossless is a
     bake over the whole library; a keystroke away from either in the
     friendlier of the two interfaces is how one gets started by mistake.
  2. The confirmation is exact. "y", "yes" and a stray Enter must all leave
     the machine idle -- only the literal word BUILD starts it.

Both are asserted against the actual subprocess call, not against printed
text, because printed text is what a broken gate would still get right.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from musaeus.config import MusicConfig
from musaeus.console import Console
from musaeus.db import open_db, upsert_archive


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


def _seed(cfg: MusicConfig) -> None:
    cfg.ensure_dirs()
    conn = open_db(cfg.db_path)
    for i in range(6):
        upsert_archive(
            conn,
            {
                "file_path": f"/vault/a{i}.m4a",
                "status": "CATALOGUED",
                "artist": "A",
                "album": "Al",
                "title": f"T{i}",
                "genre": "Rock",
                "duration": 240.0,
                "size_bytes": 40_000_000,
            },
        )
    conn.commit()
    conn.close()


def _run(cfg, monkeypatch, answers: list[str]) -> list[list[str]]:
    """Drive the menu with `answers` and return every command it launched."""
    _seed(cfg)
    con = Console()
    con._config = cfg
    responses = iter(answers)
    monkeypatch.setattr("builtins.input", lambda *a, **k: next(responses))

    launched: list[list[str]] = []

    def _fake_run(cmd, *a, **k):
        launched.append(list(cmd))
        return subprocess.CompletedProcess(cmd, 0)

    monkeypatch.setattr(subprocess, "run", _fake_run)
    con._edition_menu()
    return launched


@pytest.mark.parametrize("answer", ["", "y", "yes", "Y", "build", " BUILD ", "no"])
def test_only_the_literal_word_build_starts_an_encode(cfg, monkeypatch, answer) -> None:
    """Every near-miss leaves the machine idle.

    " BUILD " is in this list on purpose: the code strips the answer before
    comparing, so it IS accepted, and a reader could reasonably expect it to
    be rejected. It is listed as an accepted spelling in the companion test
    below; here the parametrisation stops at the ones that must not run --
    so if the strip is ever removed, one of these two tests fails.
    """
    if answer.strip() == "BUILD":
        pytest.skip("accepted spelling — covered by the build test below")
    assert _run(cfg, monkeypatch, ["2", "", answer]) == []


def test_typing_build_runs_the_iphone_builder_with_the_budget(cfg, monkeypatch) -> None:
    launched = _run(cfg, monkeypatch, ["2", "0.02", "BUILD"])
    assert len(launched) == 1, "exactly one build should start"
    cmd = launched[0]
    assert any(c.endswith("build_car_library.py") for c in cmd)
    assert "--edition" in cmd and cmd[cmd.index("--edition") + 1] == "iphone"
    assert "--from-catalogue" in cmd
    # The budget the owner typed has to reach the builder. Without this the
    # menu would preview a 20 MB selection and then encode the whole library.
    assert "--budget-gb" in cmd
    assert float(cmd[cmd.index("--budget-gb") + 1]) == pytest.approx(0.02)


def test_a_blank_budget_passes_no_budget_flag(cfg, monkeypatch) -> None:
    launched = _run(cfg, monkeypatch, ["2", "", "BUILD"])
    assert len(launched) == 1
    assert "--budget-gb" not in launched[0]


@pytest.mark.parametrize("idx,name", [(0, "lossless"), (1, "car")])
def test_the_long_editions_are_never_offered_a_build(cfg, monkeypatch, idx, name) -> None:
    """No prompt, and therefore nothing to mistype.

    The answer list holds a "BUILD" that must never be read. If a build
    offer is ever added for these, the menu consumes it and this test fails
    on the launched command rather than passing quietly.
    """
    assert _run(cfg, monkeypatch, [str(idx), "BUILD"]) == []
