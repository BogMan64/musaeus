#!/usr/bin/env python3
"""
MUSAEUS — Rebuild the archive table from disk + embedded file tags.

The real replacement for `rebuild-db`, which was disabled 2026-08-21 after
being confirmed unable to work: it replayed the events table, but that table
is a human-readable audit trail and is lossy by design (hashes stored
truncated to 16 chars plus an ellipsis; album/genre/year/track/duration/codec
never recorded at all). See musaeus/rebuild.py for the full evidence.

This module inverts the question. Instead of asking "what happened?", it asks
"what is actually on disk right now?" -- which is answerable, because MUSAEUS
deliberately writes its work back into the files themselves. That principle
("store as much as possible on the m4a so future runs don't redo it", Grey,
2026-08-20) was built for resumability, and it is exactly what makes real
recovery possible.

What is recovered, and from where:

  filesystem   file_path, filename, ext, size_bytes, last_modified
  file tags    artist, albumartist, album, title, genre, year, track
  file tags    bpm, musical_key, energy, danceability   (written by BPMStage)
  file tags    lufs, rg_gain                            (decoded from Forge's
                                                          R128_TRACK_GAIN, the
                                                          same read its own
                                                          tag-shortcut uses)
  ffprobe      duration, bitrate, sample_rate, channels, codec
  recomputed   audio_hash, full_hash
  location     status, finalized_at, canonicalized_at

What is NOT recovered, and must be re-derived by re-running the relevant
stage rather than guessed at here:

  mb_artist_id / mb_artist_name / mb_release_id / mb_enriched_at  -> MBEnrich
  car_export_path / noise_profile                                 -> Curator
  bitrot_checked_at / bitrot_ok                                   -> BitRot
  lufs_tp / rg_peak       (Forge's R128 atom carries gain only, not true peak)
  exact original date_added / rg_tagged_at / bpm_analyzed_at timestamps

Safety: this NEVER deletes. It writes into a fresh table and leaves the
existing archive untouched unless --replace is passed, and even then only
after the rebuild has completed successfully and the old table has been
renamed aside rather than dropped. That is the opposite of the old
rebuild-db, whose fatal flaw was issuing DELETE FROM archive *first* and
discovering it could not repopulate afterwards.
"""

from __future__ import annotations

import logging
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .config import AUDIO_EXTENSIONS, MusicConfig
from .hasher import audio_hash_safe, file_hash
from .stages.bpm import read_existing_tags as _read_bpm_tags
from .stages.forge import read_existing_rg_tags as _read_rg_tags
from .stages.scholar import _extract_meta, _probe

logger = logging.getLogger(__name__)

_COMMIT_EVERY = 50

# Directory names under ALAC-Library that carry a status other than the
# ordinary "this is live library content" meaning.
_STATUS_BY_DIR = {
    "DUPES_MOVED_FOR_REVIEW": "DUPE_REVIEW",
    "TRIBUTE_REMOVED_FOR_REVIEW": "TRIBUTE_REVIEW",
}


def _read_all_tags(path: Path) -> dict[str, Any]:
    """Read every tag-derived field MUSAEUS knows how to recover.

    Reuses the exact readers the pipeline stages use, rather than a second
    implementation -- duplicated tag handling is what let the article bug
    regress three times (scope doc §5).
    """
    out: dict[str, Any] = {}

    try:
        from mutagen import File as MutagenFile  # type: ignore[import-untyped]

        mf = MutagenFile(str(path), easy=True)
        if mf is not None and mf.tags:

            def _t(*keys: str) -> str:
                for k in keys:
                    v = mf.tags.get(k)
                    if v:
                        return str(v[0]).strip()
                return ""

            out["albumartist"] = _t("albumartist")
    except Exception as exc:
        logger.debug("albumartist read failed for %s: %s", path, exc)

    # BPM/key/energy/danceability -- BPMStage writes these back to the file
    # specifically so they never need recomputing.
    try:
        bpm = _read_bpm_tags(path)
        if bpm:
            out["bpm"] = bpm.get("bpm")
            out["musical_key"] = bpm.get("musical_key") or None
            out["energy"] = bpm.get("energy")
            out["danceability"] = bpm.get("danceability")
    except Exception as exc:
        logger.debug("bpm tag read failed for %s: %s", path, exc)

    # Loudness -- recovered from the embedded ReplayGain/R128 tags. lufs is a
    # physical property of the audio, so it survives the round-trip; lufs_tp
    # and rg_peak do not, because Apple's R128 atom stores gain only.
    try:
        rg = _read_rg_tags(path)
        if rg:
            out["lufs"] = rg.get("lufs")
            out["rg_gain"] = rg.get("rg_gain")
            out["rg_peak"] = rg.get("rg_peak")
    except Exception as exc:
        logger.debug("rg tag read failed for %s: %s", path, exc)

    return out


