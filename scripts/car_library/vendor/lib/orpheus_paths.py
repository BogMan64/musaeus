#!/usr/bin/env python3
"""
ORPHEUS — Orpheus Paths
Library: lib/orpheus_paths.py — imported by virtually every ORPHEUS script
Purpose: Single source of truth for all project paths (PROJECT_ROOT, RUNS_ROOT, MUSIC_ROOT, DB_PATH, etc.); root can be overridden via ORPHEUS_ROOT env var.
"""

from __future__ import annotations

import os
import re
from datetime import datetime
from pathlib import Path

# ── Load external user settings before computing paths ────────────────────────
# Reads ~/.config/orpheus/settings.env and ~/.config/orpheus/credentials.env
# into os.environ (using setdefault so real env vars always take precedence).
# This runs once at import time so all path constants pick up user config.


def _load_user_config() -> None:
    config_dir = Path.home() / ".config" / "orpheus"
    for fname in ("settings.env", "credentials.env"):
        fpath = config_dir / fname
        if not fpath.exists():
            continue
        try:
            with open(fpath) as _f:
                for _line in _f:
                    _line = _line.strip()
                    if not _line or _line.startswith("#") or "=" not in _line:
                        continue
                    _k, _, _v = _line.partition("=")
                    os.environ.setdefault(_k.strip(), _v.strip().strip("\"'"))
        except OSError:
            pass


_load_user_config()

# ── Root ──────────────────────────────────────────────────────────────────────
# ORPHEUS_ROOT may be set by the user's settings.env or by the caller.
# Fall back to the directory containing this file's parent (the repo root).

_SCRIPT_PARENT = Path(__file__).resolve().parent.parent  # SCRIPTS/lib → SCRIPTS → repo root
DEFAULT_PROJECT_ROOT = _SCRIPT_PARENT.parent if _SCRIPT_PARENT.name == "SCRIPTS" else _SCRIPT_PARENT
PROJECT_ROOT = Path(os.environ.get("ORPHEUS_ROOT", str(DEFAULT_PROJECT_ROOT))).resolve()

# ── Major directories ─────────────────────────────────────────────────────────

SCRIPTS_DIR = PROJECT_ROOT / "SCRIPTS"
METADATA_ROOT = PROJECT_ROOT / "METADATA"
EXPORTS_ROOT = PROJECT_ROOT / "EXPORTS"

# RUNS_ROOT: configurable via ORPHEUS_RUNS_ROOT (set by orpheus_setup.py)
RUNS_ROOT = Path(os.environ.get("ORPHEUS_RUNS_ROOT", str(PROJECT_ROOT / "RUNS"))).resolve()

# CONVERSION_INBOX: configurable via ORPHEUS_INBOX
CONVERSION_INBOX = Path(os.environ.get("ORPHEUS_INBOX", str(PROJECT_ROOT / "CONVERSION_INBOX")))

# Inbox layout
INBOX_ROOT = CONVERSION_INBOX
INBOX_CURRENT = CONVERSION_INBOX / "CURRENT"
INBOX_STREAMING = CONVERSION_INBOX / "STREAMING_IMPORT_TEST_DEEZER"
INBOX_ARCHIVE = CONVERSION_INBOX / "ARCHIVED"
RECHECK_ROOT = PROJECT_ROOT / "INBOX" / "RECHECK"

# Music + processing areas
MUSIC_ROOT = RUNS_ROOT / "Music"
QUARANTINE = RUNS_ROOT / "QUARANTINE"
LUFS_NORM_ROOT = RUNS_ROOT / "LUFS_NORMALIZED"

# Permanent dedupe archive
DEDUPE_ARCHIVE = RUNS_ROOT / "Dedupe" / "ARCHIVED_LOSERS"

# USB3 external drive — dedupe quarantine safe-offload target
# Configurable via ORPHEUS_USB3_DEDUPE; defaults to None if not set
_usb3_dedupe_default = os.environ.get("ORPHEUS_USB3_DEDUPE", "")
USB3_DEDUPE: Path | None = Path(_usb3_dedupe_default) if _usb3_dedupe_default else None

# Car stereo USB mount point — set by orpheus_setup.py or ORPHEUS_CAR_USB env var
_car_usb = os.environ.get("ORPHEUS_CAR_USB", "")
CAR_USB_TARGET: Path | None = Path(_car_usb) if _car_usb else None

# Kodi playlist sync target — set by orpheus_setup.py or ORPHEUS_KODI_PLAYLISTS env var
_kodi_playlists = os.environ.get("ORPHEUS_KODI_PLAYLISTS", "")
KODI_PLAYLIST_DIR: Path | None = Path(_kodi_playlists) if _kodi_playlists else None

# Music source directory — where the user's original collection lives
_music_source = os.environ.get("ORPHEUS_MUSIC_SOURCE", "")
MUSIC_SOURCE: Path | None = Path(_music_source) if _music_source else None

# ── Per-run output structure ──────────────────────────────────────────────────

