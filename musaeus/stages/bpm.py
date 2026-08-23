#!/usr/bin/env python3
"""
MUSAEUS — BPM Stage (wired into DEFAULT_PIPELINE, after Finalize)

Extracts BPM, musical key, energy, and danceability for every CATALOGUED
file and writes them back both into the archive table and into the file's
own tags. Ported from ORPHEUS's orpheus_audio_analyzer.py (Essentia-based
reference implementation), per Grey's recommended placement: late in the
pipeline, near Forge, after Finalize.

Initially built standalone (GhostStage/PermissionsStage precedent) over
a cost concern: Essentia analysis is genuinely heavy (multi-second
full-track decode + several DSP passes per file), and essentia itself is
a large optional dependency (pyproject.toml's `bpm` extra, not installed
by default or in CI). Wired into DEFAULT_PIPELINE 2026-08-19 after Grey
corrected that framing: the tag-read-first shortcut below plus
bpm_analyzed_at resumability mean that cost is paid once per new file,
ever -- not on every pipeline run -- so the original objection didn't
actually hold.

Design differences from the ORPHEUS original:
  - DB-row-driven (archive table, status='CATALOGUED'), not a directory
    walk -- matches every other MUSAEUS stage's convention rather than
    ORPHEUS's RUNS/Music tree scan.
  - Resumability via bpm_analyzed_at (nullable timestamp, same pattern
    as canonicalized_at/finalized_at/lufs_baked_at), not a separate
    mtime-tracking table.
  - Sequential, no ThreadPoolExecutor -- matches ForgeStage's own
    "already CPU-heavy, no threading" convention. ORPHEUS's parallel
    design existed specifically to manage the OOM risk multiple
    concurrent Essentia workers create; running sequentially sidesteps
    that risk entirely rather than needing to re-implement the RAM/swap
    worker-capping logic to manage it.
  - Brightness dropped: measured by the original but never written to
    tags (DB-only, nothing downstream reads it) -- out of scope for
    "BPM tagging" as asked.
  - Tag-read-first shortcut kept: if a file already has a BPM tag
    (e.g. from a prior ORPHEUS pass), record it into the DB directly
    and skip the expensive Essentia analysis entirely, unless --retag.
"""

from __future__ import annotations

import csv
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ..context import RunContext, StageResult
from .base import BaseStage, StageError
from .canonicalize import _append_tunemymusic_row

logger = logging.getLogger(__name__)

_COMMIT_EVERY = 25

#: Longest span fed to the beat tracker. Six minutes clears essentially every
#: song in the library, so normal tracks are analysed whole and their BPM is
#: unchanged; only long-form material takes the excerpt path. See the note at
#: the RhythmExtractor2013 call for why this is a length cap and not a timeout.
_RHYTHM_MAX_SECONDS = 360
_RHYTHM_MAX_SAMPLES = _RHYTHM_MAX_SECONDS * 44100

#: Hard ceiling on what will be decoded at all. Above this the file is not a
#: track and loading it is itself the hazard -- see the note in
#: _process_one(). 45 min clears the longest real music in the library
#: (Miles Davis, 28.5 min) with room to spare, and caps a decode at ~475 MB.
_MAX_ANALYSIS_SECONDS = 45 * 60

_SKIP_ERROR_PATTERNS = (
    "too short",
    "not enough audio",
    "insufficient frames",
    "empty signal",
    "output buffer is full",
    "could not push",
    "onsetdetectionglobal",
)

# Essentia's MonoLoader can't decode anything but mono/stereo -- confirmed
# 2026-08-19/20 against the real USB2 backlog (5.1 surround mixes, mostly
# soundtrack/live-album bonus tracks). Grey's call: no interest in
# multichannel outside of stereo, so these are a permanent skip, not a
# retry-forever error -- checked proactively via archive.channels (already
# populated by Scholar) before ever calling Essentia, and also caught here
# as a fallback in case that DB value is ever missing/stale.
_MULTICHANNEL_ERROR_PATTERNS = ("more than 2 channels",)


def _is_skip_error(exc: Exception) -> bool:
    msg = str(exc).lower()
    return any(p in msg for p in _SKIP_ERROR_PATTERNS)


def _is_multichannel_error(exc: Exception) -> bool:
    msg = str(exc).lower()
    return any(p in msg for p in _MULTICHANNEL_ERROR_PATTERNS)


