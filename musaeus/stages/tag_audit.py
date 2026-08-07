#!/usr/bin/env python3
"""
MUSAEUS — Tag Audit Stage

Report-only audit: compares embedded artist metadata tags against the parent
artist folder name, cross-referenced with artist_canon.tsv.
Flags cases where the folder and tag disagree on the canonical artist.

Both run() and dry_run() produce the same report — this stage never modifies files.
"""

from __future__ import annotations

import csv
import logging
import re
from pathlib import Path
from typing import TYPE_CHECKING

from ..config import AUDIO_EXTENSIONS
from ..context import RunContext, StageResult
from .base import BaseStage, StageError

if TYPE_CHECKING:
    pass

logger = logging.getLogger(__name__)


# ── Normalisation ─────────────────────────────────────────────────────────────


def _norm_key(text: str) -> str:
    """Normalise an artist name for fuzzy comparison."""
    if not text:
        return ""
    text = text.lower()
    text = text.replace("&", " and ")
    text = re.sub(r"\(\s*the\s*\)\s*$", "", text)
    text = re.sub(r"^the\s+", "", text)
    text = re.sub(r"[^a-z0-9\s]", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


# ── Canon loading ─────────────────────────────────────────────────────────────


def _load_canon(meta_dir: Path) -> dict[str, str]:
    """Load artist_canon.tsv into a dict mapping norm_key(raw) -> canonical_name.

    File format: tab-separated raw_name\\tcanonical_name (with # comment header).
    """
    canon_path = meta_dir / "artist_canon.tsv"
    canon: dict[str, str] = {}
    if not canon_path.exists():
        logger.warning("[tag_audit] artist_canon.tsv not found at %s", canon_path)
        return canon

    with open(canon_path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            parts = line.split("\t", maxsplit=1)
            if len(parts) != 2:
                continue
            raw, canonical = parts
            canon[_norm_key(raw)] = canonical.strip()

    logger.info("[tag_audit] loaded %d canon entries from %s", len(canon), canon_path)
    return canon


# ── Tag reading ───────────────────────────────────────────────────────────────


def _read_artist_tags(file_path: Path) -> tuple[str, str]:
    """Read artist and album artist tags from an audio file.

    Returns (artist, album_artist). Empty string if tag is absent.
    """
    suffix = file_path.suffix.lower()

    try:
        if suffix in (".m4a", ".mp4", ".aac", ".alac"):
            from mutagen.mp4 import MP4

            tags = MP4(str(file_path))
            artist = (tags.get("\xa9ART") or [""])[0]
            album_artist = (tags.get("aART") or [""])[0]
            return str(artist), str(album_artist)

        elif suffix == ".flac":
            from mutagen.flac import FLAC

            tags = FLAC(str(file_path))
            artist = (tags.get("artist") or [""])[0]
            album_artist = (tags.get("albumartist") or [""])[0]
            return str(artist), str(album_artist)

        elif suffix == ".mp3":
            from mutagen.mp3 import MP3

            tags = MP3(str(file_path))
            artist = ""
            album_artist = ""
            if tags.tags:
                tpe1 = tags.tags.get("TPE1")
                if tpe1 and tpe1.text:
                    artist = str(tpe1.text[0])
                tpe2 = tags.tags.get("TPE2")
                if tpe2 and tpe2.text:
                    album_artist = str(tpe2.text[0])
            return artist, album_artist

    except Exception as exc:
        logger.debug("[tag_audit] failed to read tags from %s: %s", file_path.name, exc)

    return "", ""


# ── Mismatch detection ────────────────────────────────────────────────────────


def _resolve_canonical(name: str, canon: dict[str, str]) -> str:
    """Resolve a name through the canon. Returns canonical if found, else original."""
    key = _norm_key(name)
    return canon.get(key, name)


def _classify_mismatch(
    folder_name: str,
    tag_artist: str,
    canon: dict[str, str],
) -> tuple[str, str, str] | None:
    """Classify a mismatch between folder and tag.

    Returns (canon_from_folder, canon_from_tag, mismatch_type) or None if no mismatch.
    """
    canon_folder = _resolve_canonical(folder_name, canon)
    canon_tag = _resolve_canonical(tag_artist, canon)

    norm_folder = _norm_key(canon_folder)
    norm_tag = _norm_key(canon_tag)

    if norm_folder == norm_tag:
        return None

    # Determine mismatch type
    folder_in_canon = _norm_key(folder_name) in canon
    tag_in_canon = _norm_key(tag_artist) in canon

    if folder_in_canon and tag_in_canon:
        # Both are in canon but resolve differently
        mismatch_type = "FOLDER_VS_TAG"
    elif folder_in_canon and not tag_in_canon:
        mismatch_type = "TAG_VS_CANON"
    elif not folder_in_canon and tag_in_canon:
        mismatch_type = "FOLDER_VS_CANON"
    else:
        # Neither in canon — raw disagreement
        mismatch_type = "FOLDER_VS_TAG"

    return canon_folder, canon_tag, mismatch_type


def _suggested_fix(
    canon_from_folder: str,
    canon_from_tag: str,
    tag_artist: str,
    folder_name: str,
    canon: dict[str, str],
) -> str:
    """Determine the suggested fix. Priority: canon resolution > tag > folder."""
    # If tag resolves through canon, prefer that canonical
    if _norm_key(tag_artist) in canon:
        return canon_from_tag
    # If folder resolves through canon, prefer that canonical
    if _norm_key(folder_name) in canon:
        return canon_from_folder
    # Fallback: prefer tag over folder
    return tag_artist if tag_artist else folder_name


# ── Stage class ───────────────────────────────────────────────────────────────


class TagAuditStage(BaseStage):
    """
    Tag Audit — report-only stage.
    Compares embedded artist tags against folder names, cross-referenced with canon.
    Both run() and dry_run() produce the same CSV report.
    """

    NAME = "tag_audit"

    # ── Validate ──────────────────────────────────────────────────────────────

    def validate(self, ctx: RunContext) -> None:
        canon_path = ctx.config.meta_dir / "artist_canon.tsv"
        if not canon_path.exists():
            raise StageError(
                f"artist_canon.tsv not found at {canon_path} — "
                "tag_audit requires the artist canon to cross-reference."
            )

        # Determine library root
        library_root = self._library_root(ctx)
        if not library_root.exists():
            raise StageError(
                f"Library root does not exist: {library_root} — nothing to audit."
            )

    # ── Shared logic ──────────────────────────────────────────────────────────

    def _library_root(self, ctx: RunContext) -> Path:
        """Resolve the library root: vault_root/Library if it exists, else vault_root."""
        lib = ctx.vault_root / "Library"
        if lib.exists():
            return lib
        return ctx.vault_root

    def _audit(self, ctx: RunContext, dry_run: bool) -> StageResult:
        result = self._make_result(dry_run=dry_run)

        canon = _load_canon(ctx.config.meta_dir)
        library_root = self._library_root(ctx)

        # Supported extensions for this stage
        target_exts = frozenset({".m4a", ".flac", ".mp3", ".alac", ".aac"})
        supported = AUDIO_EXTENSIONS & target_exts

        mismatches: list[dict[str, str]] = []

        # Walk: library_root/{artist}/{album}/{file}
        for artist_dir in sorted(library_root.iterdir()):
            if not artist_dir.is_dir():
                continue
            folder_name = artist_dir.name

            for album_dir in sorted(artist_dir.iterdir()):
                if not album_dir.is_dir():
                    continue

                for audio_file in sorted(album_dir.iterdir()):
                    if not audio_file.is_file():
                        continue
                    if audio_file.suffix.lower() not in supported:
                        continue

                    result.files_processed += 1
                    tag_artist, tag_album_artist = _read_artist_tags(audio_file)

                    # Use tag_artist for comparison; fall back to album_artist
                    compare_artist = tag_artist or tag_album_artist
                    if not compare_artist:
                        result.files_skipped += 1
                        continue

                    classification = _classify_mismatch(folder_name, compare_artist, canon)
                    if classification is None:
                        continue

                    canon_from_folder, canon_from_tag, mismatch_type = classification
                    fix = _suggested_fix(
                        canon_from_folder, canon_from_tag,
                        compare_artist, folder_name, canon,
                    )

                    mismatches.append({
                        "path": str(audio_file),
                        "artist_folder": folder_name,
                        "tag_artist": tag_artist,
                        "tag_album_artist": tag_album_artist,
                        "canon_from_folder": canon_from_folder,
                        "canon_from_tag": canon_from_tag,
                        "mismatch_type": mismatch_type,
                        "suggested_fix": fix,
                    })
                    result.files_changed += 1

                    ctx.log_event(
                        "TAG_AUDIT_MISMATCH",
                        file_path=str(audio_file),
                        old_value=folder_name,
                        new_value=fix,
                        stage="tag_audit",
                    )

        # Write report
        self._write_report(ctx, mismatches)

        result.notes.append(
            f"Scanned {result.files_processed} file(s): "
            f"{result.files_changed} mismatch(es), "
            f"{result.files_skipped} skipped (no tags)."
        )

        ctx.record_stage(result)
        return result

    def _write_report(self, ctx: RunContext, mismatches: list[dict[str, str]]) -> None:
        """Write the CSV audit report to the run directory."""
        run_dir = ctx.ensure_run_dir()
        report_path = run_dir / "tag_audit_report.csv"

        fieldnames = [
            "path",
            "artist_folder",
            "tag_artist",
            "tag_album_artist",
            "canon_from_folder",
            "canon_from_tag",
            "mismatch_type",
            "suggested_fix",
        ]

        with open(report_path, "w", newline="", encoding="utf-8") as fh:
            writer = csv.DictWriter(fh, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(mismatches)

        logger.info(
            "[tag_audit] report written to %s (%d row(s))",
            report_path,
            len(mismatches),
        )

    # ── Dry run / Run ─────────────────────────────────────────────────────────

    def dry_run(self, ctx: RunContext) -> StageResult:
        return self._audit(ctx, dry_run=True)

    def run(self, ctx: RunContext) -> StageResult:
        return self._audit(ctx, dry_run=False)
