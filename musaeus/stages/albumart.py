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

from ..art_quality import MIN_EDGE_PX, describe, image_dimensions
from ..context import RunContext, StageResult
from ..db import ensure_columns
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


def _ensure_columns(conn) -> None:  # type: ignore[type-arg]
    """Columns this stage owns. Mechanism shared via db.ensure_columns;
    the list stays here, next to the code that reads them."""
    ensure_columns(
        conn,
        (
            ("has_art", "INTEGER"),
            ("art_checked_at", "TEXT"),
            ("art_px", "INTEGER"),
        ),
    )
def _embedded_art(path: str) -> tuple[bool, int]:
    """(has_art, longest_edge_px) for the file's embedded cover.

    The stage used to ask only "is there art?", which reported 99.9% coverage
    while 257 files carried covers under 500px. Width and height come from the
    same ffprobe call that answers the first question, so asking the better
    question costs nothing extra. A longest edge of 0 means "art present but
    dimensions unreadable" -- not a reason to replace it.
    """
    ffprobe = shutil.which("ffprobe")
    if not ffprobe:
        return False, 0
    try:
        res = subprocess.run(
            [
                ffprobe,
                "-v",
                "error",
                "-select_streams",
                "v",
                "-show_entries",
                "stream=width,height",
                "-of",
                "csv=p=0",
                path,
            ],
            capture_output=True,
            text=True,
            timeout=_FFPROBE_TIMEOUT,
        )
    except Exception:
        return False, 0

    line = res.stdout.strip().splitlines()[0] if res.stdout.strip() else ""
    if not line:
        return False, 0
    try:
        w, h = (int(v) for v in line.split(",")[:2])
        return True, max(w, h)
    except ValueError:
        return True, 0


def _has_embedded_art(path: str) -> bool:
    """Kept as the plain yes/no question -- used by tests and callers that
    only care whether art exists."""
    return _embedded_art(path)[0]


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
    # ffmpeg picks its muxer from the output extension, so the real extension
    # has to come last. "foo.m4a.artmp" makes it exit with "Unable to find a
    # suitable output format" -- every embed failed that way, silently, and
    # ART_EMBEDDED had never once been logged.
    tmp = src.with_name(src.stem + ".artmp" + src.suffix)

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



def _fetch_sidecar(ctx: RunContext, fp: str, result: StageResult) -> Path | None:
    """Fetch cover art from the network and drop it beside the file.

    Returns the sidecar path, or None when no art exists or nothing could
    be asked. Network failure is never fatal to the stage: art is a nicety
    and the run has audio to finish processing.
    """
    from ..art_sources import ArtUnavailable, fetch_album_art

    row = ctx.conn.execute(
        "SELECT artist, album FROM archive WHERE file_path=?", (fp,)
    ).fetchone()
    if row is None:
        return None
    artist = str(row["artist"] or "").strip()
    album = str(row["album"] or "").strip()
    if not artist:
        return None

    try:
        got = fetch_album_art(artist, album, ctx.config.lastfm_api_key or "")
    except ArtUnavailable as exc:
        logger.debug("[albumart] could not ask for %r/%r: %s", artist, album, exc)
        result.notes.append(f"art lookup unavailable for {artist} — {album}")
        return None
    if not got:
        return None

    blob, source = got
    target = Path(fp).parent / "cover.jpg"
    try:
        target.write_bytes(blob)
    except OSError as exc:
        logger.warning("[albumart] could not write %s: %s", target, exc)
        return None

    ctx.log_event(
        "ART_FETCHED", file_path=fp, new_value=source, stage="albumart",
        note=describe(blob),
    )
    logger.info("[albumart] fetched %s from %s for %s", describe(blob), source, Path(fp).name)
    return target