# ── Essentia analysis ────────────────────────────────────────────────────────


def analyze_file(path: Path) -> dict[str, float | str]:
    """Full Essentia analysis pass: BPM, energy, key, danceability.
    Raises on failure -- caller decides skip vs. error via _is_skip_error."""
    import gc

    import essentia  # type: ignore[import-not-found]
    import essentia.standard as es  # type: ignore[import-not-found]

    # Essentia's own C++-side AudioLoader logs a "skipping frame" warning
    # per bad/unsupported frame straight to stdout (bypassing Python's
    # logging module), which floods the console on hi-res source files --
    # confirmed 2026-08-19 against real 96/192kHz vault content. This is
    # normal, fault-tolerant frame-skipping (decode continues, BPM/energy/
    # key/danceability are whole-track statistics unaffected by a handful
    # of skipped frames out of hundreds of thousands) -- verified the
    # suppression itself changes nothing: same sample count, same BPM,
    # only the console spam disappears.
    essentia.log.warningActive = False

    audio = es.MonoLoader(filename=str(path), sampleRate=44100)()

    # RhythmExtractor2013(multifeature) runs five beat trackers and reconciles
    # them. Cost grows with length AND with how ambiguous the beat is, so a
    # long track with no steady pulse is the worst case in both directions.
    #
    # Found the hard way on 2026-08-23: a 45-minute ambient "sound bath" held
    # the whole run at 100% CPU for over nine minutes on that single file,
    # with no checkpoint and no way to interrupt it. There is no timeout that
    # can help here -- essentia runs in C++, and a Python signal handler only
    # gets to run between bytecodes, so signal.alarm() would not fire until
    # the call had already returned. The work itself has to be bounded.
    #
    # Tempo is a local property: a few minutes establishes it as well as an
    # hour does. So anything longer than _RHYTHM_MAX_SECONDS is analysed as a
    # centred excerpt. The threshold sits well above normal song length, so
    # ordinary tracks take the whole-file path exactly as before and their
    # results do not move.
    rhythm_audio = audio
    if len(audio) > _RHYTHM_MAX_SAMPLES:
        mid = len(audio) // 2
        half = _RHYTHM_MAX_SAMPLES // 2
        rhythm_audio = audio[mid - half : mid + half]
        logger.info(
            "[bpm] %s is %.0f min; analysing a centred %.0f min excerpt for tempo",
            path.name,
            len(audio) / 44100 / 60,
            _RHYTHM_MAX_SECONDS / 60,
        )

    bpm, _, _, _, _ = es.RhythmExtractor2013(method="multifeature")(rhythm_audio)

    raw_energy = float(es.Energy()(audio))
    energy = min(1.0, raw_energy / 500000.0)

    key, scale, _ = es.KeyExtractor()(audio)

    danceability, _ = es.Danceability()(audio)

    del audio
    gc.collect()

    return {
        "bpm": float(bpm),
        "energy": energy,
        "musical_key": f"{key} {scale}",
        "danceability": float(danceability),
    }


# ── Tag readers (skip Essentia if already tagged) ────────────────────────────


def read_existing_tags(path: Path) -> dict[str, float | str] | None:
    """Read BPM/key/energy/danceability from file tags, if present.
    Returns None if BPM is missing/zero or the format isn't supported --
    caller falls back to Essentia analysis."""
    suffix = path.suffix.lower()
    try:
        if suffix in (".m4a", ".alac", ".aac"):
            from mutagen.mp4 import MP4  # type: ignore[import-untyped]

            audio = MP4(str(path))
            tags = audio.tags
            if tags is None:
                return None
            bpm_raw = tags.get("tmpo")
            bpm = float(bpm_raw[0]) if bpm_raw else 0.0
            if not bpm:
                return None

            def _freeform(key: str) -> str:
                raw = tags.get(key)
                if not raw:
                    return ""
                try:
                    return bytes(raw[0]).decode("utf-8", errors="replace").strip()
                except Exception:
                    return ""

            energy_str = _freeform("----:com.apple.iTunes:Energy")
            dance_str = _freeform("----:com.apple.iTunes:Danceability")
            return {
                "bpm": bpm,
                "musical_key": _freeform("----:com.apple.iTunes:initialkey"),
                "energy": float(energy_str) if energy_str else 0.0,
                "danceability": float(dance_str) if dance_str else 0.0,
            }

        if suffix == ".flac":
            from mutagen.flac import FLAC  # type: ignore[import-untyped]

            flac_audio = FLAC(str(path))
            flac_tags = flac_audio.tags
            if flac_tags is None:
                return None
            bpm_raw = flac_tags.get("bpm")
            bpm = float(bpm_raw[0]) if bpm_raw else 0.0
            if not bpm:
                return None
            key_raw = flac_tags.get("initialkey")
            energy_raw = flac_tags.get("energy")
            dance_raw = flac_tags.get("danceability")
            return {
                "bpm": bpm,
                "musical_key": key_raw[0] if key_raw else "",
                "energy": float(energy_raw[0]) if energy_raw else 0.0,
                "danceability": float(dance_raw[0]) if dance_raw else 0.0,
            }
    except Exception as exc:
        logger.debug("read_existing_tags failed for %s: %s", path, exc)
    return None


