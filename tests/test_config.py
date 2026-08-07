"""Focused regression tests for lazy, isolated MUSAEUS configuration resolution."""

from __future__ import annotations

import builtins
import importlib.util
import os
import sys
import uuid
from pathlib import Path

import pytest

from tests.disposable_vault import DisposableVault

_CONFIG_PATH_KEYS = (
    "MUSAEUS_VAULT_ROOT",
    "MUSAEUS_DB_PATH",
    "MUSAEUS_INBOX",
    "MUSAEUS_STAGING",
    "MUSAEUS_QUARANTINE",
    "MUSAEUS_RUNS_ROOT",
    "MUSAEUS_META_DIR",
)
_PROVIDER_KEYS = (
    "GROQ_API_KEY",
    "LASTFM_API_KEY",
    "OPENROUTER_API_KEY",
    "ACOUSTICID_API_KEY",
)


def _write_env_file(path: Path, values: dict[str, Path | str]) -> None:
    path.write_text(
        "".join(f"{name}={value}\n" for name, value in values.items()), encoding="utf-8"
    )


def _write_settings(config_home: Path, vault_root: Path) -> None:
    config_dir = config_home / "musaeus"
    config_dir.mkdir(parents=True)
    _write_env_file(config_dir / "settings.env", {"MUSAEUS_VAULT_ROOT": vault_root})
    (config_dir / "credentials.env").write_text("", encoding="utf-8")


