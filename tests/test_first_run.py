"""The first run as a new user (wrap-up check of 2026-11-11, run in a clean
home folder): `~/Music/vault` typed into the wizard made a folder named "~"
under the current directory, and a setup nobody answered ended with exit
code 0, so a script took "nothing ran" for success."""

from __future__ import annotations

import sys

import pytest

from musaeus import cli
from musaeus.setup import wizard


@pytest.fixture
def config_dir(tmp_path, monkeypatch):
    d = tmp_path / "config"
    monkeypatch.setattr(wizard, "_CONFIG_DIR", d)
    monkeypatch.setattr(wizard, "_SETTINGS_FILE", d / "settings.env")
    monkeypatch.setattr(wizard, "_CREDENTIALS_FILE", d / "credentials.env")
    monkeypatch.setattr(wizard, "_read_secret", lambda prompt: "")
    return d


def _answer(monkeypatch, vault: str) -> None:
    answers = {"Vault root path": vault}
    monkeypatch.setattr(wizard, "_ask", lambda prompt, default="": answers.get(prompt, default))


def test_a_tilde_in_the_vault_path_is_the_home_folder(config_dir, tmp_path, monkeypatch):
    home, cwd = tmp_path / "home", tmp_path / "cwd"
    home.mkdir()
    cwd.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.chdir(cwd)
    _answer(monkeypatch, "~/Music/vault")

    assert wizard.run_wizard(force=True)

    assert (home / "Music" / "vault" / "INBOX").is_dir()
    assert not (cwd / "~").exists()
    saved = (config_dir / "settings.env").read_text(encoding="utf-8")
    assert f"MUSAEUS_VAULT_ROOT={(home / 'Music' / 'vault').resolve()}" in saved


def test_a_relative_vault_path_is_refused(config_dir, tmp_path, monkeypatch, capsys):
    """The check was there, but after resolve(), which makes every path absolute."""
    monkeypatch.chdir(tmp_path)
    _answer(monkeypatch, "vault")

    assert not wizard.run_wizard(force=True)

    assert "absolute path" in capsys.readouterr().out
    assert not (tmp_path / "vault").exists()


@pytest.mark.parametrize("argv", [("setup",), ("run", "--dry-run")])
def test_an_abandoned_setup_is_not_a_success(tmp_path, monkeypatch, argv):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.delenv("MUSAEUS_VAULT_ROOT", raising=False)
    monkeypatch.setattr(sys, "argv", ["musaeus", *argv])
    monkeypatch.setattr("musaeus.setup.needs_setup", lambda: True)
    monkeypatch.setattr("musaeus.setup.run_wizard", lambda force=False: False)

    with pytest.raises(SystemExit) as stop:
        cli.main()

    assert stop.value.code == 1


@pytest.mark.parametrize("a_real_package", [False, True], ids=["folder", "package"])
def test_a_helper_command_runs_from_another_folder(tmp_path, monkeypatch, capsys, a_real_package):
    """`musaeus report` crashed outside the checkout: "No module named
    'scripts'", or another folder of that name (~/scripts) was found first.
    So did upgrade-check, spec-scout and canon-review.

    A package of that name (one with an __init__.py) wins over the folder
    beside musaeus wherever it sits on the path (local CodeRabbit, 2026-11-12)."""
    from pathlib import Path

    elsewhere = tmp_path / "elsewhere"
    (elsewhere / "scripts").mkdir(parents=True)
    if a_real_package:
        (elsewhere / "scripts" / "__init__.py").write_text("", encoding="utf-8")
    (tmp_path / "vault").mkdir()
    monkeypatch.chdir(elsewhere)
    root = Path(cli.__file__).resolve().parent.parent
    monkeypatch.setattr(
        sys, "path", ["", *(p for p in sys.path if p and Path(p).resolve() != root)]
    )
    for name in [m for m in sys.modules if m.split(".")[0] == "scripts"]:
        monkeypatch.delitem(sys.modules, name)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.setenv("MUSAEUS_VAULT_ROOT", str(tmp_path / "vault"))
    monkeypatch.setattr("musaeus.config._cached_config", None)
    monkeypatch.setattr("musaeus.setup.needs_setup", lambda: False)
    monkeypatch.setattr(sys, "argv", ["musaeus", "report"])

    with pytest.raises(SystemExit) as stop:
        cli.main()

    assert stop.value.code == 0, capsys.readouterr().err