# ── Tag writers ───────────────────────────────────────────────────────────────


def _write_tags_m4a(path: Path, features: dict[str, float | str]) -> bool:
    try:
        from mutagen.mp4 import MP4, MP4FreeForm  # type: ignore[import-untyped]

        audio: Any = MP4(str(path))
        if audio.tags is None:
            audio.add_tags()
        tags = audio.tags
        tags["tmpo"] = [int(round(features["bpm"]))]  # type: ignore[arg-type]
        tags["----:com.apple.iTunes:initialkey"] = [
            MP4FreeForm(str(features["musical_key"]).encode("utf-8"))
        ]
        tags["----:com.apple.iTunes:Energy"] = [MP4FreeForm(f"{features['energy']:.2f}".encode())]
        tags["----:com.apple.iTunes:Danceability"] = [
            MP4FreeForm(f"{features['danceability']:.2f}".encode())
        ]
        audio.save()
        return True
    except Exception as exc:
        logger.debug("m4a BPM tag write failed %s: %s", path, exc)
        return False


def _write_tags_flac(path: Path, features: dict[str, float | str]) -> bool:
    try:
        from mutagen.flac import FLAC  # type: ignore[import-untyped]

        audio = FLAC(str(path))
        audio["BPM"] = [str(int(round(features["bpm"])))]  # type: ignore[arg-type]
        audio["INITIALKEY"] = [str(features["musical_key"])]
        audio["ENERGY"] = [f"{features['energy']:.2f}"]
        audio["DANCEABILITY"] = [f"{features['danceability']:.2f}"]
        audio.save()
        return True
    except Exception as exc:
        logger.debug("flac BPM tag write failed %s: %s", path, exc)
        return False


def write_bpm_tags(path: Path, features: dict[str, float | str]) -> bool:
    """Dispatch to the right tag writer based on file extension. Returns
    True for a successful write OR a format with no tag writer (DB-only,
    matching ForgeStage's own WAV fallback -- not a failure)."""
    ext = path.suffix.lower()
    if ext in (".m4a", ".alac"):
        return _write_tags_m4a(path, features)
    if ext == ".flac":
        return _write_tags_flac(path, features)
    logger.debug("no BPM tag writer for ext %s, DB-only: %s", ext, path)
    return True


# ── DB helpers ────────────────────────────────────────────────────────────────


def _tunemymusic_csv_has_path(csv_path: Path, file_path: str) -> bool:
    """True if file_path is already logged in TuneMyMusic.csv -- guards
    against duplicate rows piling up every time a permanently-unanalyzable
    file (multichannel audio) gets re-encountered across repeated runs."""
    if not csv_path.exists():
        return False
    try:
        with open(csv_path, newline="", encoding="utf-8") as fh:
            reader = csv.reader(fh)
            next(reader, None)  # header
            return any(row and row[-1] == file_path for row in reader)
    except OSError:
        return False


