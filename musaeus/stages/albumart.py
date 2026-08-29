#!/usr/bin/env python3
"""
MUSAEUS — Stage: AlbumArt
Audit for missing embedded album art and embed sidecar images.

What it does:
  - Scans CATALOGUED files to check for embedded cover art (via ffprobe)
  - Records has_art (1/0) in archive per file
  - For files with missing art, looks for a sidecar image in the same folder:
    cover.jpg, cover.png, folder.jpg, folder.png, artwork.jpg, front.jpg (etc.)
  - If a sidecar is found, embeds it using ffmpeg (non-destructive: rewrites file)
  - Reports all files still missing art after embed pass
  - Writes album_art_report.txt to RUNS_ROOT
  - dry_run(): reports missing art counts without writing anything

Design:
  - Sidecar search is case-insensitive
  - Prefers JPG over PNG (smaller, universal support)
  - Skips files already marked has_art=1 (re-run safe)
  - force=True (via ctx.get("albumart_force")) re-checks everything

ORPHEUS equivalents: SCRIPTS/orpheus_album_art_report.py,
                     SCRIPTS/embed_sidecar_art.py
"""

from __future__ import annotations

import logging
import shutil
import subprocess
from pathlib import Path

from ..context import RunContext, StageResult
from .base import BaseStage, StageError

logger = logging.getLogger(__name__)

_COMMIT_EVERY = 50
_FFMPEG_TIMEOUT = 60
_FFPROBE_TIMEOUT = 10

# Sidecar filenames to look for (checked in order, case-insensitive)
_SIDECAR_NAMES = [
    "cover.jpg",
    "cover.png",
    "folder.jpg",
    "folder.png",
    "artwork.jpg",
    "artwork.png",
    "front.jpg",
    "front.png",
    "AlbumArt.jpg",
    "AlbumArtSmall.jpg",
]


# ── Column migration ──────────────────────────────────────────────────────────


# ── Helpers ───────────────────────────────────────────────────────────────────


def _has_embedded_art(path: str) -> bool:
    """Return True if the file has at least one embedded image stream."""
    ffprobe = shutil.which("ffprobe")
    if not ffprobe:
        return False
    try:
        res = subprocess.run(
            [
                ffprobe,
                "-v",
                "error",
                "-select_streams",
                "v",
                "-show_entries",
                "stream=codec_type",
                "-of",
                "csv=p=0",
                path,
            ],
            capture_output=True,
            text=True,
            timeout=_FFPROBE_TIMEOUT,
        )
        return bool(res.stdout.strip())
    except Exception:
        return False


def _find_sidecar(folder: Path) -> Path | None:
    """Return the first matching sidecar image in folder, or None."""
    # Build a case-insensitive lookup
    try:
        existing = {f.name.lower(): f for f in folder.iterdir() if f.is_file()}
    except OSError:
        return None
    for name in _SIDECAR_NAMES:
        hit = existing.get(name.lower())
        if hit:
            return hit
    return None


def _embed_art(audio_path: str, art_path: Path) -> bool:
    """
    Embed art_path into audio_path using ffmpeg (in-place via temp file).
    Returns True on success.
    """
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        return False

    src = Path(audio_path)
    tmp = src.with_suffix(src.suffix + ".artmp")

    cmd = [
        ffmpeg,
        "-y",
        "-hide_banner",
        "-nostats",
        "-i",
        audio_path,
        "-i",
        str(art_path),
        "-map",
        "0:a",
        "-map",
        "1:v",
        "-c:a",
        "copy",
        "-c:v",
        "mjpeg",
        "-disposition:v:0",
        "attached_pic",
        str(tmp),
    ]

    try:
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=_FFMPEG_TIMEOUT)
    except subprocess.TimeoutExpired:
        if tmp.exists():
            tmp.unlink()
        return False

    if res.returncode != 0:
        if tmp.exists():
            tmp.unlink()
        return False

    # Atomically replace original
    tmp.replace(src)
    return True


# ── Stage ─────────────────────────────────────────────────────────────────────


