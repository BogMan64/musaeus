"""Session-wide hermetic bootstrap for MUSAEUS tests.

Pytest imports this module before application test modules. Keep this bootstrap
stdlib-only so importing MUSAEUS cannot see a real home directory, configuration
file, project-local ``.env``, provider credential, or temporary directory.
"""

from __future__ import annotations

import atexit
import importlib
import os
import shutil
import stat
import sys
import tempfile
from pathlib import Path
from typing import Final

_SENSITIVE_ENVIRONMENT: Final = (
    "GROQ_API_KEY",
    "LASTFM_API_KEY",
    "OPENROUTER_API_KEY",
    "ACOUSTICID_API_KEY",
    "MUSICBRAINZ_API_KEY",
    "DISCOGS_API_KEY",
    "SPOTIFY_CLIENT_ID",
    "SPOTIFY_CLIENT_SECRET",
)
_SESSION_ENVIRONMENT: Final = (
    "HOME",
    "XDG_CONFIG_HOME",
    "XDG_CACHE_HOME",
    "XDG_DATA_HOME",
    "XDG_STATE_HOME",
    "TMPDIR",
    "TMP",
    "TEMP",
    "PYTHONDONTWRITEBYTECODE",
)
_ORIGINAL_SESSION_ENVIRONMENT = {
    name: os.environ.get(name) for name in (*_SESSION_ENVIRONMENT, *_SENSITIVE_ENVIRONMENT)
}
_ORIGINAL_MUSAEUS_ENVIRONMENT = {
    name: value for name, value in os.environ.items() if name.startswith("MUSAEUS_")
}
_ORIGINAL_TEMP_DIR = tempfile.tempdir
_ORIGINAL_DONT_WRITE_BYTECODE = sys.dont_write_bytecode
_SESSION_ROOT = Path(tempfile.mkdtemp(prefix="musaeus-pytest-", dir="/tmp"))
_SESSION_SENTINEL = _SESSION_ROOT / ".musaeus-pytest-session"
_SESSION_GLOBALS_RESTORED = False


def _write_safe_configuration(config_home: Path, vault_root: Path, state_root: Path) -> None:
    """Create only empty disposable configuration files for test collection."""
    config_dir = config_home / "musaeus"
    paths = {
        "MUSAEUS_VAULT_ROOT": vault_root,
        "MUSAEUS_DB_PATH": state_root / "musaeus.db",
        "MUSAEUS_INBOX": vault_root / "INBOX",
        "MUSAEUS_STAGING": vault_root / "STAGING",
        "MUSAEUS_QUARANTINE": vault_root / "QUARANTINE",
        "MUSAEUS_RUNS_ROOT": vault_root / "RUNS",
        "MUSAEUS_META_DIR": vault_root / "MetaData",
    }
    config_dir.mkdir(parents=True, exist_ok=True)
    settings = "".join(f"{name}={value}\n" for name, value in paths.items())
    (config_dir / "settings.env").write_text(settings, encoding="utf-8")
    (config_dir / "credentials.env").write_text("", encoding="utf-8")


def _bootstrap_session_environment() -> None:
    """Install isolated paths and remove provider values before test imports."""
    paths = {
        "home": _SESSION_ROOT / "home",
        "xdg_config": _SESSION_ROOT / "xdg-config",
        "xdg_cache": _SESSION_ROOT / "xdg-cache",
        "xdg_data": _SESSION_ROOT / "xdg-data",
        "xdg_state": _SESSION_ROOT / "xdg-state",
        "tmp": _SESSION_ROOT / "tmp",
        "vault": _SESSION_ROOT / "vault",
        "state": _SESSION_ROOT / "state",
        "recovery": _SESSION_ROOT / "recovery",
        "reports": _SESSION_ROOT / "reports",
    }
    for path in paths.values():
        path.mkdir(parents=True, exist_ok=True)
    for path in (
        paths["vault"] / "INBOX",
        paths["vault"] / "STAGING",
        paths["vault"] / "QUARANTINE",
        paths["vault"] / "RUNS",
        paths["vault"] / "MetaData",
    ):
        path.mkdir(parents=True, exist_ok=True)

    _SESSION_SENTINEL.write_text("disposable MUSAEUS pytest session\n", encoding="utf-8")
    _write_safe_configuration(paths["xdg_config"], paths["vault"], paths["state"])

    for name in tuple(os.environ):
        if name.startswith("MUSAEUS_"):
            os.environ.pop(name, None)
    for name in _SENSITIVE_ENVIRONMENT:
        os.environ.pop(name, None)

    os.environ.update(
        {
            "HOME": str(paths["home"]),
            "XDG_CONFIG_HOME": str(paths["xdg_config"]),
            "XDG_CACHE_HOME": str(paths["xdg_cache"]),
            "XDG_DATA_HOME": str(paths["xdg_data"]),
            "XDG_STATE_HOME": str(paths["xdg_state"]),
            "TMPDIR": str(paths["tmp"]),
            "TMP": str(paths["tmp"]),
            "TEMP": str(paths["tmp"]),
            "PYTHONDONTWRITEBYTECODE": "1",
            "MUSAEUS_VAULT_ROOT": str(paths["vault"]),
            "MUSAEUS_DB_PATH": str(paths["state"] / "musaeus.db"),
            "MUSAEUS_INBOX": str(paths["vault"] / "INBOX"),
            "MUSAEUS_STAGING": str(paths["vault"] / "STAGING"),
            "MUSAEUS_QUARANTINE": str(paths["vault"] / "QUARANTINE"),
            "MUSAEUS_RUNS_ROOT": str(paths["vault"] / "RUNS"),
            "MUSAEUS_META_DIR": str(paths["vault"] / "MetaData"),
            "MUSAEUS_RECOVERY_ROOT": str(paths["recovery"]),
            "MUSAEUS_REPORTS_ROOT": str(paths["reports"]),
            "MUSAEUS_CONFIG_HOME": str(paths["xdg_config"]),
            "MUSAEUS_DISABLE_PROJECT_ENV": "1",
            "MUSAEUS_TEST_SESSION_ROOT": str(_SESSION_ROOT),
        }
    )
    tempfile.tempdir = str(paths["tmp"])
    sys.dont_write_bytecode = True


