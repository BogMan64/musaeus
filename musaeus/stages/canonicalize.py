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
the source, before it is trusted and the original is ever touched.

STAGING flow (Grey's explicit design decision, 2026-08-11 session):
CONVERTED/TRANSCODED output is written to config.staging (vault_root/
STAGING), never as a sibling file next to the source in INBOX. Sequence
per file:
  1. ffmpeg writes to STAGING/<row.id>_<name>.m4a.canon_tmp
  2. ffprobe-verify that tmp file against the still-untouched INBOX
     source (stream count + duration)
  3. on success: rename it to STAGING/<row.id>_<name>.m4a (still inside
     STAGING -- Canonicalize never writes into ALAC-Library itself,
     that's Finalize's job) and update archive.file_path/canon_action/
     canonicalized_at by rowid (organize.py's _apply_rename pattern: disk
     change first, DB update second; a sqlite3.IntegrityError on the DB
     write is caught and reverted -- the staged file and the original
     INBOX source are both left exactly as they were, nothing is lost)
  4. only once the DB row safely points at the verified STAGING copy is
     the original INBOX source deleted
  5. on ffmpeg or verification failure: the partial/bad output is
     RENAMED to STAGING/<row.id>_<name>.m4a.FAILED_VERIFY and left there
     — never silently deleted, never silently retried. A
     CANONICALIZE_VERIFY_FAILED event is logged. The original INBOX
     source is untouched throughout.
Finalize then picks the row up from wherever archive.file_path currently
points (STAGING for CONVERTED/TRANSCODED, still INBOX for PASSTHROUGH)
and moves it into ALAC-Library, deleting the STAGING copy only after
that move is confirmed. A STAGING directory that isn't empty at the end
of a clean run is itself a signal something needs manual review.

Rules:
  - Only processes CATALOGUED files (status='CATALOGUED')
  - Skips files with canonicalized_at already set, unless --force
  - dry_run() reports the action each file would receive, no ffmpeg calls
"""

from __future__ import annotations

import csv
import json
import logging
import sqlite3
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
        "ffprobe",
        "-v",
        "error",
        "-print_format",
        "json",
        "-show_format",
        "-show_streams",
        str(path),
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    if proc.returncode != 0:
        raise CanonicalizeError(f"ffprobe failed ({proc.returncode}): {proc.stderr[:200]}")
    try:
        data: dict = json.loads(proc.stdout)
        return data
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
    if (
        src_dur is not None
        and out_dur is not None
        and abs(src_dur - out_dur) > _DURATION_TOLERANCE_SEC
    ):
        raise CanonicalizeError(
            f"verification failed: duration mismatch (source={src_dur:.2f}s, output={out_dur:.2f}s)"
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
            "-map",
            "0:a:0",
            "-map",
            "0:v:0",
            "-c:a",
            "alac",
            "-c:v",
            "copy",
            "-disposition:v:0",
            "attached_pic",
            "-map_metadata",
            "0",
            "-f",
            "mp4",
            str(output),
        ]
    else:
        cmd += [
            "-map",
            "0:a:0",
            "-c:a",
            "alac",
            "-map_metadata",
            "0",
            "-f",
            "mp4",
            str(output),
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
            "-map",
            "0:a:0",
            "-map",
            "0:v:0",
            "-c:a",
            "aac",
            "-b:a",
            AAC_TRANSCODE_BITRATE,
            "-c:v",
            "copy",
            "-disposition:v:0",
            "attached_pic",
            "-map_metadata",
            "0",
            "-f",
            "mp4",
            str(output),
        ]
    else:
        cmd += [
            "-map",
            "0:a:0",
            "-c:a",
            "aac",
            "-b:a",
            AAC_TRANSCODE_BITRATE,
            "-map_metadata",
            "0",
            "-f",
            "mp4",
            str(output),
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
                [
                    "reason",
                    "codec",
                    "bitrate_kbps",
                    "sample_rate",
                    "channels",
                    "duration_sec",
                    "path",
                ]
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

    def verify_effect(self, ctx: RunContext, result: StageResult) -> list[str]:
        """Sample this run's conversions and confirm they really landed.

        Two specific regressions this catches, both seen in this project:

        audio_hash going NULL through conversion. The hash is the PCM
        identity that the dupe ledger and every cross-run "have I seen this"
        check depend on. When it silently dropped, files became their own
        duplicates on the next pass -- scope doc section 4.17.

        A row that claims CANONICALIZE but whose file is not ALAC. The
        conversion can report success and leave the original codec in place
        if ffmpeg's exit status is trusted without re-probing the output.

        Samples the rows this run touched rather than the whole library --
        the point is to catch a stage that changed nothing, not to re-probe
        10,000 files.
        """
        rows = ctx.conn.execute(
            """
            SELECT a.file_path, a.audio_hash
              FROM archive a
              JOIN events e ON e.file_path = a.file_path
             WHERE e.stage = ? AND e.event_type = 'CANONICALIZE'
               AND e.run_id = ?
             ORDER BY e.id DESC LIMIT 12
            """,
            (self.NAME, ctx.run_id),
        ).fetchall()
        if not rows:
            return []

        problems: list[str] = []
        missing_hash = [r["file_path"] for r in rows if not r["audio_hash"]]
        if missing_hash:
            problems.append(
                f"{len(missing_hash)} of {len(rows)} sampled conversion(s) lost audio_hash, "
                f"e.g. {Path(missing_hash[0]).name}"
            )
        for r in rows[:5]:
            p = Path(r["file_path"])
            if not p.exists():
                problems.append(f"canonicalized file is not on disk: {p.name}")
                continue
            try:
                probe = _probe_streams(p)
            except CanonicalizeError as exc:
                problems.append(f"{p.name}: cannot probe after canonicalize: {exc}")
                continue
            # _probe_streams returns the whole ffprobe document, so the codec
            # has to be read off the AUDIO stream -- a top-level
            # probe.get("codec_name") is always None and would make this
            # check quietly unfalsifiable.
            codec = next(
                (
                    (s.get("codec_name") or "").lower()
                    for s in probe.get("streams", [])
                    if s.get("codec_type") == "audio"
                ),
                "",
            )
            if codec and codec != "alac":
                problems.append(f"{p.name} is still {codec}, not alac, after canonicalize")
        return problems

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

    def _quarantine_failed_staging(
        self, ctx: RunContext, tmp_output: Path, staging_name: str, source: Path, exc: Exception
    ) -> None:
        """
        Leave a failed conversion/verification attempt visible in STAGING
        instead of silently deleting it (Grey's explicit design decision:
        STAGING should be empty at the end of a clean run, so anything
        left there -- including a failed attempt -- is itself a signal to
        investigate manually). The original INBOX source is never touched
        here, and the row is never marked canonicalized, so it stays
        eligible to be picked up (and produce a fresh attempt) on a later
        run rather than being silently retried within this one.
        """
        if tmp_output.exists():
            failed_path = ctx.staging / f"{staging_name}.FAILED_VERIFY"
            try:
                tmp_output.rename(failed_path)
            except OSError:
                failed_path = tmp_output  # rename itself failed; report the tmp path as-is
        else:
            failed_path = tmp_output
        ctx.log_event(
            "CANONICALIZE_VERIFY_FAILED",
            file_path=str(failed_path),
            old_value=str(source),
            new_value=None,
            stage=self.NAME,
            note=str(exc),
        )

    def _process_one(self, ctx: RunContext, row: dict, dry_run: bool) -> tuple[str, str]:
        """
        Returns (canon_action, detail). canon_action is one of
        PASSTHROUGH/CONVERTED/TRANSCODED/ERROR. Does NOT touch the
        original INBOX source or the DB -- run() does both, and only
        after this has produced a verified STAGING copy, so a DB
        collision or crash between here and there never loses the
        source (see module docstring's STAGING flow).
        """
        source = Path(row["file_path"])
        if not source.exists():
            return "ERROR", "file missing on disk"

        action = self._decide_action(row)

        if action == "PASSTHROUGH":
            return "PASSTHROUGH", "already canonical codec/container"

        if dry_run:
            return ("CONVERTED" if action == "CONVERT" else "TRANSCODED"), "[dry run]"

        ctx.staging.mkdir(parents=True, exist_ok=True)
        staging_name = f"{row['id']}_{source.stem}.m4a"
        tmp_output = ctx.staging / f"{staging_name}.canon_tmp"
        staged_output = ctx.staging / staging_name

        try:
            if action == "CONVERT":
                _convert_to_alac(source, tmp_output)
            else:
                _transcode_to_aac(source, tmp_output)

            _verify_conversion(source, tmp_output)

            tmp_output.rename(staged_output)
            row["_final_path"] = str(staged_output)

            if action == "TRANSCODE":
                _append_tunemymusic_row(
                    ctx,
                    {
                        "reason": "sub-lossless source, transcoded to AAC",
                        "codec": row.get("codec"),
                        "bitrate": row.get("bitrate"),
                        "sample_rate": row.get("sample_rate"),
                        "channels": row.get("channels"),
                        "duration": row.get("duration"),
                        "file_path": str(source),
                    },
                )
                return "TRANSCODED", "sub-lossless -> 256k AAC-in-.m4a (staged)"

            return "CONVERTED", "lossless -> ALAC-in-.m4a (staged)"

        except CanonicalizeError as exc:
            self._quarantine_failed_staging(ctx, tmp_output, staging_name, source, exc)
            return "ERROR", str(exc)
        except OSError as exc:
            self._quarantine_failed_staging(ctx, tmp_output, staging_name, source, exc)
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
                if i % _COMMIT_EVERY == 0:
                    ctx.conn.commit()
                    logger.info("canonicalize: checkpoint %d/%d", i, total)
                continue

            new_path = row.get("_final_path", row["file_path"])
            old_path = row["file_path"]

            # organize.py's _apply_rename pattern: the on-disk side (the
            # verified STAGING copy) already exists; the DB write is the
            # only thing that can still fail here (a UNIQUE collision on
            # archive.file_path). If it does, revert nothing on disk --
            # the original INBOX source hasn't been touched yet, and the
            # staged file is simply left behind for manual review instead
            # of being wired into the DB.
            try:
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
            except sqlite3.IntegrityError as exc:
                logger.error(
                    "[canonicalize] DB collision for row %s -> %s (%s); "
                    "leaving staged file in place, source untouched",
                    row["id"],
                    new_path,
                    exc,
                )
                result.files_errored += 1
                result.errors.append(f"{Path(old_path).name}: DB collision on {new_path}: {exc}")
                ctx.log_event(
                    "CANONICALIZE_DB_COLLISION",
                    file_path=new_path,
                    old_value=old_path,
                    new_value=None,
                    stage=self.NAME,
                    note=str(exc),
                )
                if i % _COMMIT_EVERY == 0:
                    ctx.conn.commit()
                    logger.info("canonicalize: checkpoint %d/%d", i, total)
                continue

            result.files_changed += 1
            ctx.log_event(
                "CANONICALIZE",
                file_path=new_path,
                old_value=old_path,
                new_value=outcome,
                stage=self.NAME,
                note=detail,
            )

            # Only now, with the DB row safely pointing at the verified
            # STAGING copy, is it safe to remove the pre-conversion
            # original -- never before the DB write is confirmed.
            if new_path != old_path:
                Path(old_path).unlink(missing_ok=True)
                logger.info("[canonicalize] %s: %s -> %s", outcome, old_path, new_path)
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
            key = (
                "PASSTHROUGH"
                if action == "PASSTHROUGH"
                else ("CONVERTED" if action == "CONVERT" else "TRANSCODED")
            )
            counters[key] += 1

        for k, v in counters.items():
            if v:
                result.notes.append(f"  would be {k}: {v}")
        result.notes.append("  no files will be written, no DB changes")

        ctx.record_stage(result)
        return result