RUN_STAMP = os.environ.get("ORPHEUS_RUN_STAMP") or datetime.now().strftime("%Y-%m-%d_%H%M%S")

RUN_PARENT = RUNS_ROOT / RUN_STAMP
REPORTS_ROOT = RUN_PARENT / "Reports"
DEDUPE_ROOT = RUN_PARENT / "Dedupe"
REVIEW_ROOT = RUN_PARENT / "Review"
REVIEW_DIR = REVIEW_ROOT
LOGS_ROOT = RUN_PARENT / "Logs"

# Current reports compatibility location
REPORTS_CURRENT = RUNS_ROOT / "Reports" / "CURRENT"

# Forge output — normalized/final tracks within the run stamp
FINAL_ROOT = RUN_PARENT / "Final"

# Car Library sync target (e.g. USB drive inserted at sync time)
# Dynamically resolved by orpheus_car_sync.py at runtime
CAR_LIBRARY_TARGET = None  # resolved dynamically

# Music.Vault — final deliverable output root: configurable via ORPHEUS_VAULT_ROOT
MUSIC_VAULT_ROOT = Path(os.environ.get("ORPHEUS_VAULT_ROOT", str(RUNS_ROOT / "Music.Vault")))
MUSIC_VAULT_ALAC = MUSIC_VAULT_ROOT / "ALAC"  # [17] Build ALAC output
MUSIC_VAULT_CAR = MUSIC_VAULT_ROOT / "AAC-Car"  # [18] AAC car (256k)
MUSIC_VAULT_PORT = MUSIC_VAULT_ROOT / "AAC-Port"  # [19] AAC portable (192k)

# Legacy paths (kept for backward compatibility)
MUSIC_VAULT_LIBRARY = MUSIC_VAULT_ALAC  # Alias for ALAC
MUSIC_VAULT_MOBILE = MUSIC_VAULT_PORT  # Alias for portable
MUSIC_VAULT_LOSSLESS = MUSIC_ROOT  # FLAC originals in main library

# Exports subtree (UPDATED 2026-06-24: Now point to Music.Vault)
ALAC_EXPORT = MUSIC_VAULT_ROOT / "ALAC"  # [17] Build ALAC
AAC_CAR_EXPORT = MUSIC_VAULT_ROOT / "AAC-Car"  # [18] Build AAC Car
AAC_PORT_EXPORT = MUSIC_VAULT_ROOT / "AAC-Port"  # [19] Build AAC Portable

# Legacy / batch source locations
ALAC_BATCH_001 = EXPORTS_ROOT / "ALAC_LIBRARY" / "BATCH_001"

# LUFS-normalized source locations
LUFS_ARCHIVE_NORMALIZED = LUFS_NORM_ROOT / "ARCHIVE_NORMALIZED"
LUFS_AAC_CAR_NORMALIZED = LUFS_NORM_ROOT / "AAC_CAR_NORMALIZED"
LUFS_AAC_PORTABLE_NORMALIZED = LUFS_NORM_ROOT / "AAC_PORTABLE_NORMALIZED"

# ── Database & canon files ────────────────────────────────────────────────────

DB_PATH = (
    Path(os.environ["ORPHEUS_DB_PATH"])
    if "ORPHEUS_DB_PATH" in os.environ
    else METADATA_ROOT / "orpheus_index.db"
)
CANON_CSV = METADATA_ROOT / "Artist_Canon.csv"
ALBUM_CANON_CSV = METADATA_ROOT / "Album_Canon.csv"
GENRE_CANON_CSV = METADATA_ROOT / "Genre_Canon.csv"
MASTERLAW = METADATA_ROOT / "MasterLaw.csv"

# MusicBrainz cache — persistent on-disk storage under METADATA
MUSICBRAINZ_CACHE_DISK = METADATA_ROOT / "musicbrainz_cache"

# ── Checkpoint location ───────────────────────────────────────────────────────

CHECKPOINT_ROOT = Path(
    os.environ.get(
        "ORPHEUS_CHECKPOINT_ROOT",
        str(Path.home() / "ORPHEUS_CHECKPOINTS"),
    )
).resolve()

# ── Audio extensions ──────────────────────────────────────────────────────────

AUDIO_EXTENSIONS: frozenset[str] = frozenset(
    {".mp3", ".flac", ".m4a", ".alac", ".aac", ".wav", ".aiff", ".ogg", ".oga"}
)

# MusicBrainz cache — always on disk
MUSICBRAINZ_CACHE = MUSICBRAINZ_CACHE_DISK

# ── Helpers ───────────────────────────────────────────────────────────────────


