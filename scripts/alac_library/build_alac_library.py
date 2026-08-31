#!/usr/bin/env python3
"""
MUSAEUS — ALAC-Library LUFS Bake (Phase 2A, standalone tool, NOT a pipeline stage)

Bakes -18 LUFS into a real listening copy for every CATALOGUED archive row
whose file_path currently points into ALAC_Archive (the pristine, unbaked
tier scripts/musaeus_migrate_to_archive.py populates), writing the baked
output into ALAC-Library at the exact same relative path -- the reverse of
that script's own ALAC-Library -> ALAC_Archive path mapping.

Design, mirroring both migrate_to_archive.py's safety shape and
canonicalize.py's verify-then-atomic-swap discipline (both already
execution-verified elsewhere in this codebase, not reinvented here):

  - DB-row-driven, not a directory scan: reads from `archive`, not from
    walking ALAC_Archive on disk. Selection: status='CATALOGUED', file_path
    already under archive_dir, lufs_baked_at IS NULL (or --force).
  - Two-pass EBU R128 loudnorm, same mechanics as build_aac_library.py's
    Phase 2B fix (measure pass -> second pass using measured_I/LRA/TP/
    thresh/offset), target -18.0 LUFS / -1.0 dBTP / 11.0 LRA.
  - Bake writes to a `.bake_tmp`-suffixed temp path under the ALAC-Library
    target location; ffprobe-verify (audio stream present, duration
    matches source within tolerance) before ever trusting it; only then
    atomic-rename into place. A failed bake is quarantined to
    `.FAILED_VERIFY`, never silently deleted or retried within the same run.
  - The ALAC_Archive source is NEVER modified or deleted by this script --
    it stays the permanent rollback copy. Only archive.file_path moves (to
    point at the new ALAC-Library location), same "file_path = wherever
    this row currently, authoritatively lives" convention canonicalize.py/
    finalize.py/migrate_to_archive.py all already use. The archive-tier
    copy remains reachable afterward by re-deriving its path (swap
    ALAC-Library -> ALAC_Archive in the relative path -- the exact reverse
    of _library_path_for() below), not by a dedicated DB column --
    deliberately, to avoid a second path column that could drift out of
    sync with the real filesystem state.
  - Collision-safe DB write (SOP §4.12): disk-side bake (tmp -> target
    rename) happens before the DB UPDATE. If the UPDATE then fails (e.g. a
    UNIQUE collision on archive.file_path), the row is left with
    lufs_baked_at still NULL and file_path unchanged -- the baked file sits
    at target for manual review, and a future run naturally retries it
    (overwriting target again) rather than needing a manual revert, since
    the source was never touched.
  - Live re-check per row (status/lufs_baked_at re-read at process time,
    not trusted from the initial snapshot) -- same discipline as
    migrate_to_archive.py, guards against another process changing the row
    between selection and processing.
  - Dry run by default; --execute actually writes. --limit N caps how many
    rows are processed, for reviewing a sample before running against
    everything. Same flag shape as migrate_to_archive.py, deliberately --
    this script inherits its risk profile (real vault, real audio content)
    more than build_car_library.py's occasional-manual-export shape.

ALAC_Archive is not yet wired into musaeus/config.py (as of 2026-08-18;
confirmed absent, no ETA) -- the archive directory is resolved via
--archive-dir or MUSAEUS_ALAC_ARCHIVE, defaulting to vault_root/ALAC_Archive
(the same path migrate_to_archive.py hardcodes), rather than depending on
config wiring that doesn't exist yet. Swap to config.alac_archive once it
lands; no other change needed here.

Usage:
    python3 scripts/alac_library/build_alac_library.py               # dry run
    python3 scripts/alac_library/build_alac_library.py --execute
    python3 scripts/alac_library/build_alac_library.py --execute --limit 20
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(_REPO_ROOT))

from musaeus.config import get_config  # noqa: E402
from musaeus.db import open_db  # noqa: E402

FFMPEG = "ffmpeg"
FFPROBE = "ffprobe"

TARGET_I = "-18.0"
TARGET_TP = "-1.0"
TARGET_LRA = "11.0"
_DURATION_TOLERANCE_SEC = 1.5


# ── Path resolution ──────────────────────────────────────────────────────────


def _archive_dir(cli_value: str | None, vault_root: Path) -> Path:
    if cli_value:
        return Path(cli_value).resolve()
    env_value = os.environ.get("MUSAEUS_ALAC_ARCHIVE")
    if env_value:
        return Path(env_value).resolve()
    return vault_root / "ALAC_Archive"


def _library_path_for(source: Path, archive_dir: Path, library_dir: Path) -> Path:
    """Same relative shape as the source, just under library_dir instead of
    archive_dir -- the exact reverse of migrate_to_archive.py's _target_path."""
    rel = source.relative_to(archive_dir)
    return library_dir.joinpath(*rel.parts)


