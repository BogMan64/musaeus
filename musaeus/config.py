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
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional


_USER_CONFIG_DIR = Path.home() / ".config" / "musaeus"
_SETTINGS_FILE = _USER_CONFIG_DIR / "settings.env"
_CREDENTIALS_FILE = _USER_CONFIG_DIR / "credentials.env"


def _parse_env_file(path: Path) -> dict[str, str]:
    """Parse a simple KEY=VALUE env file. Strips quotes. Ignores comments."""
    result: dict[str, str] = {}
    try:
        with open(path) as fh:
            for line in fh:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, val = line.partition("=")
                result[key.strip()] = val.strip().strip("\"'")
    except OSError:
        pass
    return result


def _load_env() -> None:
    """Load config files into os.environ (env vars always take precedence)."""
    for fpath in (_SETTINGS_FILE, _CREDENTIALS_FILE):
        for k, v in _parse_env_file(fpath).items():
            os.environ.setdefault(k, v)
    # Project-local .env (beside musaeus package or wherever MUSAEUS_ROOT points)
    project_env = Path(__file__).resolve().parent.parent / ".env"
    for k, v in _parse_env_file(project_env).items():
        os.environ.setdefault(k, v)


_load_env()


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

    # API keys (may be None if not configured)
    groq_api_key: Optional[str] = field(default=None, repr=False)
    lastfm_api_key: Optional[str] = field(default=None, repr=False)
    openrouter_api_key: Optional[str] = field(default=None, repr=False)
    acousticid_api_key: Optional[str] = field(default=None, repr=False)

    @classmethod
    def from_env(cls) -> "MusicConfig":
        """Build MusicConfig from environment. Raises ValueError if vault_root missing."""
        vault_str = os.environ.get("MUSAEUS_VAULT_ROOT", "")
        if not vault_str:
            raise ValueError(
                "MUSAEUS_VAULT_ROOT is not set.\n"
                "Set it in ~/.config/musaeus/settings.env or export it:\n"
                "  export MUSAEUS_VAULT_ROOT=/path/to/your/vault"
            )
        vault_root = Path(vault_str).resolve()

        def _p(env_key: str, default: Path) -> Path:
            val = os.environ.get(env_key, "")
            return Path(val).resolve() if val else default

        db_path = _p("MUSAEUS_DB_PATH", vault_root / "musaeus.db")
        inbox = _p("MUSAEUS_INBOX", vault_root / "INBOX")
        staging = _p("MUSAEUS_STAGING", vault_root / "STAGING")
        quarantine = _p("MUSAEUS_QUARANTINE", vault_root / "QUARANTINE")
        runs_root = _p("MUSAEUS_RUNS_ROOT", vault_root / "RUNS")
        meta_dir = _p("MUSAEUS_META_DIR", vault_root / "MetaData")

        return cls(
            vault_root=vault_root,
            inbox=inbox,
            staging=staging,
            quarantine=quarantine,
            runs_root=runs_root,
            meta_dir=meta_dir,
            db_path=db_path,
            groq_api_key=os.environ.get("GROQ_API_KEY") or None,
            lastfm_api_key=os.environ.get("LASTFM_API_KEY") or None,
            openrouter_api_key=os.environ.get("OPENROUTER_API_KEY") or None,
            acousticid_api_key=os.environ.get("ACOUSTICID_API_KEY") or None,
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


# Convenience: load once at import time for scripts that just want paths
_cached_config: Optional[MusicConfig] = None


def get_config() -> MusicConfig:
    """Return the singleton MusicConfig (loaded once, cached)."""
    global _cached_config
    if _cached_config is None:
        _cached_config = MusicConfig.from_env()
    return _cached_config


# Audio extensions recognised by Musaeus
AUDIO_EXTENSIONS: frozenset[str] = frozenset(
    {".mp3", ".flac", ".m4a", ".alac", ".aac", ".wav", ".aiff", ".aif", ".ogg"}
)
LOSSLESS_EXTENSIONS: frozenset[str] = frozenset(
    {".flac", ".alac", ".wav", ".aiff", ".aif"}
)
LOSSY_EXTENSIONS: frozenset[str] = frozenset({".mp3", ".aac", ".m4a", ".ogg"})