def _replace_undersized(
    ctx: RunContext, fp: str, current_px: int, result: StageResult
) -> bytes | None:
    """Look for a cover BIGGER than the one the file already carries.

    Returns the new image, or None to keep what is there. The floor passed to
    the sources is the current size, not MIN_EDGE_PX: a source offering the
    same 300x300 back is not an improvement, and swapping like for like would
    rewrite the audio for nothing.
    """
    from ..art_sources import ArtUnavailable, fetch_album_art

    row = ctx.conn.execute(
        "SELECT artist, album FROM archive WHERE file_path=?", (fp,)
    ).fetchone()
    if row is None:
        return None
    artist = str(row["artist"] or "").strip()
    album = str(row["album"] or "").strip()
    if not artist:
        return None

    try:
        got = fetch_album_art(artist, album, ctx.config.lastfm_api_key or "",
                              min_edge=max(current_px + 1, MIN_EDGE_PX))
    except ArtUnavailable as exc:
        logger.debug("[albumart] could not ask for %r/%r: %s", artist, album, exc)
        return None
    if not got:
        return None

    blob, source = got
    dims = image_dimensions(blob)
    new_px = max(dims) if dims else 0
    if new_px <= current_px:
        logger.debug("[albumart] %s offered %dpx for %s, not better than %dpx",
                     source, new_px, Path(fp).name, current_px)
        return None

    ctx.log_event(
        "ART_UPGRADED", file_path=fp, old_value=f"{current_px}px",
        new_value=f"{new_px}px from {source}", stage="albumart",
    )
    logger.info("[albumart] upgrading %s: %dpx -> %s from %s",
                Path(fp).name, current_px, describe(blob), source)
    return blob


