"""A run works inside a vault that exists; it never makes one.

`ensure_dirs()` used to create the vault root unconditionally. A mistyped
MUSAEUS_VAULT_ROOT, or a drive that was not mounted yet, therefore became a
brand-new empty vault -- and the run filed music into it. `musaeus setup` is
the one command that makes a vault (Grey, 2026-11-25).

The first test fails on 6cad046 for the right reason: no exception, and the
root exists afterwards. The third fails there because the run returns 0 and
creates the vault.
"""

from __future__ import annotations

import pytest

from musaeus.config import MusicConfig
from musaeus.stages.preflight import PreflightStage


def _config_at(root):
    return MusicConfig(
        vault_root=root,
        inbox=root / "INBOX",
        staging=root / "STAGING",
        quarantine=root / "QUARANTINE",
        runs_root=root / "RUNS",
        meta_dir=root / "MetaData",
        alac_library=root / "ALAC-Library",
        db_path=root / "musaeus.db",
    )


def test_ensure_dirs_refuses_to_make_the_vault(tmp_path):
    root = tmp_path / "typo-not-a-vault"
    cfg = _config_at(root)

    with pytest.raises(RuntimeError) as caught:
        cfg.ensure_dirs(create_root=False)

    assert not root.exists(), "the vault root was created anyway"
    message = str(caught.value)
    assert str(root) in message
    assert "musaeus setup" in message
    assert "MUSAEUS_VAULT_ROOT" in message


def test_setup_can_still_lay_out_a_fresh_vault(tmp_path):
    """The default is unchanged, so the setup wizard and test fixtures work."""
    root = tmp_path / "new-vault"
    cfg = _config_at(root)

    cfg.ensure_dirs()

    assert cfg.inbox.is_dir()
    assert cfg.runs_root.is_dir()


def test_a_real_run_refuses_a_missing_vault(tmp_path, monkeypatch, capsys):
    import musaeus.cli as cli

    root = tmp_path / "unmounted-drive"
    cfg = _config_at(root)
    monkeypatch.setattr(cli, "get_config", lambda: cfg)
    monkeypatch.setattr(cli, "_RESUME_FILE", tmp_path / "resume.json")

    code = cli._run_pipeline_inner([PreflightStage], dry_run=False)

    assert code == 2
    assert not root.exists(), "a refused run created the vault"
    err = capsys.readouterr().err
    assert "the vault root does not exist" in err
    assert "musaeus setup" in err


def test_the_console_refuses_a_missing_vault(tmp_path, monkeypatch):
    from musaeus.console import Console

    root = tmp_path / "unmounted-drive"
    cfg = _config_at(root)
    monkeypatch.setattr("musaeus.console.get_config", lambda: cfg)

    con = Console()
    assert con._boot_check() is False
    assert not root.exists(), "the console created the vault"