class AlbumArtStage(BaseStage):
    """
    AlbumArt — audit for missing embedded art, embed sidecars where found.
    """

    NAME = "albumart"

    def validate(self, ctx: RunContext) -> None:
        if not shutil.which("ffprobe"):
            raise StageError("ffprobe not found — AlbumArt requires ffprobe.")
        if not shutil.which("ffmpeg"):
            logger.warning(
                "[albumart] ffmpeg not found — can audit but cannot embed art. "
                "Install ffmpeg to enable sidecar embedding."
            )

        count = ctx.conn.execute(
            "SELECT COUNT(*) FROM archive WHERE status='CATALOGUED' AND art_checked_at IS NULL"
        ).fetchone()[0]
        logger.info("[albumart] %d file(s) to check", count)

    def _audit(self, ctx: RunContext, dry_run: bool) -> StageResult:
        result = self._make_result(dry_run=dry_run)

        force = ctx.get("albumart_force", False)
        embed = ctx.get("albumart_embed", True) and not dry_run and shutil.which("ffmpeg")

        where_extra = "" if force else "AND art_checked_at IS NULL"
        rows = ctx.conn.execute(
            f"""
            SELECT file_path FROM archive
            WHERE status = 'CATALOGUED'
              {where_extra}
            ORDER BY file_path
            """
        ).fetchall()

        from datetime import datetime, timezone

        now = datetime.now(tz=timezone.utc).isoformat(timespec="seconds")

        has_art_count = 0
        missing_art: list[str] = []
        embedded_count = 0
        embed_failed: list[str] = []

        for row in rows:
            result.files_processed += 1
            fp = row["file_path"]

            if not Path(fp).exists():
                result.files_skipped += 1
                continue

            has = _has_embedded_art(fp)

            if not dry_run:
                ctx.conn.execute(
                    "UPDATE archive SET has_art=?, art_checked_at=? WHERE file_path=?",
                    (1 if has else 0, now, fp),
                )

            if has:
                has_art_count += 1
                result.files_changed += 1
            else:
                missing_art.append(fp)
                # Try sidecar embed
                if embed:
                    sidecar = _find_sidecar(Path(fp).parent)
                    if sidecar:
                        logger.info(
                            "[albumart] embedding %s → %s",
                            sidecar.name,
                            Path(fp).name,
                        )
                        ok = _embed_art(fp, sidecar)
                        if ok:
                            embedded_count += 1
                            result.files_changed += 1
                            ctx.conn.execute(
                                "UPDATE archive SET has_art=1 WHERE file_path=?",
                                (fp,),
                            )
                            ctx.log_event(
                                "ART_EMBEDDED",
                                file_path=fp,
                                new_value=str(sidecar),
                                stage=self.NAME,
                            )
                        else:
                            embed_failed.append(fp)
                            logger.warning("[albumart] embed failed: %s", Path(fp).name)
                    else:
                        result.files_changed += 1

            if result.files_processed % _COMMIT_EVERY == 0 and not dry_run:
                ctx.conn.commit()
                logger.info("[albumart] checkpoint %d", result.files_processed)

        if not dry_run:
            self._write_report(ctx, missing_art, embedded_count, embed_failed)

        prefix = "Would check" if dry_run else "Checked"
        result.notes.append(
            f"{prefix} {result.files_processed} file(s): "
            f"{has_art_count} have art, {len(missing_art)} missing."
        )
        if embedded_count:
            result.notes.append(f"Embedded sidecar art in {embedded_count} file(s).")
        if embed_failed:
            result.notes.append(f"{len(embed_failed)} embed failure(s) — check logs.")
        if missing_art and not dry_run:
            result.notes.append(
                f"{len(missing_art)} file(s) still missing art — "
                f"see {ctx.config.runs_root / 'album_art_report.txt'}"
            )

        ctx.record_stage(result)
        return result

    def _write_report(
        self,
        ctx: RunContext,
        missing_art: list[str],
        embedded: int,
        failed: list[str],
    ) -> None:
        report_path = ctx.config.runs_root / "album_art_report.txt"
        ctx.config.runs_root.mkdir(parents=True, exist_ok=True)
        lines = [
            "MUSAEUS ALBUM ART REPORT",
            f"Vault     : {ctx.config.vault_root}",
            f"Missing   : {len(missing_art)} file(s)",
            f"Embedded  : {embedded} sidecar(s) embedded this run",
            f"Failures  : {len(failed)}",
            "=" * 72,
            "",
        ]
        if missing_art:
            lines.append("Files missing embedded art:")
            for fp in missing_art:
                lines.append(f"  ✗  {fp}")
            lines.append("")
        if failed:
            lines.append("Embed failures:")
            for fp in failed:
                lines.append(f"  !  {fp}")
            lines.append("")
        with open(report_path, "w", encoding="utf-8") as fh:
            fh.write("\n".join(lines))
        logger.info("[albumart] report written to %s", report_path)

    def dry_run(self, ctx: RunContext) -> StageResult:
        return self._audit(ctx, dry_run=True)

    def run(self, ctx: RunContext) -> StageResult:
        return self._audit(ctx, dry_run=False)