class AlbumArtStage(BaseStage):
    """
    AlbumArt — audit for missing embedded art, embed sidecars where found.
    """

    NAME = "albumart"

    def verify_effect(self, ctx: RunContext, result: StageResult) -> list[str]:
        """A file this stage says it gave art to must actually have art.

        This is the stage that made the case for the whole mechanism. On
        2026-08-31 it reported "OK ✓verified | changed=10549" while every
        single embed failed -- `_embed_art` had named its temp file
        `track.m4a.artmp`, ffmpeg could not infer a muxer from `.artmp`,
        and the encode died every time. ART_EMBEDDED had been zero for the
        project's entire history. Nothing noticed, because the stage's
        claim was checked against nothing.

        So the check is deliberately against the FILE, not the row: read
        has_art back from ffprobe rather than from the database column
        this stage just wrote. A stage confirming its own bookkeeping
        proves only that it can write to SQLite.

        Samples rather than re-probing 10,000 files -- the point is to
        catch a wholesale failure, not to double the runtime.
        """
        rows = ctx.conn.execute(
            "SELECT file_path FROM archive "
            " WHERE has_art = 1 AND art_checked_at IS NOT NULL "
            " ORDER BY art_checked_at DESC LIMIT 5"
        ).fetchall()
        if not rows:
            return []

        checked = [r for r in rows if Path(r["file_path"]).is_file()]
        if not checked:
            return []

        artless = [
            Path(r["file_path"]).name
            for r in checked
            if not _has_embedded_art(r["file_path"])
        ]
        if not artless:
            return []
        return [
            f"{len(artless)} of {len(checked)} sampled file(s) are recorded as "
            f"having art but ffprobe finds none: {', '.join(artless[:3])}"
        ]

    def validate(self, ctx: RunContext) -> None:
        if not shutil.which("ffprobe"):
            raise StageError("ffprobe not found — AlbumArt requires ffprobe.")
        if not shutil.which("ffmpeg"):
            logger.warning(
                "[albumart] ffmpeg not found — can audit but cannot embed art. "
                "Install ffmpeg to enable sidecar embedding."
            )

        try:
            count = ctx.conn.execute(
                "SELECT COUNT(*) FROM archive WHERE status='CATALOGUED' AND art_checked_at IS NULL"
            ).fetchone()[0]
        except Exception:
            count = ctx.conn.execute(
                "SELECT COUNT(*) FROM archive WHERE status='CATALOGUED'"
            ).fetchone()[0]
        logger.info("[albumart] %d file(s) to check", count)

    def _audit(self, ctx: RunContext, dry_run: bool) -> StageResult:
        result = self._make_result(dry_run=dry_run)

        if not dry_run:
            _ensure_columns(ctx.conn)

        force = ctx.get("albumart_force", False)
        # Replacing a too-small cover reaches the network and rewrites audio,
        # so it is live-run only, exactly like the missing-art fetch.
        replace_small = ctx.get("albumart_replace_small", True) and not dry_run \
            and bool(shutil.which("ffmpeg"))
        embed = ctx.get("albumart_embed", True) and not dry_run and shutil.which("ffmpeg")

        try:
            where_extra = "" if force else "AND art_checked_at IS NULL"
            rows = ctx.conn.execute(
                f"""
                SELECT file_path FROM archive
                WHERE status = 'CATALOGUED'
                  {where_extra}
                ORDER BY file_path
                """
            ).fetchall()
        except Exception:
            rows = ctx.conn.execute(
                "SELECT file_path FROM archive WHERE status='CATALOGUED' ORDER BY file_path"
            ).fetchall()

        from datetime import datetime, timezone

        now = datetime.now(tz=timezone.utc).isoformat(timespec="seconds")

        # Network fetch is a live-run behaviour. A preview must not reach out,
        # and the gateway would refuse it anyway.
        fetched_ok = None if dry_run else _fetch_sidecar

        has_art_count = 0
        missing_art: list[str] = []
        embedded_count = 0
        embed_failed: list[str] = []
        undersized: list[str] = []
        upgraded_count = 0

        for row in rows:
            result.files_processed += 1
            fp = row["file_path"]

            if not Path(fp).exists():
                result.files_skipped += 1
                continue

            has, art_px = _embedded_art(fp)

            if not dry_run:
                ctx.conn.execute(
                    "UPDATE archive SET has_art=?, art_checked_at=?, art_px=? "
                    "WHERE file_path=?",
                    (1 if has else 0, now, art_px or None, fp),
                )

            if has:
                has_art_count += 1
                result.files_changed += 1
                # Present but too small still counts as a defect worth fixing.
                # 0 means the header would not parse -- leave those alone
                # rather than replacing art we could not measure.
                if replace_small and 0 < art_px < MIN_EDGE_PX:
                    undersized.append(fp)
                    blob = _replace_undersized(ctx, fp, art_px, result)
                    if blob:
                        target = Path(fp).parent / "cover.jpg"
                        try:
                            target.write_bytes(blob)
                        except OSError as exc:
                            logger.warning("[albumart] could not write %s: %s", target, exc)
                        else:
                            if _embed_art(fp, target):
                                upgraded_count += 1
                                new_px = max(image_dimensions(blob) or (0, 0))
                                ctx.conn.execute(
                                    "UPDATE archive SET art_px=? WHERE file_path=?",
                                    (new_px, fp),
                                )
                            else:
                                embed_failed.append(fp)
                                logger.warning("[albumart] upgrade embed failed: %s",
                                               Path(fp).name)
            else:
                missing_art.append(fp)
                # Try sidecar embed
                if embed:
                    sidecar = _find_sidecar(Path(fp).parent)

                    # No sidecar on disk used to end it -- this stage made
                    # zero network calls, so a folder without a cover.jpg
                    # meant the file stayed without art for ever. Fetch one.
                    #
                    # Written to the folder as a sidecar rather than embedded
                    # directly, so the existing embed path stays the single
                    # place that mutates audio, and so the next file in the
                    # same album reuses it instead of asking again.
                    if sidecar is None and fetched_ok is not None:
                        sidecar = fetched_ok(ctx, fp, result)

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
        if undersized:
            result.notes.append(
                f"{len(undersized)} file(s) carried art under {MIN_EDGE_PX}px; "
                f"upgraded {upgraded_count}."
            )
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
