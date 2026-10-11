"""Review of slice E (#144), findings 6 and 7: `musaeus setup` printed every
saved API key in full (the AcoustID key included), and credentials.env was
written in place with mode 644."""

from __future__ import annotations

import os
import stat

import pytest

from musaeus.setup import wizard

SECRET = "FAKEKEY-abcdef-SECRET-TAIL"


@pytest.fixture
def config_dir(tmp_path, monkeypatch):
    d = tmp_path / "config"
    d.mkdir()
    monkeypatch.setattr(wizard, "_CONFIG_DIR", d)
    monkeypatch.setattr(wizard, "_SETTINGS_FILE", d / "settings.env")
    monkeypatch.setattr(wizard, "_CREDENTIALS_FILE", d / "credentials.env")
    (d / "credentials.env").write_text(f"ACOUSTICID_API_KEY={SECRET}\n")
    os.chmod(d / "credentials.env", 0o644)
    return d


def _run(tmp_path, monkeypatch):
    vault = tmp_path / "vault"
    answers = {"Vault root path": str(vault), "Inbox path": str(vault / "INBOX")}
    monkeypatch.setattr(wizard, "_ask", lambda prompt, default="": answers.get(prompt, default))
    monkeypatch.setattr(wizard, "_confirm", lambda *a, **k: True, raising=False)
    monkeypatch.setattr(wizard, "_read_secret", lambda prompt: "", raising=False)  # Enter: keep
    wizard.run_wizard(force=True)


def test_the_wizard_never_shows_a_saved_key(config_dir, tmp_path, monkeypatch, capsys):
    _run(tmp_path, monkeypatch)
    out = capsys.readouterr().out
    assert SECRET not in out and SECRET[:8] not in out, "a saved key was printed"
    assert f"ACOUSTICID_API_KEY={SECRET}" in (config_dir / "credentials.env").read_text()


def test_the_wizard_saves_credentials_readable_by_grey_alone(config_dir, tmp_path, monkeypatch):
    _run(tmp_path, monkeypatch)
    assert stat.S_IMODE((config_dir / "credentials.env").stat().st_mode) == 0o600


class _DiskFull:
    """A value whose writing fails part way through the file."""

    def __format__(self, spec):
        raise OSError(28, "No space left on device")


def test_a_save_cut_short_leaves_the_old_file_whole(config_dir):
    creds = config_dir / "credentials.env"
    with pytest.raises(OSError):
        wizard._save_env(creds, {"A_KEY": "new", "B_KEY": _DiskFull()})  # type: ignore[dict-item]
    assert creds.read_text() == f"ACOUSTICID_API_KEY={SECRET}\n", "a failed save cut the file"
    assert sorted(p.name for p in config_dir.iterdir()) == ["credentials.env"]


# ── Pre-merge review of #157 ──────────────────────────────────────────────────


def test_a_byte_order_mark_does_not_lose_the_key(config_dir, tmp_path, monkeypatch):
    creds = config_dir / "credentials.env"
    creds.write_bytes(f"﻿ACOUSTICID_API_KEY={SECRET}\nZZ_OTHER=x\n".encode())
    _run(tmp_path, monkeypatch)
    text = creds.read_text(encoding="utf-8")
    assert "﻿" not in text
    assert f"ACOUSTICID_API_KEY={SECRET}" in text.splitlines(), "the key was lost"


def test_a_key_typed_in_is_saved_and_not_shown(config_dir, tmp_path, monkeypatch, capsys):
    vault = tmp_path / "vault"
    answers = {"Vault root path": str(vault), "Inbox path": str(vault / "INBOX")}
    monkeypatch.setattr(wizard, "_ask", lambda prompt, default="": answers.get(prompt, default))
    monkeypatch.setattr(wizard, "_confirm", lambda *a, **k: True)
    typed = "TYPED-NEW-KEY-123"
    monkeypatch.setattr(
        wizard, "_read_secret", lambda prompt: typed if "ACOUSTICID" in prompt else ""
    )
    wizard.run_wizard(force=True)
    assert typed not in capsys.readouterr().out
    assert f"ACOUSTICID_API_KEY={typed}" in (config_dir / "credentials.env").read_text()


def test_the_key_manager_saves_private(config_dir, monkeypatch, capsys):
    for key in wizard.MANAGED_KEYS:
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setattr(wizard, "_confirm", lambda *a, **k: True)
    monkeypatch.setattr(wizard, "_read_secret", lambda prompt: "MANAGER-NEW-KEY")
    wizard.run_api_key_manager()
    creds = config_dir / "credentials.env"
    assert stat.S_IMODE(creds.stat().st_mode) == 0o600
    assert "MANAGER-NEW-KEY" not in capsys.readouterr().out


def test_a_symlinked_file_stays_a_symlink(config_dir, tmp_path):
    real = tmp_path / "elsewhere" / "credentials.env"
    real.parent.mkdir()
    real.write_text("ACOUSTICID_API_KEY=old\n")
    link = config_dir / "credentials.env"
    link.unlink()
    link.symlink_to(real)
    wizard._save_env(link, {"ACOUSTICID_API_KEY": "new"}, private=True)
    assert link.is_symlink()
    assert real.read_text() == "ACOUSTICID_API_KEY=new\n"


def test_a_settings_file_keeps_its_mode(config_dir):
    settings = config_dir / "settings.env"
    settings.write_text("MUSAEUS_VAULT_ROOT=/v\n")
    os.chmod(settings, 0o600)
    wizard._save_env(settings, {"MUSAEUS_VAULT_ROOT": "/w"})
    assert stat.S_IMODE(settings.stat().st_mode) == 0o600
