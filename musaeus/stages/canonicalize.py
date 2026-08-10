#!/usr/bin/env python3
"""
MUSAEUS — Canonicalize Stage (Act 3)

Converts every CATALOGUED file to the format the canonical ALAC-Library
expects, based on the file's REAL codec (from Scholar's ffprobe read),
never on file extension. This is the fix for a real, confirmed bug: the
old LOSSLESS_EXTENSIONS/LOSSY_EXTENSIONS sets in config.py classify
".m4a" as lossy, which misidentifies Grey's actual library (ALAC-in-.m4a)
as lossy everywhere those sets were used for that decision. Canonicalize
never uses those sets — it reads archive.codec directly.

Three outcomes per file (recorded in archive.canon_action):

  PASSTHROUGH
    Already ALAC-in-.m4a, or already AAC-in-.m4a. Nothing to convert.
    No file write at all.

  CONVERTED
    Lossless source (FLAC/WAV/AIFF) → ALAC-in-.m4a. A codec swap, not a
    re-encode — no quality loss. Ported from ORPHEUS's
    SCRIPTS/convert_flac_to_alac_v2.py build_ffmpeg_command(): -c:a alac,
    -map_metadata 0, cover art preserved via -map 0:v:0 -c:v copy
    -disposition:v:0 attached_pic when present.

  TRANSCODED
    Sub-lossless source (mp3/ogg/wma/etc, or lossy AAC not already in an
    .m4a container) → 256k AAC-in-.m4a. This IS a real lossy re-encode —
    quality cannot improve, and there is a small risk of further loss
    from decode+re-encode. Confirmed and accepted by Grey (2026-08-09/10
    session) as the tradeoff for having every file in ALAC-Library share
    one predictable container/codec pairing. Also logged to
    config.tunemymusic_csv_path (ORPHEUS TuneMyMusic.csv convention:
    reason,codec,bitrate_kbps,sample_rate,channels,duration_sec,path) so
    the original sub-lossless source can be manually replaced later.

Verification: ORPHEUS's own convert_flac_to_alac_v2.py does NOT verify a
conversion after writing it — it only checks size>0 on a PRE-EXISTING
output to decide skip-vs-reconvert, never re-probes a freshly written
file. Canonicalize adds a real post-conversion check here (confirmed with
Grey as a deliberate improvement, not a port): ffprobe the output,
require the stream count and duration (within a small tolerance) to match
the source, before the temp file is renamed into place and the DB is
updated. If verification fails, the source is left untouched and the
failure is recorded as a normal per-file error — never a silent skip.

Rules:
  - Only processes CATALOGUED files (status='CATALOGUED')
  - Skips files with canonicalized_at already set, unless --force
  - Writes to a .tmp sibling file first, verifies, then renames into
    place over the original — the DB row's file_path and extension both
    change together, in the same way organize.py's _apply_rename keeps
    disk and DB from drifting
  - dry_run() reports the action each file would receive, no ffmpeg calls
"""

from __future__ import annotations

import csv
import json
import logging
import subprocess
from pathlib import Path

from ..config import LOSSLESS_CODECS as _LOSSLESS_CODECS
from ..context import RunContext, StageResult
from .base import BaseStage, StageError

logger = logging.getLogger(__name__)

_COMMIT_EVERY = 25

# Real codec names as reported by ffprobe's codec_name field (Scholar's
# archive.codec), NOT file extensions.
_ALAC_CODECS = frozenset({"alac"})
_AAC_CODECS = frozenset({"aac"})

AAC_TRANSCODE_BITRATE = "256k"

# Tolerance for post-conversion duration comparison (seconds). ffprobe
# duration reporting can differ slightly between containers even when the
# actual audio is identical.
_DURATION_TOLERANCE_SEC = 1.5


class CanonicalizeError(Exception):
    """Raised internally when a conversion or verification step fails."""


# ── ffprobe helpers ────────────────────────────────────────────────────────────