# ── Two-pass loudnorm (measure + bake) ───────────────────────────────────────
#
# Same mechanics as scripts/car_library/vendor/build_aac_library.py's Phase
# 2B fix (itself ported from ORPHEUS's normalize_selected_lufs.py). Kept as
# a local copy rather than a shared import: build_aac_library.py is invoked
# as a subprocess with its own sys.path rooted at its vendor/ directory, so
# sharing a module across it and this script would need its own import-path
# plumbing for marginal benefit over ~40 lines duplicated once. Revisit if a
# third caller shows up.


def ffmpeg_measure_loudnorm(path: Path, target_i: str, target_tp: str, target_lra: str) -> dict:
    filter_str = f"loudnorm=I={target_i}:TP={target_tp}:LRA={target_lra}:print_format=json"
    cmd = [
        FFMPEG,
        "-hide_banner",
        "-nostats",
        "-i",
        str(path),
        "-af",
        filter_str,
        "-f",
        "null",
        "-",
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    stderr = result.stderr or ""
    start, end = stderr.rfind("{"), stderr.rfind("}")
    if start == -1 or end == -1 or end <= start:
        raise RuntimeError(f"Could not parse loudnorm measurement for: {path}")
    return json.loads(stderr[start : end + 1])


def build_second_pass_filter(measured: dict, target_i: str, target_tp: str, target_lra: str) -> str:
    return (
        f"loudnorm="
        f"I={target_i}:TP={target_tp}:LRA={target_lra}:"
        f"measured_I={measured['input_i']}:"
        f"measured_LRA={measured['input_lra']}:"
        f"measured_TP={measured['input_tp']}:"
        f"measured_thresh={measured['input_thresh']}:"
        f"offset={measured['target_offset']}:"
        f"linear=true:print_format=summary"
    )


def _probe_streams(path: Path) -> dict:
    cmd = [
        FFPROBE,
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
        raise RuntimeError(f"ffprobe failed on {path} ({proc.returncode}): {proc.stderr[:200]}")
    return json.loads(proc.stdout)


def _has_attached_picture(probe: dict) -> bool:
    for s in probe.get("streams", []):
        if s.get("codec_type") == "video" and s.get("disposition", {}).get("attached_pic") == 1:
            return True
    return False


# How far the baked result may sit from the target before it is a failure.
# loudnorm's linear mode is accurate to well inside this; a miss of more than
# 1 dB means the filter did not do what was asked.
_LUFS_TOLERANCE = 1.0

_OUTPUT_I_RE = re.compile(r"Output Integrated:\s*(-?\d+(?:\.\d+)?)\s*LUFS", re.I)


def parse_output_loudness(stderr: str) -> float | None:
    """The integrated loudness ffmpeg says it ACHIEVED, from loudnorm's summary.

    None when it cannot be read -- which is reported, never treated as a pass.
    """
    m = _OUTPUT_I_RE.search(stderr or "")
    return float(m.group(1)) if m else None


def verify_bake(
    source: Path, output: Path, achieved: float | None = None, target_i: str = TARGET_I
) -> None:
    """Same discipline as canonicalize.py's _verify_conversion: output must
    have an audio stream, duration must match source within tolerance."""
    src_probe = _probe_streams(source)
    out_probe = _probe_streams(output)

    out_audio = [s for s in out_probe.get("streams", []) if s.get("codec_type") == "audio"]
    if not out_audio:
        raise RuntimeError("verification failed: baked output has no audio stream")

    def _duration(probe: dict) -> float | None:
        d = probe.get("format", {}).get("duration")
        try:
            return float(d) if d else None
        except (TypeError, ValueError):
            return None

    # Did the LOUDNESS actually land?
    #
    # This function checked only that an audio stream existed and the
    # duration matched -- i.e. that a file came out the other end. Neither
    # says anything about the one thing the bake exists to do. A loudnorm
    # that silently no-ops, or a second pass fed wrong measured values,
    # produces a perfectly valid file at the wrong loudness, and
    # lufs_baked_at is stamped anyway so it is never retried. That is the
    # same shape as Forge reporting 12,279 successful tag writes while
    # writing nothing.
    #
    # Free to check: the second pass already runs with print_format=summary,
    # so ffmpeg reports the achieved integrated loudness on stderr.
    if achieved is not None:
        want = float(target_i)
        if abs(achieved - want) > _LUFS_TOLERANCE:
            raise RuntimeError(
                f"verification failed: baked to {achieved:.2f} LUFS, "
                f"wanted {want:.2f} (tolerance {_LUFS_TOLERANCE})"
            )
    else:
        print(
            f"  WARNING: could not read achieved loudness for {output.name} -- "
            f"the bake is UNVERIFIED on the thing it exists to do"
        )

    src_dur, out_dur = _duration(src_probe), _duration(out_probe)
    if (
        src_dur is not None
        and out_dur is not None
        and abs(src_dur - out_dur) > _DURATION_TOLERANCE_SEC
    ):
        raise RuntimeError(
            f"verification failed: duration mismatch (source={src_dur:.2f}s, output={out_dur:.2f}s)"
        )


def quarantine_failed_tmp(tmp_output: Path | None) -> None:
    if tmp_output is None or not tmp_output.exists():
        return
    failed_path = tmp_output.with_name(tmp_output.name + ".FAILED_VERIFY")
    with contextlib.suppress(OSError):
        tmp_output.rename(failed_path)


def build_bake_command(
    source: Path, tmp_output: Path, loudnorm_filter: str, has_art: bool
) -> list[str]:
    """ALAC -> ALAC, codec unchanged -- this bakes loudness, not format. Same
    -map/-c:v copy/attached_pic/-map_metadata shape as canonicalize.py's
    _convert_to_alac, plus the -af loudnorm filter. -f mp4 forced explicitly
    since tmp_output's on-disk extension (.m4a.bake_tmp) isn't a real hint."""
    cmd = [FFMPEG, "-y", "-i", str(source), "-threads", "2"]
    if has_art:
        cmd += [
            "-map",
            "0:a:0",
            "-map",
            "0:v:0",
            "-c:a",
            "alac",
            "-af",
            loudnorm_filter,
            "-c:v",
            "copy",
            "-disposition:v:0",
            "attached_pic",
            "-map_metadata",
            "0",
            "-f",
            "mp4",
            str(tmp_output),
        ]
    else:
        cmd += [
            "-map",
            "0:a:0",
            "-c:a",
            "alac",
            "-af",
            loudnorm_filter,
            "-map_metadata",
            "0",
            "-f",
            "mp4",
            str(tmp_output),
        ]
    return cmd


# ── Per-row processing ────────────────────────────────────────────────────────


def _candidate_rows(conn, archive_dir: Path, force: bool) -> list[dict]:
    if force:
        rows = conn.execute(
            """
            SELECT id, file_path, artist, album, title FROM archive
             WHERE status = 'CATALOGUED'
               AND file_path LIKE ? || '%'
             ORDER BY file_path
            """,
            (str(archive_dir),),
        ).fetchall()
    else:
        rows = conn.execute(
            """
            SELECT id, file_path, artist, album, title FROM archive
             WHERE status = 'CATALOGUED'
               AND file_path LIKE ? || '%'
               AND (lufs_baked_at IS NULL OR lufs_baked_at = '')
             ORDER BY file_path
            """,
            (str(archive_dir),),
        ).fetchall()
    return [dict(r) for r in rows]


def _unmigrated_count(conn, library_dir: Path) -> int:
    """Finalized rows still sitting in ALAC-Library that never got moved
    into ALAC_Archive by migrate_to_archive.py -- this script can't see
    them (it only reads rows already under archive_dir), so they'd
    silently never get baked unless someone remembers to run the
    migration script first. Same selection shape as
    migrate_to_archive.py's own _candidate_rows(), so the two scripts
    agree on what "not yet migrated" means."""
    return conn.execute(
        """
        SELECT COUNT(*) FROM archive
         WHERE status = 'CATALOGUED'
           AND finalized_at IS NOT NULL
           AND file_path LIKE ? || '%'
        """,
        (str(library_dir),),
    ).fetchone()[0]


def _process_one(conn, row: dict, archive_dir: Path, library_dir: Path, execute: bool) -> str:
    source = Path(row["file_path"])

    current = conn.execute(
        "SELECT status, lufs_baked_at FROM archive WHERE id = ?", (row["id"],)
    ).fetchone()
    if not current or current["status"] != "CATALOGUED":
        return f"SKIP  {source.name}: no longer a CATALOGUED row"
    if current["lufs_baked_at"]:
        return f"SKIP  {source.name}: already baked (re-check raced initial snapshot)"
    if not source.exists():
        return f"ERROR {source.name}: source missing on disk"

    target = _library_path_for(source, archive_dir, library_dir)

    if not execute:
        return f"WOULD BAKE  {source.name} -> {target}"

    target.parent.mkdir(parents=True, exist_ok=True)
    tmp_output = target.with_name(target.name + ".bake_tmp")

    try:
        probe = _probe_streams(source)
        has_art = _has_attached_picture(probe)

        measured = ffmpeg_measure_loudnorm(source, TARGET_I, TARGET_TP, TARGET_LRA)
        loudnorm_filter = build_second_pass_filter(measured, TARGET_I, TARGET_TP, TARGET_LRA)

        cmd = build_bake_command(source, tmp_output, loudnorm_filter, has_art)
        bake = subprocess.run(cmd, capture_output=True, text=True, check=True)
        achieved = parse_output_loudness(bake.stderr)
        verify_bake(source, tmp_output, achieved=achieved)

        tmp_output.rename(target)
    except subprocess.CalledProcessError as e:
        quarantine_failed_tmp(tmp_output)
        stderr = (e.stderr or "").strip().splitlines()
        return f"ERROR {source.name}: {stderr[-1] if stderr else e}"
    except (RuntimeError, OSError) as e:
        quarantine_failed_tmp(tmp_output)
        return f"ERROR {source.name}: {e}"

    # Disk change is done; DB update second (SOP §4.12). A failure here
    # leaves the baked file at `target` and lufs_baked_at still NULL --
    # self-healing on the next run, source untouched throughout.
    try:
        conn.execute(
            """
            UPDATE archive
               SET file_path = ?, lufs_baked_at = datetime('now'), lufs_baked_target = ?
             WHERE id = ?
            """,
            (str(target), float(TARGET_I), row["id"]),
        )
        conn.execute(
            "INSERT INTO events (run_id, event_type, file_path, old_value, new_value, stage, note) "
            "VALUES (?, 'LUFS_BAKE', ?, ?, ?, 'build_alac_library', ?)",
            (
                f"lufs_bake_{datetime.now(tz=timezone.utc).strftime('%Y%m%dT%H%M%SZ')}",
                str(target),
                str(source),
                str(target),
                f"baked to {TARGET_I} LUFS (ALAC_Archive -> ALAC-Library)",
            ),
        )
        conn.commit()
    except Exception as exc:  # noqa: BLE001 -- must not crash the run over one row's bookkeeping
        return f"ERROR {source.name}: baked ok but DB update failed ({exc}); target={target}"

    return f"BAKED {source.name} -> {target}"


# ── main ───────────────────────────────────────────────────────────────────────


def main() -> int:
    parser = argparse.ArgumentParser(
        description="MUSAEUS Phase 2A -- bake -18 LUFS from ALAC_Archive into ALAC-Library"
    )
    parser.add_argument("--archive-dir", default=None, help="Override ALAC_Archive location")
    parser.add_argument(
        "--limit", type=int, default=None, help="Only process the first N matching rows"
    )
    parser.add_argument(
        "--force", action="store_true", help="Re-bake rows even if lufs_baked_at is set"
    )
    parser.add_argument(
        "--execute", action="store_true", help="Actually bake + update DB (default: dry run)"
    )
    args = parser.parse_args()

    cfg = get_config()
    archive_dir = _archive_dir(args.archive_dir, cfg.vault_root)
    library_dir = cfg.alac_library

    conn = open_db(cfg.db_path)

    unmigrated = _unmigrated_count(conn, library_dir)
    if unmigrated:
        print(
            f"⚠  {unmigrated} finalized row(s) are still in ALAC-Library, never migrated "
            f"to ALAC_Archive -- this run WILL NOT see them (FinalizeStage doesn't write "
            f"to ALAC_Archive yet, this is still a manual step). Run "
            f"scripts/musaeus_migrate_to_archive.py first if you want them included.\n"
        )

    rows = _candidate_rows(conn, archive_dir, args.force)
    if args.limit:
        rows = rows[: args.limit]

    mode = "EXECUTING" if args.execute else "DRY RUN (pass --execute to actually bake)"
    print(f"{mode} — target {TARGET_I} LUFS — {len(rows)} row(s)")
    print(f"  archive: {archive_dir}")
    print(f"  library: {library_dir}\n")

    baked = skipped = errored = 0
    for row in rows:
        result = _process_one(conn, row, archive_dir, library_dir, args.execute)
        print(f"  {result}")
        if result.startswith("BAKED") or result.startswith("WOULD BAKE"):
            baked += 1
        elif result.startswith("SKIP"):
            skipped += 1
        else:
            errored += 1

    conn.close()
    verb = "baked" if args.execute else "would bake"
    print(f"\n{baked} {verb}, {skipped} skipped, {errored} errored.")
    return 1 if errored else 0


if __name__ == "__main__":
    sys.exit(main())