def sanitize_folder_name(name: str) -> str:
    """Sanitize a tag value for use as a filesystem folder name.

    Blocks directory traversal, illegal chars, and handles empty/whitespace-only.
    Enhanced version (2026-06-06) based on Nexus S1 fix.
    """
    if not name:
        return "Unknown_Folder"

    # 1. Block primary path operators completely
    cleaned = name.replace("/", "").replace("\\", "")

    # 2. Block all directory traversal patterns (../, ..\, multiple dots)
    cleaned = re.sub(r"\.\.+", ".", cleaned)

    # 3. Remove illegal filesystem characters (Windows/macOS/Linux)
    #    Replace with underscore instead of deleting to preserve readability
    illegal = r'[<>:"/\\|?*\x00-\x1f]'
    cleaned = re.sub(illegal, "_", cleaned)

    # 4. Collapse multiple whitespace/underscores to single
    cleaned = re.sub(r"[\s_]+", "_", cleaned)

    # 5. Strip leading/trailing dots, spaces, underscores (prevents hidden files)
    cleaned = cleaned.strip(". _")

    # 6. Enforce length limit (200 chars for most filesystems)
    cleaned = cleaned[:200]

    # 7. Final fallback for edge cases
    return cleaned if cleaned else "Unknown_Folder"


def apply_article_tail(name: str) -> str:
    """Move leading 'The ' to a trailing ' (the)' suffix, album-name variant.

    Only fires when the name begins with 'The ' (case-insensitive).
    Conservative: does not handle 'A', 'An', etc.
    """
    if name.lower().startswith("the "):
        return name[4:] + " (the)"
    return name


def make_run_parent_dirs() -> Path:
    """Create RUN_PARENT and the standard run subdirs. Returns RUN_PARENT."""
    for d in (
        RUN_PARENT,
        REPORTS_ROOT,
        DEDUPE_ROOT,
        REVIEW_ROOT,
        LOGS_ROOT,
        FINAL_ROOT,
    ):
        d.mkdir(parents=True, exist_ok=True)
    return RUN_PARENT


def make_run_report_dir(prefix: str = "") -> Path:
    """Return a subdir of REPORTS_ROOT for this run."""
    out = REPORTS_ROOT / prefix if prefix else REPORTS_ROOT
    out.mkdir(parents=True, exist_ok=True)
    return out


def make_reports_current() -> Path:
    """Create and return RUNS/Reports/CURRENT."""
    REPORTS_CURRENT.mkdir(parents=True, exist_ok=True)
    return REPORTS_CURRENT


def assert_project_layout() -> None:
    missing: list[Path] = []
    for required in (SCRIPTS_DIR, RUNS_ROOT, METADATA_ROOT):
        if not required.exists():
            missing.append(required)
    if missing:
        raise SystemExit(
            "ORPHEUS project layout looks wrong. Missing:\n"
            + "\n".join(f"  - {p}" for p in missing)
            + f"\n\nPROJECT_ROOT is currently: {PROJECT_ROOT}"
            + "\nSet ORPHEUS_ROOT in the environment if the project moved."
        )


__all__ = [
    "PROJECT_ROOT",
    "SCRIPTS_DIR",
    "RUNS_ROOT",
    "METADATA_ROOT",
    "EXPORTS_ROOT",
    "CONVERSION_INBOX",
    "INBOX_ROOT",
    "INBOX_CURRENT",
    "INBOX_STREAMING",
    "INBOX_ARCHIVE",
    "RECHECK_ROOT",
    "MUSIC_ROOT",
    "QUARANTINE",
    "LUFS_NORM_ROOT",
    "DEDUPE_ARCHIVE",
    "RUN_STAMP",
    "RUN_PARENT",
    "REPORTS_ROOT",
    "REPORTS_CURRENT",
    "DEDUPE_ROOT",
    "REVIEW_ROOT",
    "REVIEW_DIR",
    "LOGS_ROOT",
    "MUSIC_VAULT_ROOT",
    "MUSIC_VAULT_ALAC",
    "MUSIC_VAULT_CAR",
    "MUSIC_VAULT_PORT",
    "MUSIC_VAULT_LIBRARY",
    "MUSIC_VAULT_MOBILE",
    "MUSIC_VAULT_LOSSLESS",
    "ALAC_EXPORT",
    "AAC_CAR_EXPORT",
    "AAC_PORT_EXPORT",
    "ALAC_BATCH_001",
    "LUFS_ARCHIVE_NORMALIZED",
    "LUFS_AAC_CAR_NORMALIZED",
    "LUFS_AAC_PORTABLE_NORMALIZED",
    "DB_PATH",
    "CANON_CSV",
    "ALBUM_CANON_CSV",
    "GENRE_CANON_CSV",
    "MASTERLAW",
    "MUSICBRAINZ_CACHE",
    "CHECKPOINT_ROOT",
    "USB3_DEDUPE",
    "CAR_USB_TARGET",
    "KODI_PLAYLIST_DIR",
    "MUSIC_SOURCE",
    "FINAL_ROOT",
    "CAR_LIBRARY_TARGET",
    "AUDIO_EXTENSIONS",
    "sanitize_folder_name",
    "apply_article_tail",
    "make_run_parent_dirs",
    "make_run_report_dir",
    "make_reports_current",
    "assert_project_layout",
]