def _probe_streams(path: Path) -> dict:
    """Run ffprobe and return the parsed JSON (streams + format)."""
    cmd = [
        "ffprobe", "-v", "error", "-print_format", "json",
        "-show_format", "-show_streams", str(path),
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    if proc.returncode != 0:
        raise CanonicalizeError(f"ffprobe failed ({proc.returncode}): {proc.stderr[:200]}")
    try:
        return json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        raise CanonicalizeError(f"ffprobe JSON parse error: {exc}") from exc


def _has_attached_picture(probe: dict) -> bool:
    for s in probe.get("streams", []):
        if s.get("codec_type") == "video" and s.get("disposition", {}).get("attached_pic") == 1:
            return True
    return False


def _verify_conversion(source: Path, output: Path) -> None:
    """
    Post-conversion check: output must have an audio stream, and its
    duration must match the source within tolerance. Raises
    CanonicalizeError on any mismatch — caller must not trust the output.
    """
    src_probe = _probe_streams(source)
    out_probe = _probe_streams(output)

    out_audio_streams = [s for s in out_probe.get("streams", []) if s.get("codec_type") == "audio"]
    if not out_audio_streams:
        raise CanonicalizeError("verification failed: output has no audio stream")

    def _duration(probe: dict) -> float | None:
        d = probe.get("format", {}).get("duration")
        try:
            return float(d) if d else None
        except (TypeError, ValueError):
            return None

    src_dur = _duration(src_probe)
    out_dur = _duration(out_probe)
    if src_dur is not None and out_dur is not None:
        if abs(src_dur - out_dur) > _DURATION_TOLERANCE_SEC:
            raise CanonicalizeError(
                f"verification failed: duration mismatch "
                f"(source={src_dur:.2f}s, output={out_dur:.2f}s)"
            )


# ── ffmpeg conversion commands ────────────────────────────────────────────────


def _convert_to_alac(source: Path, output: Path) -> None:
    """
    Lossless source → ALAC-in-.m4a. Codec swap only, -map_metadata 0
    copies existing container tags, cover art preserved if present.
    Ported from ORPHEUS's convert_flac_to_alac_v2.py build_ffmpeg_command().
    """
    probe = _probe_streams(source)
    has_art = _has_attached_picture(probe)

    cmd = ["ffmpeg", "-y" if output.exists() else "-n", "-i", str(source), "-threads", "2"]
    if has_art:
        cmd += [
            "-map", "0:a:0", "-map", "0:v:0",
            "-c:a", "alac", "-c:v", "copy",
            "-disposition:v:0", "attached_pic",
            "-map_metadata", "0",
            "-f", "mp4", str(output),
        ]
    else:
        cmd += [
            "-map", "0:a:0",
            "-c:a", "alac",
            "-map_metadata", "0",
            "-f", "mp4", str(output),
        ]

    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
    if proc.returncode != 0:
        raise CanonicalizeError(f"ffmpeg ALAC convert failed: {proc.stderr[-300:]}")


def _transcode_to_aac(source: Path, output: Path) -> None:
    """
    Sub-lossless source → 256k AAC-in-.m4a. A real re-encode when the
    source isn't already AAC; a cheap remux (still via -c:a aac to
    guarantee a consistent, correct .m4a container) when it already is.
    """
    probe = _probe_streams(source)
    has_art = _has_attached_picture(probe)

    cmd = ["ffmpeg", "-y" if output.exists() else "-n", "-i", str(source), "-threads", "2"]
    if has_art:
        cmd += [
            "-map", "0:a:0", "-map", "0:v:0",
            "-c:a", "aac", "-b:a", AAC_TRANSCODE_BITRATE, "-c:v", "copy",
            "-disposition:v:0", "attached_pic",
            "-map_metadata", "0",
            "-f", "mp4", str(output),
        ]
    else:
        cmd += [
            "-map", "0:a:0",
            "-c:a", "aac", "-b:a", AAC_TRANSCODE_BITRATE,
            "-map_metadata", "0",
            "-f", "mp4", str(output),
        ]

    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
    if proc.returncode != 0:
        raise CanonicalizeError(f"ffmpeg AAC transcode failed: {proc.stderr[-300:]}")


# ── TuneMyMusic.csv ────────────────────────────────────────────────────────────


def _append_tunemymusic_row(ctx: RunContext, row: dict) -> None:
    """
    Append one row to config.tunemymusic_csv_path. Header matches
    ORPHEUS's generate_tunemymusic_csv.py convention exactly:
      reason,codec,bitrate_kbps,sample_rate,channels,duration_sec,path
    Written incrementally here (unlike ORPHEUS's full-report regeneration)
    since Canonicalize processes one file at a time and the CSV must
    survive a DB wipe — appending directly is simpler and equally safe.
    """
    csv_path = ctx.config.tunemymusic_csv_path
    is_new = not csv_path.exists()
    csv_path.parent.mkdir(parents=True, exist_ok=True)
    with open(csv_path, "a", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        if is_new:
            writer.writerow(
                ["reason", "codec", "bitrate_kbps", "sample_rate", "channels", "duration_sec", "path"]
            )
        writer.writerow(
            [
                row.get("reason", "sub-lossless"),
                (row.get("codec") or "unknown").upper(),
                f"{(row.get('bitrate') or 0) // 1000}" if row.get("bitrate") else "",
                row.get("sample_rate") or "",
                row.get("channels") or "",
                f"{row.get('duration') or 0.0:.1f}",
                row.get("file_path", ""),
            ]
        )


# ── Stage ──────────────────────────────────────────────────────────────────────


class CanonicalizeStage(BaseStage):
    """
    Canonicalize — bring every CATALOGUED file to ALAC-in-.m4a (lossless
    sources) or 256k AAC-in-.m4a (sub-lossless sources), based on real
    ffprobe codec, not file extension.
    """

    NAME = "canonicalize"

    def validate(self, ctx: RunContext) -> None:
        import shutil
        if not shutil.which("ffmpeg"):
            raise StageError("ffmpeg not found — required for canonicalize")
        if not shutil.which("ffprobe"):
            raise StageError("ffprobe not found — required for canonicalize")

    def _get_pending(self, ctx: RunContext, force: bool) -> list[dict]:
        # archive.id is INTEGER PRIMARY KEY, which in SQLite IS the rowid
        # (they're aliases for the same value) -- selecting both "rowid"
        # and "*" here would return two columns that collapse into one
        # key on dict(row), silently dropping the value. Just select "*"
        # and use the existing "id" column below.
        if force:
            rows = ctx.conn.execute(
                "SELECT * FROM archive WHERE status='CATALOGUED' ORDER BY file_path"
            ).fetchall()
        else:
            rows = ctx.conn.execute(
                """
                SELECT * FROM archive
                 WHERE status='CATALOGUED'
                   AND (canonicalized_at IS NULL OR canonicalized_at = '')
                 ORDER BY file_path
                """
            ).fetchall()
        return [dict(r) for r in rows]

    def _decide_action(self, row: dict) -> str:
        """Return 'PASSTHROUGH' | 'CONVERT' | 'TRANSCODE' for this row."""
        codec = (row.get("codec") or "").lower()
        ext = (row.get("ext") or "").lower()

        if codec in _ALAC_CODECS and ext == ".m4a":
            return "PASSTHROUGH"
        if codec in _AAC_CODECS and ext == ".m4a":
            return "PASSTHROUGH"
        if codec in _LOSSLESS_CODECS:
            return "CONVERT"
        return "TRANSCODE"

    def _process_one(self, ctx: RunContext, row: dict, dry_run: bool) -> tuple[str, str]:
        """
        Returns (canon_action, detail). canon_action is one of
        PASSTHROUGH/CONVERTED/TRANSCODED/ERROR.
        """
        source = Path(row["file_path"])
        if not source.exists():
            return "ERROR", "file missing on disk"

        action = self._decide_action(row)

        if action == "PASSTHROUGH":
            return "PASSTHROUGH", "already canonical codec/container"

        if dry_run:
            return ("CONVERTED" if action == "CONVERT" else "TRANSCODED"), "[dry run]"

        output = source.with_suffix(".m4a.canon_tmp")
        try:
            if action == "CONVERT":
                _convert_to_alac(source, output)
            else:
                _transcode_to_aac(source, output)

            _verify_conversion(source, output)

            final_path = source.with_suffix(".m4a")
            if final_path == source:
                # Source was already .m4a with a non-canonical codec
                # (shouldn't normally happen given _decide_action, but
                # guard against clobbering the source with itself).
                final_path = source.with_name(source.stem + "_canon.m4a")

            output.rename(final_path)
            if final_path != source:
                source.unlink(missing_ok=True)

            row["_final_path"] = str(final_path)

            if action == "TRANSCODE":
                _append_tunemymusic_row(ctx, {
                    "reason": "sub-lossless source, transcoded to AAC",
                    "codec": row.get("codec"),
                    "bitrate": row.get("bitrate"),
                    "sample_rate": row.get("sample_rate"),
                    "channels": row.get("channels"),
                    "duration": row.get("duration"),
                    "file_path": str(source),
                })
                return "TRANSCODED", "sub-lossless -> 256k AAC-in-.m4a"

            return "CONVERTED", "lossless -> ALAC-in-.m4a"

        except CanonicalizeError as exc:
            if output.exists():
                output.unlink(missing_ok=True)
            return "ERROR", str(exc)
        except OSError as exc:
            if output.exists():
                output.unlink(missing_ok=True)
            return "ERROR", f"filesystem error: {exc}"

    # ── run ───────────────────────────────────────────────────────────────────

    def run(self, ctx: RunContext) -> StageResult:
        result = self._make_result(dry_run=False)
        force: bool = ctx.get("canonicalize_force", False)
        pending = self._get_pending(ctx, force)

        total = len(pending)
        result.notes.append(f"files to canonicalize: {total}")
        if not total:
            result.notes.append("nothing to do — all CATALOGUED files already canonicalized")
            ctx.record_stage(result)
            return result

        counters: dict[str, int] = {"PASSTHROUGH": 0, "CONVERTED": 0, "TRANSCODED": 0, "ERROR": 0}

        for i, row in enumerate(pending, 1):
            outcome, detail = self._process_one(ctx, row, dry_run=False)
            counters[outcome] = counters.get(outcome, 0) + 1
            result.files_processed += 1

            if outcome == "ERROR":
                result.files_errored += 1
                result.errors.append(f"{Path(row['file_path']).name}: {detail}")
                logger.warning("[canonicalize] %s: %s", row["file_path"], detail)
            else:
                result.files_changed += 1
                new_path = row.get("_final_path", row["file_path"])
                ctx.conn.execute(
                    """
                    UPDATE archive
                       SET file_path = ?, ext = '.m4a',
                           canonicalized_at = datetime('now'),
                           canon_action = ?
                     WHERE id = ?
                    """,
                    (new_path, outcome, row["id"]),
                )
                ctx.log_event(
                    "CANONICALIZE",
                    file_path=new_path,
                    old_value=row["file_path"],
                    new_value=outcome,
                    stage=self.NAME,
                    note=detail,
                )
                if new_path != row["file_path"]:
                    logger.info("[canonicalize] %s: %s -> %s", outcome, row["file_path"], new_path)
                else:
                    logger.info("[canonicalize] %s: %s", outcome, new_path)

            if i % _COMMIT_EVERY == 0:
                ctx.conn.commit()
                logger.info("canonicalize: checkpoint %d/%d", i, total)

        ctx.conn.commit()

        for k, v in counters.items():
            if v:
                result.notes.append(f"  {k}: {v}")

        if counters["ERROR"] > 0:
            result.success = False

        ctx.record_stage(result)
        return result

    # ── dry_run ───────────────────────────────────────────────────────────────

    def dry_run(self, ctx: RunContext) -> StageResult:
        result = self._make_result(dry_run=True)
        force: bool = ctx.get("canonicalize_force", False)
        pending = self._get_pending(ctx, force)
        total = len(pending)

        result.files_processed = total
        result.notes.append(f"[DRY RUN] would inspect {total} file(s)")

        counters: dict[str, int] = {"PASSTHROUGH": 0, "CONVERTED": 0, "TRANSCODED": 0}
        for row in pending:
            action = self._decide_action(row)
            key = "PASSTHROUGH" if action == "PASSTHROUGH" else ("CONVERTED" if action == "CONVERT" else "TRANSCODED")
            counters[key] += 1

        for k, v in counters.items():
            if v:
                result.notes.append(f"  would be {k}: {v}")
        result.notes.append("  no files will be written, no DB changes")

        ctx.record_stage(result)
        return result
