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
  MUSAEUS_ALAC_LIBRARY — canonical finalized library (default: VAULT_ROOT/ALAC-Library)

ALAC-Library is the canonical, finalized output of the pipeline — distinct
from INBOX (mutable working area). Physical presence of a file in
ALAC-Library is meant to be a trustworthy, DB-independent signal that
processing is actually complete for that file, not just that a DB row
claims so. musaeus.db itself is treated as transient per-batch working
state and is expected to be wiped after each batch finalizes; anything
that must survive across batches (the audio-hash index used for
cross-batch dedup, the TuneMyMusic.csv sub-lossless log, DB snapshots)
lives under ALAC-Library itself, not in the wipeable vault DB.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

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

    # Canonical finalized library — see module docstring
    alac_library: Path

    # Database
    db_path: Path

    # Curator export target (exports.curator.root). None means "not
    # configured", and Curator refuses rather than inventing a path.
    #
    # This field did not exist. CuratorStage._get_export_root has always
    # ended in `getattr(ctx.config, "car_export_root", None)`, so its
    # "fall back to config" branch could never return anything: the
    # attribute was never declared here, and getattr's default hid that
    # completely. Anyone who set a configuration value watched it be
    # ignored in silence, and --export-root was in practice mandatory on
    # every invocation.
    curator_export_root: Path | None = None

    # API keys (may be None if not configured)
    groq_api_key: str | None = field(default=None, repr=False)
    lastfm_api_key: str | None = field(default=None, repr=False)
    openrouter_api_key: str | None = field(default=None, repr=False)
    acousticid_api_key: str | None = field(default=None, repr=False)

    @classmethod
    def from_env(cls) -> MusicConfig:
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
        alac_library = _p("MUSAEUS_ALAC_LIBRARY", vault_root / "ALAC-Library")

        curator_export_root_raw = os.environ.get("MUSAEUS_CURATOR_EXPORT_ROOT", "")

        return cls(
            vault_root=vault_root,
            inbox=inbox,
            staging=staging,
            quarantine=quarantine,
            runs_root=runs_root,
            meta_dir=meta_dir,
            alac_library=alac_library,
            db_path=db_path,
            curator_export_root=(
                Path(curator_export_root_raw).expanduser() if curator_export_root_raw else None
            ),
            groq_api_key=os.environ.get("GROQ_API_KEY") or None,
            lastfm_api_key=os.environ.get("LASTFM_API_KEY") or None,
            openrouter_api_key=os.environ.get("OPENROUTER_API_KEY") or None,
            acousticid_api_key=os.environ.get("ACOUSTICID_API_KEY") or None,
        )

    # ── ALAC-Library derived paths ───────────────────────────────────────────
    # Everything here lives under alac_library itself (not the vault DB) so
    # it survives a DB wipe between batches.

    @property
    def dupes_review_dir(self) -> Path:
        """Losing duplicates land here, never deleted. ORPHEUS
        LESSER_DUPES_MOVED_FOR_REVIEW convention."""
        return self.alac_library / "DUPES_MOVED_FOR_REVIEW"

    @property
    def tribute_review_dir(self) -> Path:
        """Quarantined tribute/karaoke/meditation tracks land here, never
        deleted -- same reversible convention as dupes_review_dir. Reuses
        the exact folder name the 2026-08-12 one-off tribute-removal
        script already used (TRIBUTE_REMOVED_FOR_REVIEW), for consistency
        with that precedent rather than introducing a second name for the
        same concept."""
        return self.alac_library / "TRIBUTE_REMOVED_FOR_REVIEW"

    @property
    def hash_index_path(self) -> Path:
        """Persistent audio-hash index of everything already finalized into
        ALAC-Library, used for cross-batch dedup once musaeus.db has been
        wiped. A plain SQLite file, separate from the transient vault DB.

        Lives OUTSIDE alac_library as of 2026-08-21 -- see db_history_dir
        for why."""
        return self.db_history_dir / "hash_index.db"

    @property
    def db_history_dir(self) -> Path:
        """Where a musaeus.db snapshot is copied before it's wiped at the
        end of a completed batch, and where the hash ledger lives.

        Moved out of ALAC-Library/_history/ on 2026-08-21 after a
        third-party duplicate-finder (PerfectTunes), pointed at the music
        library, emptied it: the snapshots are ~464 MB near-identical
        SQLite files, which is exactly what such a tool is built to find
        and delete. It took the hash ledger with them, and the next audit
        failed on 18,405 rows.

        Nothing was lost that time -- the ledger rebuilds from the archive
        table -- but a directory of backups sitting inside the directory
        being scanned is a standing invitation. Anything that is not audio
        now lives beside the library rather than within it."""
        return self.vault_root / "_db_backups"

    @property
    def tunemymusic_csv_path(self) -> Path:
        """Cross-platform track-list CSV for sub-lossless sources that
        Canonicalize transcoded rather than archived losslessly. Appended
        across batches, lives at the top of ALAC-Library so it survives a
        DB wipe."""
        return self.alac_library / "TuneMyMusic.csv"

    def ensure_dirs(self) -> None:
        """Create all required directories if they don't exist."""
        for d in (
            self.vault_root,
            self.inbox,
            self.staging,
            self.quarantine,
            self.runs_root,
            self.meta_dir,
            self.alac_library,
            self.dupes_review_dir,
            self.tribute_review_dir,
            self.db_history_dir,
            self.db_path.parent,
        ):
            d.mkdir(parents=True, exist_ok=True)

    def describe(self) -> str:
        """Human-readable summary for console display."""
        lines = [
            "  MUSAEUS Configuration",
            f"  Vault      : {self.vault_root}",
            f"  Inbox      : {self.inbox}",
            f"  Staging    : {self.staging}",
            f"  Quarantine : {self.quarantine}",
            f"  Runs       : {self.runs_root}",
            f"  MetaData   : {self.meta_dir}",
            f"  ALAC-Library: {self.alac_library}",
            f"  DB         : {self.db_path}",
            f"  Groq key   : {'✓ set' if self.groq_api_key else '✗ not set'}",
            f"  Last.fm    : {'✓ set' if self.lastfm_api_key else '✗ not set'}",
            f"  AcousticID : {'✓ set' if self.acousticid_api_key else '✗ not set'}",
        ]
        return "\n".join(lines)


# Convenience: load once at import time for scripts that just want paths
_cached_config: MusicConfig | None = None


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
LOSSLESS_EXTENSIONS: frozenset[str] = frozenset({".flac", ".alac", ".wav", ".aiff", ".aif"})
LOSSY_EXTENSIONS: frozenset[str] = frozenset({".mp3", ".aac", ".m4a", ".ogg"})

# Real, codec-based (not extension-based) lossless check -- the sets above
# are known-broken for this purpose: .m4a can hold either ALAC (lossless)
# or AAC (lossy), so an extension-only check misclassifies the entire
# ALAC-in-.m4a case. Use this against archive.codec (Scholar's real
# ffprobe codec_name) wherever "is this file actually lossless" matters,
# e.g. picking a keeper between duplicate candidates of different codecs.
LOSSLESS_CODECS: frozenset[str] = frozenset({"flac", "alac", "pcm_s16le", "pcm_s24le", "pcm_s32le"})