def _mark_multichannel_skipped(ctx: RunContext, file_path: str) -> None:
    """Permanently exclude a multichannel file from future BPM attempts
    (bpm_analyzed_at set with bpm/key/energy/danceability left NULL --
    a legitimate terminal state, not a failed-analysis-to-retry) and log
    it to TuneMyMusic.csv for manual review/replacement with a stereo
    source. Grey's call, 2026-08-20: no interest in multichannel audio
    outside of stereo."""
    now = datetime.now(tz=timezone.utc).isoformat(timespec="seconds")
    ctx.conn.execute(
        "UPDATE archive SET bpm_analyzed_at = ? WHERE file_path = ?",
        (now, file_path),
    )
    ctx.log_event(
        "BPM_SKIPPED_MULTICHANNEL",
        file_path=file_path,
        stage="bpm",
        note="multichannel audio -- unsupported by Essentia, unwanted per Grey's policy",
    )

    csv_path = ctx.config.tunemymusic_csv_path
    if _tunemymusic_csv_has_path(csv_path, file_path):
        return
    row = ctx.conn.execute(
        "SELECT codec, bitrate, sample_rate, channels, duration FROM archive WHERE file_path = ?",
        (file_path,),
    ).fetchone()
    _append_tunemymusic_row(
        ctx,
        {
            "reason": "multichannel audio (no stereo interest -- see BPM skip)",
            "codec": row["codec"] if row else None,
            "bitrate": row["bitrate"] if row else None,
            "sample_rate": row["sample_rate"] if row else None,
            "channels": row["channels"] if row else None,
            "duration": row["duration"] if row else None,
            "file_path": file_path,
        },
    )


def _save_features(ctx: RunContext, file_path: str, features: dict[str, float | str]) -> None:
    ctx.conn.execute(
        """
        UPDATE archive
           SET bpm             = ?,
               musical_key     = ?,
               energy          = ?,
               danceability    = ?,
               bpm_analyzed_at = ?
         WHERE file_path = ?
        """,
        (
            features["bpm"],
            features["musical_key"],
            features["energy"],
            features["danceability"],
            datetime.now(tz=timezone.utc).isoformat(timespec="seconds"),
            file_path,
        ),
    )
    ctx.log_event(
        "BPM_ANALYZED",
        file_path=file_path,
        new_value=f"bpm={features['bpm']:.0f} key={features['musical_key']}",
        stage="bpm",
    )


# ── BPM Stage ─────────────────────────────────────────────────────────────────