def _clear_config_process_values(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in (*_CONFIG_PATH_KEYS, *_PROVIDER_KEYS):
        monkeypatch.delenv(name, raising=False)


def _session_bootstrap_module() -> object:
    """Return pytest's already-loaded session bootstrap without importing it again."""
    expected_path = Path(__file__).with_name("conftest.py").absolute()
    for module in tuple(sys.modules.values()):
        module_path = getattr(module, "__file__", None)
        if module_path and Path(module_path).absolute() == expected_path:
            return module
    raise AssertionError("pytest did not load the MUSAEUS session bootstrap")


def test_config_import_does_not_read_any_environment_file(monkeypatch) -> None:
    """Importing config is inert; file reads begin only at explicit resolution time."""
    original_open = builtins.open
    attempted_reads: list[str] = []

    def tracking_open(file: object, *args: object, **kwargs: object):
        try:
            name = Path(os.fspath(file)).name
        except TypeError:
            name = ""
        if name in {"settings.env", "credentials.env", ".env"}:
            attempted_reads.append(name)
            raise AssertionError(f"configuration file read during import: {name}")
        return original_open(file, *args, **kwargs)

    monkeypatch.setattr(builtins, "open", tracking_open)
    module_path = Path(__file__).parents[1] / "musaeus" / "config.py"
    module_name = f"_isolated_musaeus_config_{uuid.uuid4().hex}"
    spec = importlib.util.spec_from_file_location(module_name, module_path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    try:
        spec.loader.exec_module(module)
    finally:
        sys.modules.pop(module_name, None)

    assert attempted_reads == []


def test_from_env_merges_files_in_documented_order_without_environment_side_effects(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Each source contributes at its precedence layer without mutating process state."""
    from musaeus import config as config_module

    config_home = tmp_path / "config-home"
    config_dir = config_home / "musaeus"
    config_dir.mkdir(parents=True)
    settings_vault = tmp_path / "settings-vault"
    credentials_staging = tmp_path / "credentials-staging"
    credentials_quarantine = tmp_path / "credentials-quarantine"
    project_meta = tmp_path / "project-meta"
    process_staging = tmp_path / "process-staging"
    process_inbox = tmp_path / "process-inbox"
    project_env = tmp_path / "project.env"

    _write_env_file(
        config_dir / "settings.env",
        {
            "MUSAEUS_VAULT_ROOT": settings_vault,
            "MUSAEUS_STAGING": tmp_path / "settings-staging",
            "GROQ_API_KEY": "settings-value",
        },
    )
    _write_env_file(
        config_dir / "credentials.env",
        {
            "MUSAEUS_STAGING": credentials_staging,
            "MUSAEUS_QUARANTINE": credentials_quarantine,
            "GROQ_API_KEY": "credentials-value",
        },
    )
    _write_env_file(
        project_env,
        {
            "MUSAEUS_STAGING": tmp_path / "project-staging",
            "MUSAEUS_META_DIR": project_meta,
            "GROQ_API_KEY": "project-value",
        },
    )

    _clear_config_process_values(monkeypatch)
    monkeypatch.setenv("MUSAEUS_CONFIG_HOME", str(config_home))
    monkeypatch.delenv("MUSAEUS_DISABLE_PROJECT_ENV", raising=False)
    monkeypatch.setattr(config_module, "_project_env_path", lambda: project_env)
    monkeypatch.setenv("MUSAEUS_STAGING", str(process_staging))
    monkeypatch.setenv("MUSAEUS_INBOX", str(process_inbox))
    monkeypatch.setenv("GROQ_API_KEY", "process-value")
    config_module.reset_config_cache()

    resolved = config_module.MusicConfig.from_env()

    assert config_module._config_dir() == config_dir
    assert resolved.vault_root == settings_vault.resolve()
    assert resolved.staging == process_staging.resolve()
    assert resolved.quarantine == credentials_quarantine.resolve()
    assert resolved.meta_dir == project_meta.resolve()
    assert resolved.inbox == process_inbox.resolve()
    assert resolved.groq_api_key == "process-value"
    assert "MUSAEUS_QUARANTINE" not in os.environ
    assert "MUSAEUS_META_DIR" not in os.environ


def test_project_env_toggle_is_deterministic(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The isolated-test project-env opt-out changes only the current resolution."""
    from musaeus import config as config_module

    config_home = tmp_path / "config-home"
    settings_vault = tmp_path / "settings-vault"
    project_vault = tmp_path / "project-vault"
    _write_settings(config_home, settings_vault)
    project_env = tmp_path / "project.env"
    _write_env_file(project_env, {"MUSAEUS_VAULT_ROOT": project_vault})

    _clear_config_process_values(monkeypatch)
    monkeypatch.setenv("MUSAEUS_CONFIG_HOME", str(config_home))
    monkeypatch.setattr(config_module, "_project_env_path", lambda: project_env)

    monkeypatch.setenv("MUSAEUS_DISABLE_PROJECT_ENV", "yes")
    assert config_module.MusicConfig.from_env().vault_root == settings_vault.resolve()

    monkeypatch.delenv("MUSAEUS_DISABLE_PROJECT_ENV", raising=False)
    assert config_module.MusicConfig.from_env().vault_root == project_vault.resolve()

    monkeypatch.setenv("MUSAEUS_DISABLE_PROJECT_ENV", "true")
    assert config_module.MusicConfig.from_env().vault_root == settings_vault.resolve()


def test_get_config_cache_reset_tracks_current_config_home(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Fresh calls follow the current home; the explicit reset changes cache lifetime."""
    from musaeus import config as config_module

    first_home = tmp_path / "first-home"
    second_home = tmp_path / "second-home"
    first_vault = tmp_path / "first-vault"
    second_vault = tmp_path / "second-vault"
    _write_settings(first_home, first_vault)
    _write_settings(second_home, second_vault)

    _clear_config_process_values(monkeypatch)
    monkeypatch.setenv("MUSAEUS_CONFIG_HOME", str(first_home))
    monkeypatch.setenv("MUSAEUS_DISABLE_PROJECT_ENV", "1")
    config_module.reset_config_cache()
    first = config_module.get_config()

    monkeypatch.setenv("MUSAEUS_CONFIG_HOME", str(second_home))
    assert config_module.MusicConfig.from_env().vault_root == second_vault.resolve()
    assert config_module.get_config() is first

    config_module.reset_config_cache()
    assert config_module.get_config().vault_root == second_vault.resolve()


def test_fixture_config_cache_does_not_leak_back_into_session_bootstrap(tmp_path: Path) -> None:
    """Fixture config resolution is discarded before the session environment resumes."""
    from musaeus import config as config_module

    config_module.reset_config_cache()
    session_config = config_module.get_config()
    session_root = Path(os.environ["MUSAEUS_TEST_SESSION_ROOT"])
    assert session_config.vault_root.is_relative_to(session_root)

    fixture = DisposableVault.create(tmp_path)
    with pytest.MonkeyPatch.context() as fixture_patch:
        fixture.install_environment(fixture_patch)
        assert config_module.get_config().vault_root == fixture.vault_root
        # This mirrors the disposable_vault fixture finalizer before monkeypatch
        # restores the session bootstrap environment.
        config_module.reset_config_cache()

    resumed = config_module.get_config()
    assert resumed.vault_root == session_config.vault_root
    assert resumed.db_path == session_config.db_path


def test_pytest_unconfigure_clears_cached_disposable_config_before_cleanup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The lifecycle helper is safe for a long-lived pytest.main() host process."""
    from musaeus import config as config_module

    session_bootstrap = _session_bootstrap_module()
    session_root = Path(os.environ["MUSAEUS_TEST_SESSION_ROOT"])
    config_module.reset_config_cache()
    cached = config_module.get_config()
    assert cached.vault_root.is_relative_to(session_root)

    calls: list[str] = []

    def clear_cache() -> None:
        assert session_root.exists()
        config_module.reset_config_cache()
        calls.append("clear")

    def restore_globals() -> None:
        assert config_module._cached_config is None
        calls.append("restore")

    monkeypatch.setattr(session_bootstrap, "_clear_loaded_musaeus_config_cache", clear_cache)
    monkeypatch.setattr(session_bootstrap, "_restore_process_globals", restore_globals)
    monkeypatch.setattr(session_bootstrap, "_SESSION_GLOBALS_RESTORED", False)

    session_bootstrap.pytest_unconfigure(object())
    session_bootstrap.pytest_unconfigure(object())

    assert config_module._cached_config is None
    assert session_root.exists()
    assert calls == ["clear", "restore"]


def test_session_bootstrap_directs_get_config_to_temporary_space() -> None:
    """Collection bootstrap supplies only disposable config before test modules import it."""
    from musaeus import config as config_module

    config_module.reset_config_cache()
    resolved = config_module.get_config()
    session_root = Path(os.environ["MUSAEUS_TEST_SESSION_ROOT"])

    assert os.environ["MUSAEUS_DISABLE_PROJECT_ENV"] == "1"
    assert config_module._config_dir().is_relative_to(session_root)
    assert resolved.vault_root.is_relative_to(session_root)
    assert resolved.db_path.is_relative_to(session_root)
    assert resolved.meta_dir.is_relative_to(session_root)
