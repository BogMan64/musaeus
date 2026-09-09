"""Exporting MUSAEUS_VAULT_ROOT must be enough to configure MUSAEUS.

``needs_setup()`` used to test only for ``~/.config/musaeus/settings.env``. It
never consulted ``os.environ``, so a configured environment still dropped every
command into the interactive wizard — which then aborts, because in a
container, a cron job or a systemd unit nothing can answer it.

``MusicConfig.from_env()`` already treats the variable as sufficient. The two
disagreeing about the same question is what made the Docker image seed the file
as a workaround, leaving the image and a bare ``pip install`` diverging.

Found 2026-09-05 by running the container, not by reading the code — which is
why these tests drive the real function with a real environment rather than
asserting on its source.
"""

from __future__ import annotations

import pytest

from musaeus.setup import wizard
from musaeus.setup.wizard import needs_setup


@pytest.fixture(autouse=True)
def _no_settings_file(tmp_path, monkeypatch):
    """Point the settings file somewhere that does not exist.

    Without this the developer's own ~/.config/musaeus/settings.env answers
    every case and all four tests pass over nothing.
    """
    monkeypatch.setattr(wizard, "_SETTINGS_FILE", tmp_path / "absent" / "settings.env")


def test_the_environment_alone_is_enough(monkeypatch):
    """The container / cron / systemd case: configured, no settings file."""
    monkeypatch.setenv("MUSAEUS_VAULT_ROOT", "/tmp/fixture-vault")

    assert needs_setup() is False


def test_no_environment_and_no_file_is_a_genuine_first_run(monkeypatch):
    monkeypatch.delenv("MUSAEUS_VAULT_ROOT", raising=False)

    assert needs_setup() is True


def test_an_empty_value_does_not_count_as_configured(monkeypatch):
    """`MUSAEUS_VAULT_ROOT=` is not an answer, and must not be mistaken for one.

    The env-var check has to be truthiness on the *value*, not presence of the
    key — the same distinction that made MUSAEUS_FORCE_REENCODE=0 mean "on".
    """
    monkeypatch.setenv("MUSAEUS_VAULT_ROOT", "")

    assert needs_setup() is True


def test_a_settings_file_still_works_without_the_variable(tmp_path, monkeypatch):
    """The pre-existing path must not regress: the file is still an answer."""
    monkeypatch.delenv("MUSAEUS_VAULT_ROOT", raising=False)
    settings = tmp_path / "settings.env"
    settings.write_text("MUSAEUS_VAULT_ROOT=/tmp/from-file\n", encoding="utf-8")
    monkeypatch.setattr(wizard, "_SETTINGS_FILE", settings)

    assert needs_setup() is False


def test_a_settings_file_without_the_key_is_not_an_answer(tmp_path, monkeypatch):
    monkeypatch.delenv("MUSAEUS_VAULT_ROOT", raising=False)
    settings = tmp_path / "settings.env"
    settings.write_text("SOMETHING_ELSE=1\n", encoding="utf-8")
    monkeypatch.setattr(wizard, "_SETTINGS_FILE", settings)

    assert needs_setup() is True
