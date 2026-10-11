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
