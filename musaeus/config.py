#!/usr/bin/env python3
"""
MUSAEUS — Configuration
All paths and API keys resolved from env vars / config files.
Zero hardcoded paths. Move the vault → change one env var.

Loading priority (later wins):
  1. ~/.config/musaeus/settings.env
  2. ~/.config/musaeus/credentials.env
  3. {MUSAEUS_ROOT}/.env
  4. Process environment variables (always highest priority)

Key env vars:
  MUSAEUS_VAULT_ROOT   — root of the music vault (required)
  MUSAEUS_DB_PATH      — override DB location (default: VAULT_ROOT/musaeus.db)
  MUSAEUS_INBOX        — where new files arrive (default: VAULT_ROOT/INBOX)
  MUSAEUS_RUNS_ROOT    — where run logs/reports go (default: VAULT_ROOT/RUNS)
  MUSAEUS_STAGING      — staging area before vault (default: VAULT_ROOT/STAGING)
  MUSAEUS_QUARANTINE   — quarantine for bad files (default: VAULT_ROOT/QUARANTINE)
  MUSAEUS_META_DIR     — canon CSVs location (default: VAULT_ROOT/MetaData)

Isolated-test control:
  MUSAEUS_DISABLE_PROJECT_ENV — when set to a truthy value, skip the project-local
      .env file. This is intended for hermetic test bootstrap only; it never prints
      or exposes configuration values.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

_PROJECT_ENV_DISABLE_VAR = "MUSAEUS_DISABLE_PROJECT_ENV"
_TRUTHY_ENV_VALUES = frozenset({"1", "true", "yes", "on"})


def _config_dir() -> Path:
    """Resolve the MUSAEUS configuration directory when configuration is requested.

    ``MUSAEUS_CONFIG_HOME`` is an optional XDG-style parent directory. When it is
    absent, the historic ``~/.config/musaeus`` location remains the normal default.
    """
    config_home = os.environ.get("MUSAEUS_CONFIG_HOME")
    if config_home:
        return Path(config_home).expanduser() / "musaeus"
    return Path.home() / ".config" / "musaeus"


def _config_files() -> tuple[Path, Path]:
    """Return settings and credentials paths without capturing a home directory."""
    config_dir = _config_dir()
    return config_dir / "settings.env", config_dir / "credentials.env"


def _project_env_path() -> Path:
    """Return the project-local environment file without reading it."""
    return Path(__file__).parent.parent / ".env"


def _project_env_loading_disabled() -> bool:
    """Return whether the narrow isolated-test project-env opt-out is active."""
    value = os.environ.get(_PROJECT_ENV_DISABLE_VAR, "")
    return value.strip().lower() in _TRUTHY_ENV_VALUES


def _parse_env_file(path: Path) -> dict[str, str]:
    """Parse a simple KEY=VALUE env file. Strips quotes. Ignores comments."""
    result: dict[str, str] = {}
    try:
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, val = line.partition("=")
                result[key.strip()] = val.strip().strip("\"'")
    except OSError:
        pass
    return result


def _resolved_environment() -> dict[str, str]:
    """Resolve configuration into a fresh mapping without changing ``os.environ``.

    File values are merged in documented order, then overridden by the actual
    caller process environment. This keeps file-derived values scoped to one
    configuration resolution and prevents one config home from contaminating a
    later resolution in the same process.
    """
    resolved: dict[str, str] = {}
    files = list(_config_files())
    if not _project_env_loading_disabled():
        files.append(_project_env_path())

    for fpath in files:
        resolved.update(_parse_env_file(fpath))
    resolved.update(os.environ)
    return resolved


@dataclass
class MusicConfig:
    """All resolved paths for a Musaeus run. No hardcodes."""

    # Core vault
    vault_root: Path
    inbox: Path
    staging: Path
    quarantine: Path
    runs_root: Path
    meta_dir: Path

    # Database
    db_path: Path

    # AAC-Car-Masked export paths
    aac_car_root: Path
    aac_car_masked_root: Path
    noise_dir: Path
    alac_source_dir: Path | None = field(default=None)

    # API keys (may be None if not configured)
    groq_api_key: str | None = field(default=None, repr=False)
    lastfm_api_key: str | None = field(default=None, repr=False)
    openrouter_api_key: str | None = field(default=None, repr=False)
    acousticid_api_key: str | None = field(default=None, repr=False)

    @classmethod
    def from_env(cls) -> MusicConfig:
        """Build MusicConfig from current config files and process environment."""
        environment = _resolved_environment()
        vault_str = environment.get("MUSAEUS_VAULT_ROOT", "")
        if not vault_str:
            raise ValueError(
                "MUSAEUS_VAULT_ROOT is not set.\n"
                "Set it in ~/.config/musaeus/settings.env or export it:\n"
                "  export MUSAEUS_VAULT_ROOT=/path/to/your/vault"
            )
        vault_root = Path(vault_str).resolve()

        def _p(env_key: str, default: Path) -> Path:
            val = environment.get(env_key, "")
            return Path(val).resolve() if val else default

        db_path = _p("MUSAEUS_DB_PATH", vault_root / "musaeus.db")
        inbox = _p("MUSAEUS_INBOX", vault_root / "INBOX")
        staging = _p("MUSAEUS_STAGING", vault_root / "STAGING")
        quarantine = _p("MUSAEUS_QUARANTINE", vault_root / "QUARANTINE")
        runs_root = _p("MUSAEUS_RUNS_ROOT", vault_root / "RUNS")
        meta_dir = _p("MUSAEUS_META_DIR", vault_root / "MetaData")

        # AAC-Car-Masked export paths
        aac_car_root = _p("MUSAEUS_AAC_CAR_ROOT", runs_root / "AAC-Car")
        aac_car_masked_root = _p("MUSAEUS_AAC_CAR_MASKED_ROOT", runs_root / "AAC-Car-Masked")
        noise_dir = _p("MUSAEUS_NOISE_DIR", runs_root / "Noise")
        alac_source_str = environment.get("MUSAEUS_ALAC_SOURCE_DIR", "")
        alac_source_dir = Path(alac_source_str).resolve() if alac_source_str else None

        return cls(
            vault_root=vault_root,
            inbox=inbox,
            staging=staging,
            quarantine=quarantine,
            runs_root=runs_root,
            meta_dir=meta_dir,
            db_path=db_path,
            aac_car_root=aac_car_root,
            aac_car_masked_root=aac_car_masked_root,
            noise_dir=noise_dir,
            alac_source_dir=alac_source_dir,
            groq_api_key=environment.get("GROQ_API_KEY") or None,
            lastfm_api_key=environment.get("LASTFM_API_KEY") or None,
            openrouter_api_key=environment.get("OPENROUTER_API_KEY") or None,
            acousticid_api_key=environment.get("ACOUSTICID_API_KEY") or None,
        )

    def ensure_dirs(self) -> None:
        """Create all required directories if they don't exist."""
        for d in (
            self.vault_root,
            self.inbox,
            self.staging,
            self.quarantine,
            self.runs_root,
            self.meta_dir,
            self.db_path.parent,
            self.aac_car_root,
            self.aac_car_masked_root,
            self.noise_dir,
        ):
            d.mkdir(parents=True, exist_ok=True)

    def describe(self) -> str:
        """Human-readable summary for console display."""
        lines = [
            "  MUSAEUS Configuration",
            f"  Vault     : {self.vault_root}",
            f"  Inbox     : {self.inbox}",
            f"  Staging   : {self.staging}",
            f"  Quarantine: {self.quarantine}",
            f"  Runs      : {self.runs_root}",
            f"  MetaData  : {self.meta_dir}",
            f"  DB        : {self.db_path}",
            f"  Groq key  : {'✓ set' if self.groq_api_key else '✗ not set'}",
            f"  Last.fm   : {'✓ set' if self.lastfm_api_key else '✗ not set'}",
            f"  AcousticID: {'✓ set' if self.acousticid_api_key else '✗ not set'}",
            f"  Groq      : {'✓ set' if self.groq_api_key else '✗ not set'}",
        ]
        return "\n".join(lines)


# Convenience: cache the first explicitly resolved configuration for scripts that need paths
_cached_config: MusicConfig | None = None


def reset_config_cache() -> None:
    """Forget the cached configuration so the current environment resolves afresh."""
    global _cached_config
    _cached_config = None


def get_config() -> MusicConfig:
    """Return the singleton MusicConfig for the current cache lifetime."""
    global _cached_config
    if _cached_config is None:
        _cached_config = MusicConfig.from_env()
    return _cached_config


# Audio extensions recognised by Musaeus
AUDIO_EXTENSIONS: frozenset[str] = frozenset(
    {".mp3", ".flac", ".m4a", ".alac", ".aac", ".wav", ".aiff", ".aif", ".ogg"}
)
LOSSLESS_EXTENSIONS: frozenset[str] = frozenset({".flac", ".alac", ".wav", ".aiff", ".aif"})
LOSSY_EXTENSIONS: frozenset[str] = frozenset({".mp3", ".aac", ".m4a", ".ogg"})
