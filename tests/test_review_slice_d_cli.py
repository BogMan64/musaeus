"""Review of slice D (#146), findings 5, 8, 9 and 10."""

from __future__ import annotations

import sys

import pytest

from musaeus import cli
from musaeus.config import MusicConfig
from musaeus.planner import RunMode, build_plan
from musaeus.stages import IngestStage


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("MUSAEUS_VAULT_ROOT", str(tmp_path / "vault"))
    monkeypatch.setenv("MUSAEUS_VERBOSE", "0")
    monkeypatch.setenv("MUSAEUS_PROGRESS", "0")
    monkeypatch.setattr("musaeus.setup.needs_setup", lambda: False)
    return tmp_path


def _main(monkeypatch, capsys, *argv):
    monkeypatch.setattr(sys, "argv", ["musaeus", *argv])
    with pytest.raises(SystemExit) as stop:
        cli.main()
    return int(stop.value.code or 0), capsys.readouterr()


def test_a_forced_preview_is_refused(env, monkeypatch, capsys):
    """5: `forge --force --dry-run` previewed the unforced run (0 files)."""
    code, out = _main(monkeypatch, capsys, "forge", "--force", "--dry-run")
    assert code == 2, out.out
    assert "--force" in out.err


@pytest.mark.parametrize(
    ("argv", "flag"),
    [
        (("bitrot", "--rebaseline"), "--rebaseline"),
        (("bitrot", "--backfill-pcm"), "--backfill-pcm"),
        (("genre-validate", "--consolidate"), "--consolidate"),
    ],
)
def test_a_preview_of_a_different_job_is_refused(env, monkeypatch, capsys, argv, flag):
    """Grey's code review, 2026-10-11, finding 6: each of these previewed the
    plain check, while the real run rewrites the baseline, records new
    fingerprints, or re-genres whole artists."""
    code, out = _main(monkeypatch, capsys, *argv, "--dry-run")
    assert code == 2, out.out
    assert flag in out.err


def test_a_reset_preview_keeps_the_resume_marker(env, monkeypatch, capsys, tmp_path):
    """Pre-merge review of #183: `run --reset --dry-run` deleted the resume
    marker in a run that said nothing was changed."""
    marker = tmp_path / "resume_state.json"
    marker.write_text('{"completed": ["preflight"]}', encoding="utf-8")
    monkeypatch.setattr(cli, "_RESUME_FILE", marker)

    _main(monkeypatch, capsys, "run", "--reset", "--dry-run")

    assert marker.exists(), "a preview deleted the resume marker"


def test_a_home_relative_vault_is_expanded(env, monkeypatch, tmp_path):
    """8: "~/vault" from settings.env became a folder named "~"."""
    monkeypatch.setenv("MUSAEUS_VAULT_ROOT", "~/vault")
    assert MusicConfig.from_env().vault_root == (tmp_path / "vault").resolve()


def test_with_no_database_ingest_still_counts_the_inbox(tmp_path):
    """9: no database read as 0 for Ingest while INBOX held files."""
    cfg = MusicConfig(
        vault_root=tmp_path,
        inbox=tmp_path / "INBOX",
        staging=tmp_path / "STAGING",
        quarantine=tmp_path / "QUARANTINE",
        runs_root=tmp_path / "RUNS",
        meta_dir=tmp_path / "MetaData",
        alac_library=tmp_path / "Libraries" / "ALAC_Library",
        db_path=tmp_path / "musaeus.db",
    )
    cfg.inbox.mkdir()
    for n in range(3):
        (cfg.inbox / f"{n}.flac").write_bytes(b"x")
    plan = build_plan(cfg, [IngestStage], RunMode.PREVIEW)
    assert plan.stages[0].candidates == 3
    assert not cfg.db_path.exists(), "the preview created a database"


def test_the_documented_safety_gate_flag_is_accepted(env, monkeypatch, capsys):
    """10: argparse rejected --safety-gate as unrecognized."""
    monkeypatch.setattr(cli, "_cmd_status", lambda: 0)
    code, out = _main(monkeypatch, capsys, "--safety-gate", "status")
    assert "unrecognized arguments" not in out.err
    assert code == 0