def _status_for(path: Path, alac_library: Path) -> str:
    """Infer status from where the file actually sits.

    Location is the honest signal: DupeResolver physically relocates a loser
    into DUPES_MOVED_FOR_REVIEW, TributeQuarantine into
    TRIBUTE_REMOVED_FOR_REVIEW. Anything else under the library is live
    content, which by definition reached Finalize.
    """
    try:
        parts = path.relative_to(alac_library).parts
    except ValueError:
        return "CATALOGUED"
    for part in parts:
        mapped = _STATUS_BY_DIR.get(part)
        if mapped:
            return mapped
    return "CATALOGUED"


def scan_and_rebuild(
    conn: sqlite3.Connection,
    cfg: MusicConfig,
    *,
    table: str = "archive_rebuilt",
    limit: int = 0,
    compute_hashes: bool = True,
    progress_every: int = _COMMIT_EVERY,
) -> dict[str, Any]:
    """Walk ALAC-Library and rebuild rows into *table*.

    Never touches the existing `archive` table. Returns a summary dict.
    """
    lib = cfg.alac_library
    summary: dict[str, Any] = {"scanned": 0, "rebuilt": 0, "errors": [], "table": table}

    if not lib.exists():
        summary["errors"].append(f"library not found: {lib}")
        return summary

    files = sorted(
        p
        for p in lib.rglob("*")
        if p.is_file() and p.suffix.lower() in AUDIO_EXTENSIONS and "_history" not in p.parts
    )
    if limit:
        files = files[:limit]
    summary["scanned"] = len(files)

    # Mirror archive's shape so the result is directly comparable, and so a
    # later --replace is a rename rather than a schema translation.
    conn.execute(f"DROP TABLE IF EXISTS {table}")
    conn.execute(f"CREATE TABLE {table} AS SELECT * FROM archive WHERE 0")

    cols = {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
    now = datetime.now(tz=timezone.utc).isoformat(timespec="seconds")

    for i, path in enumerate(files, 1):
        row: dict[str, Any] = {}
        try:
            st = path.stat()
            row.update(
                file_path=str(path),
                filename=path.name,
                ext=path.suffix.lower(),
                size_bytes=st.st_size,
                last_modified=datetime.fromtimestamp(st.st_mtime, tz=timezone.utc).isoformat(
                    timespec="seconds"
                ),
                status=_status_for(path, lib),
                date_added=now,
                last_seen=now,
            )

            try:
                row.update(_extract_meta(_probe(path)))
            except Exception as exc:
                summary["errors"].append(f"{path.name}: probe failed: {exc}")

            row.update(_read_all_tags(path))

            if compute_hashes:
                ah, err = audio_hash_safe(path)
                if ah:
                    row["audio_hash"] = ah
                elif err:
                    summary["errors"].append(f"{path.name}: audio_hash: {err}")
                try:
                    row["full_hash"] = file_hash(path)
                except OSError as exc:
                    summary["errors"].append(f"{path.name}: full_hash: {exc}")

            # Live library content necessarily passed Finalize/Canonicalize.
            # The timestamp is not recoverable, so record the file's own mtime
            # rather than inventing "now" and implying it just happened.
            if row["status"] == "CATALOGUED":
                row["finalized_at"] = row["last_modified"]
                row["canonicalized_at"] = row["last_modified"]

            usable = {k: v for k, v in row.items() if k in cols}
            placeholders = ", ".join("?" for _ in usable)
            conn.execute(
                f"INSERT INTO {table} ({', '.join(usable)}) VALUES ({placeholders})",
                list(usable.values()),
            )
            summary["rebuilt"] += 1

        except Exception as exc:  # one bad file must not end the rebuild
            summary["errors"].append(f"{path}: {exc}")

        if i % progress_every == 0:
            conn.commit()
            logger.info("rebuild-from-disk: %d/%d", i, len(files))

    conn.commit()
    return summary


def promote(conn: sqlite3.Connection, *, table: str = "archive_rebuilt") -> str:
    """Swap *table* into place as `archive`, keeping the old one aside.

    The previous archive is RENAMED, never dropped. The whole reason
    rebuild-db was dangerous is that it destroyed the old data before
    knowing it could produce new data; nothing here repeats that.
    Returns the name the old table was preserved under.
    """
    stamp = datetime.now(tz=timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    backup = f"archive_pre_rebuild_{stamp}"
    conn.execute(f"ALTER TABLE archive RENAME TO {backup}")
    conn.execute(f"ALTER TABLE {table} RENAME TO archive")
    conn.commit()
    return backup
