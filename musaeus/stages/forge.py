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

Tag-read-first shortcut (added 2026-08-19, matching bpm.py's existing
pattern): a candidate row can reach this stage with rg_tagged_at NULL
purely because the DB was reset/rebuilt while the file itself already
carries loudness tags from an earlier MUSAEUS or ORPHEUS pass -- that's
exactly what a DB wipe does to already-forged files re-ingested from
INBOX. Before spending real ffmpeg time re-measuring, read_existing_rg_tags()
checks the file's own embedded tag first (com.apple.iTunes.R128_TRACK_GAIN
for M4A, REPLAYGAIN_TRACK_GAIN/PEAK for FLAC/MP3/AIFF) and, if present,
recovers `lufs` from it directly -- lufs is a physical property of the
audio, not of any particular reference level, so this stays correct even
if --target-lufs differs from whatever reference produced the original
tag. Skipped via --retag, same escape hatch as bpm.py's --retag. WAV has
no standard RG tag container (see write_rg_tags below) so it always
falls through to a real ffmpeg measurement -- there's nothing to read.
M4A's R128_TRACK_GAIN atom carries gain only, not true-peak, so a
tag-shortcut hit leaves lufs_tp/rg_peak NULL in the DB rather than
guessing a value ffmpeg never actually measured.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

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

_COMMIT_EVERY = 25  # commit DB every N files (crash resilience)


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

        audio: Any
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
        assert audio.tags is not None
        audio.tags["TXXX:replaygain_track_gain"] = __import__(
            "mutagen.id3", fromlist=["TXXX"]
        ).TXXX(encoding=3, desc="replaygain_track_gain", text=f"{rg_gain:+.2f} dB")
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
    return True  # not a failure — we just don't embed


# ── Tag readers (skip ffmpeg if already tagged) ─────────────────────────────


def read_existing_rg_tags(path: Path) -> dict[str, float | None] | None:
    """
    Read already-embedded loudness info without invoking ffmpeg.

    Returns {"lufs", "lufs_tp", "rg_gain", "rg_peak"} (lufs_tp/rg_peak may
    be None when the tag container doesn't carry true-peak, e.g. Apple's
    R128_TRACK_GAIN) or None if no usable tag is present.
    """
    ext = path.suffix.lower()
    try:
        if ext in (".m4a", ".alac"):
            from mutagen.mp4 import MP4  # type: ignore[import-untyped]

            audio = MP4(str(path))
            raw = audio.get("com.apple.iTunes.R128_TRACK_GAIN")
            if not raw:
                return None
            r128_gain = int(raw[0]) / 256.0  # Q7.8 fixed-point, dB @ -23 LUFS
            lufs = R128_APPLE_REFERENCE - r128_gain
            return {
                "lufs": lufs,
                "lufs_tp": None,
                "rg_gain": lufs_to_rg(lufs, reference=R128_REFERENCE),
                "rg_peak": None,
            }

        if ext == ".flac":
            from mutagen.flac import FLAC  # type: ignore[import-untyped]

            return _rg_dict_from_vorbis_style(FLAC(str(path)))

        if ext == ".mp3":
            from mutagen.easyid3 import EasyID3  # type: ignore[import-untyped]

            return _rg_dict_from_vorbis_style(EasyID3(str(path)))

        # AIFF's gain-only TXXX tag and WAV's lack of any standard RG
        # container both mean there's nothing reliable to shortcut from.
        return None
    except Exception as exc:
        logger.debug("read_existing_rg_tags failed for %s: %s", path, exc)
        return None


def _rg_dict_from_vorbis_style(audio: Any) -> dict[str, float | None] | None:
    """Shared parser for FLAC/MP3's ReplayGain-2-style text tags."""
    gain_tag = audio.get("replaygain_track_gain")
    if not gain_tag:
        return None
    rg_gain = float(str(gain_tag[0]).replace("dB", "").strip())

    ref_tag = audio.get("replaygain_reference_loudness")
    reference = -float(str(ref_tag[0]).replace("LUFS", "").strip()) if ref_tag else R128_REFERENCE
    lufs = reference - rg_gain

    peak_tag = audio.get("replaygain_track_peak")
    rg_peak = float(str(peak_tag[0])) if peak_tag else None

    return {"lufs": lufs, "lufs_tp": None, "rg_gain": rg_gain, "rg_peak": rg_peak}


# ── DB helpers ────────────────────────────────────────────────────────────────


def _save_loudness(
    ctx: RunContext,
    file_path: str,
    lufs: float,
    lufs_tp: float | None,
    rg_gain: float,
    rg_peak: float | None,
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
    Use ctx.set("forge_retag", True) to skip the tag-read shortcut and
    always re-measure via ffmpeg, even for files with a usable embedded tag.
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
            raise StageError("mutagen not installed — run: pip install mutagen") from None

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
        self,
        ctx: RunContext,
        file_path: str,
        dry_run: bool,
        target_lufs: float,
        retag: bool = False,
    ) -> str:
        """
        Measure + tag one file.
        Returns: 'ok' | 'tag_shortcut' | 'silence' | 'json_fail' | 'ffmpeg_fail'
                 | 'tag_fail' | 'missing'
        """
        path = Path(file_path)

        if not retag:
            existing = read_existing_rg_tags(path)
            if existing is not None:
                if not dry_run:
                    lufs_v = existing["lufs"]
                    rg_gain_v = existing["rg_gain"]
                    assert lufs_v is not None and rg_gain_v is not None
                    _save_loudness(
                        ctx,
                        file_path,
                        lufs_v,
                        existing["lufs_tp"],
                        rg_gain_v,
                        existing["rg_peak"],
                        tagged=True,
                    )
                return "tag_shortcut"

        lufs, tp, reason = measure_loudness(path)

        if reason != "ok":
            return reason

        rg_gain = lufs_to_rg(lufs, reference=target_lufs)  # type: ignore[arg-type]
        r128_gain = lufs_to_rg(lufs, reference=R128_APPLE_REFERENCE)  # type: ignore[arg-type]
        rg_peak = dbtp_to_linear(tp)  # type: ignore[arg-type]

        tagged = False
        if not dry_run:
            tagged = write_rg_tags(path, rg_gain, rg_peak, r128_gain=r128_gain)
            _save_loudness(ctx, file_path, lufs, tp, rg_gain, rg_peak, tagged)  # type: ignore[arg-type]

        return "ok" if tagged or dry_run else "tag_fail"

    # ── run ───────────────────────────────────────────────────────────────────

    def run(self, ctx: RunContext) -> StageResult:
        result = self._make_result(dry_run=False)
        force: bool = ctx.get("forge_force", False)
        retag: bool = ctx.get("forge_retag", False)
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
            "ok": 0,
            "tag_shortcut": 0,
            "silence": 0,
            "json_fail": 0,
            "ffmpeg_fail": 0,
            "tag_fail": 0,
            "missing": 0,
        }

        for i, (fp, _ext) in enumerate(pending, 1):
            status = self._process_one(ctx, fp, dry_run=False, target_lufs=target_lufs, retag=retag)
            counters[status] = counters.get(status, 0) + 1
            result.files_processed += 1

            if status in ("ok", "tag_shortcut"):
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
        result.notes.append(
            f"  (measured via ffmpeg: {counters['ok']}, from existing tags: {counters['tag_shortcut']})"
        )

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
        result.notes.append("  (LUFS values shown on live run only — dry_run skips ffmpeg)")

        ctx.record_stage(result)
        return result