class BPMStage(BaseStage):
    """
    BPM/key/energy/danceability extraction for every CATALOGUED file.
    Standalone -- not part of DEFAULT_PIPELINE. Use ctx.set("bpm_force", True)
    to re-analyze already-analyzed rows, or ctx.set("bpm_retag", True) to
    force Essentia even when a file already has BPM tags.
    """

    @classmethod
    def plan_candidates(cls, conn, cfg) -> tuple[int, str]:
        """Rows this stage would act on. Read-only; see planner.py."""
        n = conn.execute(
            "SELECT COUNT(*) FROM archive WHERE status='CATALOGUED' AND bpm IS NULL"
        ).fetchone()[0]
        return int(n), "files needing BPM analysis"

    NAME = "bpm"

    def validate(self, ctx: RunContext) -> None:
        try:
            import essentia.standard  # noqa: F401
        except ImportError:
            raise StageError("essentia not installed -- run: pip install -e '.[bpm]'") from None
        try:
            import mutagen  # noqa: F401
        except ImportError:
            raise StageError("mutagen not installed -- run: pip install mutagen") from None

    def _get_pending(self, ctx: RunContext, force: bool) -> list[str]:
        if force:
            rows = ctx.conn.execute(
                "SELECT file_path FROM archive WHERE status='CATALOGUED' ORDER BY artist, album, track"
            ).fetchall()
        else:
            rows = ctx.conn.execute(
                """
                SELECT file_path FROM archive
                 WHERE status='CATALOGUED'
                   AND (bpm_analyzed_at IS NULL OR bpm_analyzed_at = '')
                 ORDER BY artist, album, track
                """
            ).fetchall()
        return [r["file_path"] for r in rows]

    def _process_one(self, ctx: RunContext, file_path: str, retag: bool) -> str:
        """Returns: 'ok' | 'tag_shortcut' | 'skip' | 'skip_multichannel' | 'error' | 'missing'"""
        path = Path(file_path)
        if not path.exists():
            return "missing"

        # Proactive check via Scholar's already-populated channel count --
        # avoids ever spinning up Essentia for a file we already know is
        # unanalyzable, not just reacting to its exception after the fact.
        row = ctx.conn.execute(
            "SELECT channels, duration FROM archive WHERE file_path = ?", (file_path,)
        ).fetchone()
        if row and row["channels"] and row["channels"] > 2:
            _mark_multichannel_skipped(ctx, file_path)
            return "skip_multichannel"

        # Same idea as the multichannel check above, for the same reason:
        # decide from what Scholar already recorded rather than discovering
        # it inside Essentia.
        #
        # MonoLoader materialises the WHOLE decoded file as float32 before
        # anything can trim it -- roughly 10 MB per minute at 44.1 kHz. On
        # 2026-08-23 a 12-hour "sound bath" (7.6 GB decoded, 681 s just to
        # load) got far enough to have the kernel OOM-kill the entire run at
        # the BPM stage, after 18 stages of good work, with no checkpoint
        # written. The run reported exit 137, and because the wrapper's own
        # exit code was 0 it briefly looked like a clean finish.
        #
        # Nothing above the ceiling is a track: the longest real music in the
        # library is Miles Davis at 28.5 min, with the Allman Brothers'
        # "Whipping Post" at 22.9 min. Everything past 45 minutes is guided
        # meditation and sleep loops, which have no meaningful tempo anyway.
        duration = row["duration"] if row else None
        if duration and duration > _MAX_ANALYSIS_SECONDS:
            logger.warning(
                "[bpm] skipping %s: %.0f min exceeds the %.0f min analysis ceiling "
                "(decoding it would need ~%.1f GB)",
                path.name,
                duration / 60,
                _MAX_ANALYSIS_SECONDS / 60,
                duration * 44100 * 4 / 1e9,
            )
            ctx.log_event(
                "BPM_SKIPPED_TOO_LONG",
                file_path=file_path,
                old_value=None,
                new_value=None,
                stage=self.NAME,
                note=f"{duration / 60:.0f} min exceeds {_MAX_ANALYSIS_SECONDS / 60:.0f} min ceiling",
            )
            return "skip"

        features: dict[str, float | str] | None = None
        from_tags = False
        if not retag:
            features = read_existing_tags(path)
            from_tags = features is not None

        if features is None:
            try:
                features = analyze_file(path)
            except Exception as exc:
                if _is_multichannel_error(exc):
                    _mark_multichannel_skipped(ctx, file_path)
                    return "skip_multichannel"
                if _is_skip_error(exc):
                    return "skip"
                logger.warning("[bpm] %s: %s", path, exc)
                return "error"

        _save_features(ctx, file_path, features)
        if not from_tags:
            write_bpm_tags(path, features)
        return "tag_shortcut" if from_tags else "ok"

    def run(self, ctx: RunContext) -> StageResult:
        result = self._make_result(dry_run=False)
        force: bool = ctx.get("bpm_force", False)
        retag: bool = ctx.get("bpm_retag", False)

        pending = self._get_pending(ctx, force)
        total = len(pending)
        result.notes.append(f"files to analyze: {total}")
        if not total:
            result.notes.append("nothing to do — all CATALOGUED files already analyzed")
            ctx.record_stage(result)
            return result

        counters: dict[str, int] = {
            "ok": 0,
            "tag_shortcut": 0,
            "skip": 0,
            "skip_multichannel": 0,
            "error": 0,
            "missing": 0,
        }

        for i, fp in enumerate(pending, 1):
            status = self._process_one(ctx, fp, retag)
            counters[status] = counters.get(status, 0) + 1
            result.files_processed += 1

            if status in ("ok", "tag_shortcut"):
                result.files_changed += 1
            elif status in ("skip", "skip_multichannel", "missing"):
                result.files_skipped += 1
            else:
                result.files_errored += 1

            if i % _COMMIT_EVERY == 0:
                ctx.conn.commit()
                logger.info("bpm: checkpoint %d/%d", i, total)

        ctx.conn.commit()

        for k, v in counters.items():
            if v:
                result.notes.append(f"  {k}: {v}")
        result.notes.append(
            f"  (analyzed via Essentia: {counters['ok']}, from existing tags: {counters['tag_shortcut']})"
        )

        if counters["error"] > 0:
            result.success = False

        ctx.record_stage(result)
        return result

    def dry_run(self, ctx: RunContext) -> StageResult:
        result = self._make_result(dry_run=True)
        force: bool = ctx.get("bpm_force", False)
        pending = self._get_pending(ctx, force)
        total = len(pending)

        result.files_processed = total
        result.notes.append(f"[DRY RUN] would analyze {total} file(s)")
        result.notes.append("  no tags will be written, no DB changes")

        ctx.record_stage(result)
        return result