def _clear_loaded_musaeus_config_cache() -> None:
    """Clear a loaded config cache without importing MUSAEUS during teardown."""
    config_module = sys.modules.get("musaeus.config")
    if config_module is None:
        return
    reset_cache = getattr(config_module, "reset_config_cache", None)
    if callable(reset_cache):
        reset_cache()
    else:
        # Retain compatibility with a partially imported legacy config module.
        config_module._cached_config = None


def _session_root_is_owned() -> bool:
    """Return whether the fixed session root still has our expected sentinel."""
    if (
        _SESSION_ROOT.parent != Path("/tmp")
        or not _SESSION_ROOT.name.startswith("musaeus-pytest-")
        or _SESSION_SENTINEL.parent != _SESSION_ROOT
    ):
        return False
    try:
        return stat.S_ISDIR(_SESSION_ROOT.lstat().st_mode) and stat.S_ISREG(
            _SESSION_SENTINEL.lstat().st_mode
        )
    except OSError:
        return False


def _cleanup_session_root() -> None:
    """Remove only the sentinel-owned session root after pytest temp cleanup."""
    if _session_root_is_owned():
        shutil.rmtree(_SESSION_ROOT)


def _restore_process_globals() -> None:
    """Restore the caller process environment and Python temporary-directory state."""
    for name in tuple(os.environ):
        if name.startswith("MUSAEUS_"):
            os.environ.pop(name, None)
    for name, value in _ORIGINAL_MUSAEUS_ENVIRONMENT.items():
        os.environ[name] = value
    for name, value in _ORIGINAL_SESSION_ENVIRONMENT.items():
        if value is None:
            os.environ.pop(name, None)
        else:
            os.environ[name] = value
    tempfile.tempdir = _ORIGINAL_TEMP_DIR
    sys.dont_write_bytecode = _ORIGINAL_DONT_WRITE_BYTECODE


def _restore_session_process_state() -> None:
    """Idempotently clear disposable config state from a long-lived pytest host."""
    global _SESSION_GLOBALS_RESTORED
    if _SESSION_GLOBALS_RESTORED:
        return

    # The cache can hold paths below _SESSION_ROOT, so reset it while the
    # disposable tree and environment still exist.
    _clear_loaded_musaeus_config_cache()
    _restore_process_globals()
    _SESSION_GLOBALS_RESTORED = True


def _cleanup_after_pytest_exit() -> None:
    """Fallback process restoration, then remove the root after pytest temp cleanup."""
    _restore_session_process_state()
    _cleanup_session_root()


def pytest_unconfigure(config: object) -> None:
    """Restore globals when pytest.main() returns without removing pytest's temp root."""
    del config
    _restore_session_process_state()


_bootstrap_session_environment()
# Normal pytest completion restores globals via pytest_unconfigure. The atexit
# handler is the abnormal-exit fallback for that restoration and deliberately
# defers sentinel cleanup until pytest's own temporary-directory exit cleanup.
atexit.register(_cleanup_after_pytest_exit)

# Import only after the environment above is active. This registers the fixture
# without importing MUSAEUS application modules during conftest collection.
disposable_vault = importlib.import_module("tests.disposable_vault").disposable_vault

__all__ = ["disposable_vault"]
