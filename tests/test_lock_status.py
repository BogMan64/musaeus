"""`musaeus lock status`: the one gate that sees both kinds of job.

The old gate (any_musaeus.py) looked only for a `musaeus` subcommand in argv.
A repair script holding the masters lock was invisible; so was the fact that
36 of the 55 scripts in scripts/ never take the lock at all.

A script that takes the lock leaves a record (pid, what, mode, since). This
command reads those records and the process table, and exits 1 when either
says a job is running.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from musaeus import cli


@pytest.fixture
def cfg_no_jobs(disposable_vault, monkeypatch):
    monkeypatch.setattr("musaeus.config.get_config", lambda: disposable_vault.cfg)
    monkeypatch.setattr("musaeus.edition_build.busy_musaeus", lambda procs, me: [])
    return disposable_vault.cfg


def _write_holder(cfg, *, pid: int, what: str = "repair_artist_casing.py") -> None:
    d = Path(cfg.runs_root) / "locks" / "masters.holders"
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{pid}-deadbeef.json").write_text(
        json.dumps({"pid": pid, "what": what, "mode": "exclusive", "since": "2026-11-25 09:00:00"}),
        encoding="utf-8",
    )


def test_clear_when_nothing_runs(cfg_no_jobs, capsys):
    assert cli._cmd_lock() == 0
    assert "clear:" in capsys.readouterr().out


def test_names_a_script_that_holds_the_lock(cfg_no_jobs, capsys):
    _write_holder(cfg_no_jobs, pid=os.getpid())
    assert cli._cmd_lock() == 1
    out = capsys.readouterr().out
    assert "masters lock:" in out
    assert "repair_artist_casing.py" in out
    assert str(os.getpid()) in out


def test_names_a_running_musaeus_job(cfg_no_jobs, monkeypatch, capsys):
    monkeypatch.setattr("musaeus.edition_build.busy_musaeus", lambda procs, me: [4242])
    assert cli._cmd_lock() == 1
    assert "pid 4242" in capsys.readouterr().out


def test_a_dead_holder_is_not_a_running_job(cfg_no_jobs, capsys):
    _write_holder(cfg_no_jobs, pid=999_999)
    assert cli._cmd_lock() == 0
    assert "clear:" in capsys.readouterr().out


def test_asking_creates_no_directories(tmp_path, monkeypatch, capsys):
    """A status check on a wrong path must not build anything there."""
    cfg = tmp_path / "nothing-here"
    from musaeus.config import MusicConfig

    config = MusicConfig(
        vault_root=cfg,
        inbox=cfg / "INBOX",
        staging=cfg / "STAGING",
        quarantine=cfg / "QUARANTINE",
        runs_root=cfg / "RUNS",
        meta_dir=cfg / "MetaData",
        alac_library=cfg / "ALAC-Library",
        db_path=cfg / "musaeus.db",
    )
    monkeypatch.setattr("musaeus.config.get_config", lambda: config)
    monkeypatch.setattr("musaeus.edition_build.busy_musaeus", lambda procs, me: [])

    assert cli._cmd_lock() == 0
    assert not cfg.exists(), "lock status must not create the vault"


# ── Two status runs must not see each other, and only `lock status` is free ──


def _procs(*argvs: list[str]) -> list[tuple[int, int, str, list[str]]]:
    return [(10 + i, 1, "python3", argv) for i, argv in enumerate(argvs)]


def test_asking_twice_does_not_make_either_run_busy():
    """A status run reads the process table; it must not count its twin."""
    from musaeus.edition_build import busy_musaeus

    procs = _procs(
        ["python3", "-m", "musaeus", "lock", "status"],
        ["/usr/bin/python3", "/usr/local/bin/musaeus", "lock"],
        ["python3", "-m", "musaeus.cli", "lock", "status"],
    )
    assert busy_musaeus(procs, me=99) == []


def test_a_real_job_is_still_busy_beside_a_status_run():
    from musaeus.edition_build import busy_musaeus

    procs = _procs(
        ["python3", "-m", "musaeus", "lock", "status"],
        ["python3", "-m", "musaeus", "run"],
    )
    assert busy_musaeus(procs, me=99) == [11]


def test_a_future_writing_lock_subcommand_is_not_exempt_in_advance():
    """If `lock` grows a writing verb, it is busy until it is listed as read-only."""
    from musaeus.edition_build import is_read_only

    assert is_read_only(["python3", "-m", "musaeus", "lock", "status"]) is True
    assert is_read_only(["python3", "-m", "musaeus", "lock"]) is True
    assert is_read_only(["python3", "-m", "musaeus", "lock", "acquire"]) is False
    assert is_read_only(["python3", "-m", "musaeus", "run"]) is False
    assert is_read_only(["python3", "-m", "musaeus"]) is False
    assert is_read_only(["python3", "-m", "pytest"]) is False
