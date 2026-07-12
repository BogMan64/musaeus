#!/usr/bin/env python3
"""
MUSAEUS — Forge Stage

Measures integrated loudness (EBU R128) for every CATALOGUED file and writes
ReplayGain / R128 tags back into the audio file.  Stores results in the archive
table (lufs, lufs_tp, rg_gain, rg_peak, rg_tagged_at).

Rules:
  - Skips files already forged (rg_tagged_at IS NOT NULL) unless --force.
  - Never re-encodes audio.  Tags only.
  - Works sequentially (ffmpeg is already CPU-heavy); no threading.
  - Periodic DB commits every _COMMIT_EVERY files.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from pathlib import Path

from ..context import RunContext, StageResult
from ..loudness import (
    R128_APPLE_REFERENCE,
    R128_REFERENCE,
    dbtp_to_linear,
    lufs_to_rg,
    measure_loudness,
)
from .base import BaseStage, StageError

logger = logging.getLogger(__name__)

_COMMIT_EVERY = 25   # commit DB every N files (crash resilience)


# ── Tag writers ───────────────────────────────────────────────────────────────

def _write_tags_m4a(path: Path, rg_gain: float, rg_peak: float) -> bool:
    """Write R128 gain to M4A/ALAC using mutagen (Apple Q7.8 format)."""
    try:
        from mutagen.mp4 import MP4  # type: ignore[import-untyped]
        audio = MP4(str(path))
        # Apple uses R128_TRACK_GAIN in Q7.8 fixed-point (gain × 256, integer)
        audio["com.apple.iTunes.R128_TRACK_GAIN"] = [str(int(round(rg_gain * 256)))]
        audio.save()
        return True
    except Exception as exc:
        logger.debug("m4a tag write failed %s: %s", path, exc)
        return False


def _write_tags_flac(path: Path, rg_gain: float, rg_peak: float) -> bool:
    """Write ReplayGain tags to FLAC."""
    try:
        from mutagen.flac import FLAC  # type: ignore[import-untyped]
        audio = FLAC(str(path))
        audio["REPLAYGAIN_TRACK_GAIN"] = [f"{rg_gain:+.2f} dB"]
        audio["REPLAYGAIN_TRACK_PEAK"] = [f"{rg_peak:.8f}"]
        audio["REPLAYGAIN_REFERENCE_LOUDNESS"] = ["18.00 LUFS"]
        audio.save()
        return True
    except Exception as exc:
        logger.debug("flac tag write failed %s: %s", path, exc)
        return False


def _write_tags_mp3(path: Path, rg_gain: float, rg_peak: float) -> bool:
    """Write ReplayGain tags to MP3."""
    try:
        from mutagen.easyid3 import EasyID3  # type: ignore[import-untyped]
        try:
            audio = EasyID3(str(path))
        except Exception:
            from mutagen.id3 import ID3  # type: ignore[import-untyped]
            audio = ID3(str(path))
        audio["replaygain_track_gain"] = [f"{rg_gain:+.2f} dB"]
        audio["replaygain_track_peak"] = [f"{rg_peak:.8f}"]
        audio.save()
        return True
    except Exception as exc:
        logger.debug("mp3 tag write failed %s: %s", path, exc)
        return False


def _write_tags_aiff(path: Path, rg_gain: float, rg_peak: float) -> bool:
    """Write ReplayGain tags to AIFF via ID3."""
    try:
        from mutagen.aiff import AIFF  # type: ignore[import-untyped]
        audio = AIFF(str(path))
        if audio.tags is None:
            audio.add_tags()
        audio.tags["TXXX:replaygain_track_gain"] = \
            __import__("mutagen.id3", fromlist=["TXXX"]).TXXX(
                encoding=3, desc="replaygain_track_gain", text=f"{rg_gain:+.2f} dB"
            )
        audio.save()
        return True
    except Exception as exc:
        logger.debug("aiff tag write failed %s: %s", path, exc)
        return False


def write_rg_tags(
    path: Path, rg_gain: float, rg_peak: float, r128_gain: float | None = None
) -> bool:
    """Dispatch to the right tag writer based on file extension.

    r128_gain — gain referenced to -23 LUFS (EBU R128), used for Apple M4A tags.
                Falls back to rg_gain if not supplied.
    """
    ext = path.suffix.lower()
    if ext in (".m4a", ".alac"):
        # Apple com.apple.iTunes.R128_TRACK_GAIN must reference -23 LUFS.
        return _write_tags_m4a(path, r128_gain if r128_gain is not None else rg_gain, rg_peak)
    if ext == ".flac":
        return _write_tags_flac(path, rg_gain, rg_peak)
    if ext == ".mp3":
        return _write_tags_mp3(path, rg_gain, rg_peak)
    if ext in (".aiff", ".aif"):
        return _write_tags_aiff(path, rg_gain, rg_peak)
    # WAV: no standard RG tag container — store in DB only
    logger.debug("no RG tag writer for ext %s, DB-only: %s", ext, path)
    return True   # not a failure — we just don't embed


# ── DB helpers ────────────────────────────────────────────────────────────────

def _save_loudness(
    ctx: RunContext,
    file_path: str,
    lufs: float,
    lufs_tp: float,
    rg_gain: float,
    rg_peak: float,
    tagged: bool,
) -> None:
    ts = datetime.now(tz=timezone.utc).isoformat(timespec="seconds") if tagged else None
    ctx.conn.execute(
        """
        UPDATE archive
           SET lufs          = ?,
               lufs_tp       = ?,
               rg_gain       = ?,
               rg_peak       = ?,
               rg_tagged_at  = ?
         WHERE file_path = ?
        """,
        (lufs, lufs_tp, rg_gain, rg_peak, ts, file_path),
    )
    ctx.log_event(
        "FORGE_TAG",
        file_path=file_path,
        new_value=f"lufs={lufs:.2f} rg_gain={rg_gain:+.2f}",
        stage="forge",
    )


# ── Forge Stage ───────────────────────────────────────────────────────────────

class ForgeStage(BaseStage):
    """
    EBU R128 loudness measurement + ReplayGain tag embedding.

    Processes every CATALOGUED file not yet forged.
    Use ctx.set("forge_force", True) before running to retag everything.
    """

    NAME = "forge"

    def validate(self, ctx: RunContext) -> None:
        import shutil
        if not shutil.which("ffmpeg"):
            raise StageError("ffmpeg not found — required for loudness measurement")
        if not shutil.which("ffprobe"):
            raise StageError("ffprobe not found — required for duration detection")
        try:
            import mutagen  # noqa: F401
        except ImportError:
            raise StageError("mutagen not installed — run: pip install mutagen")

    # ── helpers ───────────────────────────────────────────────────────────────

    def _get_pending(self, ctx: RunContext, force: bool) -> list[tuple[str, str]]:
        """Return [(file_path, ext)] rows needing forge."""
        if force:
            rows = ctx.conn.execute(
                "SELECT file_path, ext FROM archive WHERE status='CATALOGUED' ORDER BY artist, album, track"
            ).fetchall()
        else:
            rows = ctx.conn.execute(
                """
                SELECT file_path, ext FROM archive
                 WHERE status='CATALOGUED'
                   AND (rg_tagged_at IS NULL OR rg_tagged_at = '')
                 ORDER BY artist, album, track
                """
            ).fetchall()
        return [(r["file_path"], r["ext"] or "") for r in rows]

    def _process_one(
        self, ctx: RunContext, file_path: str, dry_run: bool, target_lufs: float
    ) -> str:
        """
        Measure + tag one file.
        Returns: 'ok' | 'silence' | 'json_fail' | 'ffmpeg_fail' | 'tag_fail' | 'missing'
        """
        path = Path(file_path)
        lufs, tp, reason = measure_loudness(path)

        if reason != "ok":
            return reason

        rg_gain = lufs_to_rg(lufs, reference=target_lufs)       # type: ignore[arg-type]
        r128_gain = lufs_to_rg(lufs, reference=R128_APPLE_REFERENCE)  # type: ignore[arg-type]
        rg_peak = dbtp_to_linear(tp)                                   # type: ignore[arg-type]

        tagged = False
        if not dry_run:
            tagged = write_rg_tags(path, rg_gain, rg_peak, r128_gain=r128_gain)
            _save_loudness(ctx, file_path, lufs, tp, rg_gain, rg_peak, tagged)   # type: ignore[arg-type]

        return "ok" if tagged or dry_run else "tag_fail"

    # ── run ───────────────────────────────────────────────────────────────────

    def run(self, ctx: RunContext) -> StageResult:
        result = self._make_result(dry_run=False)
        force: bool = ctx.get("forge_force", False)
        target_lufs: float = ctx.get("forge_target_lufs", R128_REFERENCE)

        pending = self._get_pending(ctx, force)

        total = len(pending)
        result.notes.append(f"files to forge: {total}")
        result.notes.append(f"target LUFS: {target_lufs}")
        if not total:
            result.notes.append("nothing to do — all CATALOGUED files already forged")
            ctx.record_stage(result)
            return result

        counters: dict[str, int] = {
            "ok": 0, "silence": 0, "json_fail": 0,
            "ffmpeg_fail": 0, "tag_fail": 0, "missing": 0,
        }

        for i, (fp, _ext) in enumerate(pending, 1):
            status = self._process_one(ctx, fp, dry_run=False, target_lufs=target_lufs)
            counters[status] = counters.get(status, 0) + 1
            result.files_processed += 1

            if status == "ok":
                result.files_changed += 1
            elif status in ("silence", "missing"):
                result.files_skipped += 1
            else:
                result.files_errored += 1

            if i % _COMMIT_EVERY == 0:
                ctx.conn.commit()
                logger.info("forge: checkpoint %d/%d", i, total)

        ctx.conn.commit()

        # Summarise in notes
        for k, v in counters.items():
            if v:
                result.notes.append(f"  {k}: {v}")

        if counters.get("ffmpeg_fail", 0) + counters.get("tag_fail", 0) > 0:
            result.success = False

        ctx.record_stage(result)
        return result

    # ── dry_run ───────────────────────────────────────────────────────────────

    def dry_run(self, ctx: RunContext) -> StageResult:
        result = self._make_result(dry_run=True)
        force: bool = ctx.get("forge_force", False)
        target_lufs: float = ctx.get("forge_target_lufs", R128_REFERENCE)

        pending = self._get_pending(ctx, force)
        total = len(pending)

        result.files_processed = total
        result.notes.append(f"[DRY RUN] would measure {total} file(s)")
        result.notes.append(f"[DRY RUN] target LUFS: {target_lufs}")
        result.notes.append("  no tags will be written, no DB changes")

        # Peek at a sample to show what LUFS values look like
        sample = pending[:5]
        for fp, _ in sample:
            lufs, tp, reason = measure_loudness(Path(fp))
            if reason == "ok":
                rg = lufs_to_rg(lufs, reference=target_lufs)   # type: ignore[arg-type]
                result.notes.append(f"  {Path(fp).name[:60]}  {lufs:.1f} LUFS → RG {rg:+.1f} dB")
            else:
                result.notes.append(f"  {Path(fp).name[:60]}  [{reason}]")

        ctx.record_stage(result)
        return result
