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

import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ..context import RunContext, StageResult
from .base import BaseStage, StageError

logger = logging.getLogger(__name__)

_COMMIT_EVERY = 25

_SKIP_ERROR_PATTERNS = (
    "too short",
    "not enough audio",
    "insufficient frames",
    "empty signal",
    "output buffer is full",
    "could not push",
    "onsetdetectionglobal",
)


def _is_skip_error(exc: Exception) -> bool:
    msg = str(exc).lower()
    return any(p in msg for p in _SKIP_ERROR_PATTERNS)


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

    bpm, _, _, _, _ = es.RhythmExtractor2013(method="multifeature")(audio)

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
        """Returns: 'ok' | 'tag_shortcut' | 'skip' | 'error' | 'missing'"""
        path = Path(file_path)
        if not path.exists():
            return "missing"

        features: dict[str, float | str] | None = None
        from_tags = False
        if not retag:
            features = read_existing_tags(path)
            from_tags = features is not None

        if features is None:
            try:
                features = analyze_file(path)
            except Exception as exc:
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

        counters: dict[str, int] = {"ok": 0, "tag_shortcut": 0, "skip": 0, "error": 0, "missing": 0}

        for i, fp in enumerate(pending, 1):
            status = self._process_one(ctx, fp, retag)
            counters[status] = counters.get(status, 0) + 1
            result.files_processed += 1

            if status in ("ok", "tag_shortcut"):
                result.files_changed += 1
            elif status in ("skip", "missing"):
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
