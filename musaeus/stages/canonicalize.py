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
import os
import sqlite3
import subprocess
from pathlib import Path

from ..config import LOSSLESS_CODECS as _LOSSLESS_CODECS
from ..context import RunContext, StageResult
from ..safety.mutation import MutationBoundary, PreconditionError, UnmanagedPathError
from ..safety.recovery import (
    JOURNAL_FILENAME,
    CollisionError,
    OperationJournal,
    create_checkpoint,
)
from .base import BaseStage, StageError

logger = logging.getLogger(__name__)

_COMMIT_EVERY = 25

# Real codec names as reported by ffprobe's codec_name field (Scholar's
# archive.codec), NOT file extensions.
_ALAC_CODECS = frozenset({"alac"})
_AAC_CODECS = frozenset({"aac"})

# Sub-lossless codecs this stage knows how to normalise into 256k AAC.
#
# An explicit list, because the alternative -- "anything not recognised gets
# transcoded" -- meant an unidentified file was lossily re-encoded by
# default. A destructive operation must be opted into by name.
_TRANSCODABLE_CODECS: frozenset[str] = frozenset(
    {
        "aac",
        "mp3",
        "vorbis",
        "opus",
        "wmav1",
        "wmav2",
        "ac3",
        "eac3",
        "musepack",
        "mp2",
        "amrnb",
        "amrwb",
    }
)

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

    # If the source is ALREADY AAC, copy the stream instead of re-encoding.
    #
    # The docstring above has always promised "a cheap remux ... when it
    # already is", but the command was `-c:a aac -b:a 256k` unconditionally,
    # so AAC in a .mp4/.m4b/.aac container -- which needs nothing but a new
    # container -- got a generation-two lossy re-encode. The same harm this
    # stage was just changed to stop defaulting to, left standing one branch
    # over. A stream copy is bit-exact.
    _src_audio = [st for st in probe.get("streams", []) if st.get("codec_type") == "audio"]
    _already_aac = (
        bool(_src_audio)
        and str(_src_audio[0].get("codec_name", "")).lower() == "aac"
    )
    audio_args = (
        ["-c:a", "copy"]
        if _already_aac
        else ["-c:a", "aac", "-b:a", AAC_TRANSCODE_BITRATE]
    )

    cmd = ["ffmpeg", "-y" if output.exists() else "-n", "-i", str(source), "-threads", "2"]
    if has_art:
        cmd += [
            "-map",
            "0:a:0",
            "-map",
            "0:v:0",
            *audio_args,
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
            *audio_args,
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
    Append one row to config.tunemymusic_csv_path, as Title,Artist,Album.

    The header was previously ORPHEUS's diagnostic one --
    reason,codec,bitrate_kbps,sample_rate,channels,duration_sec,path --
    which carries no artist and no title. That made the file unusable for
    the one job its name claims: importing these tracks into a streaming
    service to re-source them. Uploading it searched for a file path.

    It now matches the consolidated playlist batches
    (~/Desktop/Playlists_Consolidated/batch_*.csv), so the two are
    interchangeable at the import step. The diagnostic fields remain
    recoverable from the archive row and the event log; the identity of
    the track is not, which is why that is what gets written here.
    """
    csv_path = ctx.config.tunemymusic_csv_path
    is_new = not csv_path.exists()
    csv_path.parent.mkdir(parents=True, exist_ok=True)

    title = (row.get("title") or "").strip()
    artist = (row.get("artist") or "").strip()
    album = (row.get("album") or "").strip()
    if not title:
        # Last resort: the archive row should carry these, but a row with
        # no title at all is worse than one parsed from its filename.
        stem = Path(str(row.get("file_path") or "")).stem
        parsed_artist, sep, parsed_title = stem.partition(" - ")
        if sep:
            title = parsed_title.strip() or stem
            artist = artist or parsed_artist.strip()
        else:
            # No separator: the whole stem is the title. Treating it as the
            # artist too -- which the first version did -- writes rows like
            # "surround,surround", an identity that is wrong twice.
            title = stem
    if not title:
        return

    with open(csv_path, "a", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        if is_new:
            writer.writerow(["Title", "Artist", "Album"])
        writer.writerow([title, artist, album])


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
            SELECT a.file_path, a.audio_hash, a.duration
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

            # Duration, added 2026-09-01. Codec + hash + existence all pass
            # for a TRUNCATED conversion: right format, right identity, half
            # the audio. Four masters were found that same day with intact
            # container headers over missing audio -- a 5-minute song
            # decoding to 55 seconds -- and none of them would have been
            # caught by the three checks above.
            #
            # Read off the audio stream, not the container: a truncated file
            # frequently keeps a correct-looking format-level duration,
            # which is exactly what made those four invisible.
            recorded = r["duration"]
            if recorded and recorded > 0:
                actual = next(
                    (
                        float(s2["duration"])
                        for s2 in probe.get("streams", [])
                        if s2.get("codec_type") == "audio" and s2.get("duration")
                    ),
                    None,
                )
                if actual is not None and abs(actual - recorded) > max(2.0, recorded * 0.02):
                    problems.append(
                        f"{p.name}: {actual:.1f}s after canonicalize but "
                        f"{recorded:.1f}s recorded — conversion truncated the audio"
                    )
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

    @staticmethod
    def _resolve_codec(row: dict, source: Path | None) -> str:
        """The codec of this file: the row's if it has one, else ffprobe's.

        Shared by the decision and by the refusal message, so the operator is
        told the codec that was actually determined rather than an empty DB
        column.
        """
        codec = (row.get("codec") or "").lower()
        if codec or source is None or not source.exists():
            return codec
        try:
            probe = _probe_streams(source)
            audio = [
                st for st in probe.get("streams", []) if st.get("codec_type") == "audio"
            ]
            if audio:
                return str(audio[0].get("codec_name") or "").lower()
        except Exception as exc:  # a probe failure is not a licence to re-encode
            logger.warning("[canonicalize] codec probe failed for %s: %s", source, exc)
        return ""

    def _decide_action(self, row: dict, source: Path | None = None) -> str:
        """Return 'PASSTHROUGH' | 'CONVERT' | 'TRANSCODE' | 'UNKNOWN'.

        The decision is which of two irreversible things to do to real audio,
        so it is taken from the FILE where possible, not from the row.

        Two defects this closes, both reproduced 2026-08-29 against a real
        pipeline run in a disposable vault:

        `ext` was read from the DB column. It is a property of the path and
        the path is right here -- when the column was empty (as it is for
        every row whose ingest never populated it) an AAC-in-.m4a file, which
        the documented policy PASSES THROUGH, was classified TRANSCODE and
        given a needless 256k lossy re-encode. `alac` with an empty `ext`
        went to CONVERT and was re-converted to what it already was.

        Worse, the fall-through for an UNRECOGNISED codec was TRANSCODE --
        a lossy re-encode. "We do not know what this file is" therefore meant
        "re-encode it destructively". The live database holds 63 rows with no
        codec recorded at all. An unknown codec now returns UNKNOWN and the
        caller skips the row, leaving the audio untouched for a human. A
        default has to fail towards the reversible answer.
        """
        codec = self._resolve_codec(row, source)
        ext = (row.get("ext") or "").lower()

        # The path is the authority on the container.
        if source is not None:
            ext = source.suffix.lower()

        if codec in _ALAC_CODECS and ext == ".m4a":
            return "PASSTHROUGH"
        if codec in _AAC_CODECS and ext == ".m4a":
            return "PASSTHROUGH"
        if codec in _LOSSLESS_CODECS:
            return "CONVERT"
        if codec in _TRANSCODABLE_CODECS:
            return "TRANSCODE"
        return "UNKNOWN"

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

        action = self._decide_action(row, source)

        if action == "PASSTHROUGH":
            return "PASSTHROUGH", "already canonical codec/container"

        if action == "UNKNOWN":
            # Refusing is the answer. The alternative is a lossy re-encode of
            # a file nobody has identified.
            # Report the codec actually DETERMINED, not the DB column. For a
            # row whose codec was empty and got probed from the file, naming
            # the column says "(none recorded)" and hides the one fact needed
            # to act: which codec to add to a set.
            probed = self._resolve_codec(row, source)
            return "ERROR", (
                f"unrecognised codec {probed or '(none recorded)'!r} "
                f"for {source.name} -- refusing to re-encode blindly"
            )

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

    #: Set MUSAEUS_CANONICALIZE_CHECKPOINT=0 to run without a recovery
    #: boundary. An escape hatch, not a default: this stage destroys the
    #: only copy of a pre-conversion original.
    CHECKPOINT_ENV = "MUSAEUS_CANONICALIZE_CHECKPOINT"

    def _open_boundary(self, ctx: RunContext, result: StageResult):
        """Open a journalled mutation boundary for the disposal of originals.

        Scope is deliberate, and differs from finalize's. What this stage
        destroys is the pre-conversion INBOX original, and INBOX holds the
        whole incoming batch -- checkpointing it would mean copying every
        file the run is about to read, which is the same capacity argument
        that keeps finalize from checkpointing ALAC-Library.

        It does not need to. The protection here is not the checkpoint's
        payload, it is quarantine_item's contract: a move, never a delete,
        so the original's bytes still exist at a recorded location when
        disposal returns. The checkpoint is checkpointed over STAGING --
        this stage's own output area, empty or near-empty at entry, so the
        copy is cheap -- and serves as the quarantine container and journal
        anchor. Rolling back therefore means restoring each original from
        quarantine and clearing what was staged, which the journal alone
        supports.

        source_root spans the vault because the paths being disposed of
        live under INBOX while the quarantine area lives under RUNS, and
        both ends must validate.

        WEAKER THAN FINALIZE'S, DELIBERATELY. Do not read this as parity.
        MutationBoundary._expected_digest falls back to the checkpoint
        manifest and returns None for an item the manifest does not hold,
        so _check_precondition passes such an item straight through. The
        originals disposed of here live under INBOX and are therefore NOT
        in a STAGING checkpoint, which means they get no digest
        verification -- nothing here detects that an original changed
        underneath the run. What the boundary supplies for this stage is
        journaling and recoverability: the operation is durably recorded
        and the bytes are retrievable. finalize, whose sources ARE its
        checkpointed root, additionally gets the precondition check.

        Returns None when disabled or unavailable, and says which in the
        result -- a run with no boundary must announce itself rather than
        look identical to one that has it.
        """
        if os.environ.get(self.CHECKPOINT_ENV, "1").strip().lower() in ("0", "false", "no"):
            result.notes.append("recovery boundary: DISABLED by " + self.CHECKPOINT_ENV)
            return None
        try:
            recovery_root = ctx.config.runs_root / "recovery"
            recovery_root.mkdir(parents=True, exist_ok=True)
            ctx.staging.mkdir(parents=True, exist_ok=True)
            checkpoint = create_checkpoint(
                ctx.staging,
                recovery_root,
                checkpoint_id=f"canonicalize_{ctx.run_id}",
                capture_tags=False,
            )
            journal = OperationJournal(checkpoint.root / JOURNAL_FILENAME)
            boundary = MutationBoundary(
                checkpoint,
                journal,
                run_id=ctx.run_id,
                source_root=ctx.config.vault_root,
            )
            result.notes.append(
                f"recovery boundary: checkpoint {checkpoint.checkpoint_id} "
                f"(journal at {journal.path})"
            )
            return boundary
        except Exception as exc:
            result.notes.append(f"recovery boundary: UNAVAILABLE ({exc})")
            logger.warning("[canonicalize] no recovery boundary: %s", exc)
            return None

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
        boundary = self._open_boundary(ctx, result)

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

            if new_path != old_path:
                # The comment that stood here said the original is removed
                # only once "the DB write is confirmed". It was confirmed in
                # memory, not on disk. open_db() connects with sqlite3's
                # default deferred isolation -- not autocommit, unlike
                # state/migrator.py which sets isolation_level=None where it
                # wants that -- and the commit below fired only every
                # _COMMIT_EVERY rows. So up to 24 originals could be gone
                # while the row naming their replacement sat in an open
                # transaction. A kill in that window discarded the
                # transaction: original deleted, converted file an
                # unattributed orphan in STAGING, archive row still naming
                # the deleted path with canonicalized_at NULL, and the next
                # run erroring it as missing forever.
                #
                # So commit before disposing, per row. This stage is
                # ffmpeg-bound at seconds per file; a commit costs nothing
                # measurable against that.
                ctx.conn.commit()
                try:
                    if boundary is not None:
                        # A move into the checkpoint's quarantine area, never
                        # a delete -- the bytes still exist at a recorded
                        # location when this returns, and the journal says
                        # where. Same filesystem, so it is a rename: peak
                        # disk is unchanged from the pre-canonicalize state,
                        # it just isn't reclaimed until the run is released.
                        boundary.quarantine(
                            Path(old_path), reason=f"canonicalized to {new_path}"
                        )
                    else:
                        Path(old_path).unlink(missing_ok=True)
                except (
                    OSError,
                    UnmanagedPathError,
                    PreconditionError,
                    CollisionError,
                ) as exc:
                    # One row's problem, not the stage's -- finalize's
                    # 2026-08-25 lesson. The archive row already points at
                    # the verified staged copy and that write is durable, so
                    # the only consequence is an original left in place.
                    result.files_errored += 1
                    result.errors.append(
                        f"{Path(old_path).name}: original not disposed: {exc}"
                    )
                    logger.warning(
                        "[canonicalize] %s: original not disposed: %s", old_path, exc
                    )
                    continue
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

        counters: dict[str, int] = {
            "PASSTHROUGH": 0,
            "CONVERTED": 0,
            "TRANSCODED": 0,
            # A refusal is its own outcome, not a transcode.
            "REFUSED (unrecognised codec)": 0,
        }
        for row in pending:
            # The SAME inputs run() uses. Passing only the row made the
            # preview disagree with the run in both directions: an
            # AAC-in-.m4a row with an empty `ext` column previewed as a 256k
            # lossy TRANSCODE that run() actually passes through, and an
            # UNKNOWN codec -- which run() REFUSES -- previewed as
            # TRANSCODED because the ternary had no branch for it. A preview
            # of an irreversible operation has to be the operation's own
            # answer, not an approximation of it.
            action = self._decide_action(row, Path(row["file_path"]))
            key = {
                "PASSTHROUGH": "PASSTHROUGH",
                "CONVERT": "CONVERTED",
                "TRANSCODE": "TRANSCODED",
                "UNKNOWN": "REFUSED (unrecognised codec)",
            }[action]
            counters[key] += 1

        for k, v in counters.items():
            if v:
                result.notes.append(f"  would be {k}: {v}")
        result.notes.append("  no files will be written, no DB changes")

        ctx.record_stage(result)
        return result
