#!/usr/bin/env python3
"""
MUSAEUS — DupeResolver Stage (end of Act 2)

Physically relocates duplicate-group losers out of the batch and into a
review folder that mirrors ALAC-Library's own shape exactly, per Grey's
explicit instruction: "Same as Dedup folder, which should be the same
as ALAC-Library. Artist folder, album folder, song tracks." Nothing is
ever deleted -- this is a hold area for human review, not a resolution.

Runs at the END of Act 2 (after Sentinel/CrossDupe/NearDupe have all had
a chance to flag everything they're going to flag for this batch), and
BEFORE Act 3 (Canonicalize/Forge/Tagger/Finalize). This ordering is the
actual point of building this now rather than later: a confirmed
duplicate gets pulled out of the batch before any ffmpeg conversion or
loudness measurement is wasted on a file that's about to be set aside
anyway.

Destination shape:
    ALAC-Library/DUPES_MOVED_FOR_REVIEW/<YYYY-MM-DD>/<Artist>/<Album>/<Track>.ext

Same dated-batch-folder convention as Finalize's own output, same
Artist/Album/Track naming (via organize.py's build_track_filename/
sanitize_path_component -- identical helpers, so a resolved file that
turns out NOT to be a duplicate after human review lands at exactly the
same path Finalize would have produced for it).

Keeper selection:
  - EXACT / NEAR groups (multiple files WITHIN this batch): reuses
    dedupe.py's existing highest-bitrate-then-largest-size rule
    (_auto_keep_best's logic, applied here directly rather than
    reimplemented) -- this is not a new policy, it's the same rule
    already used by the interactive `musaeus dedupe --auto` console.
  - CROSS_BATCH groups (this batch's file vs. something already in
    ALAC-Library from a prior batch): there is nothing to choose
    between -- the prior-batch copy is untouched and already safe, so
    the incoming file is simply the one that moves.

Every move is recorded in a per-run manifest CSV (source, destination,
group_id, duplicate_type, reason) plus an auto-generated bash restore
script (mkdir -p + mv -n pairs, chmod +x) -- ORPHEUS's own
move_lesser_dedupe_candidates.py pattern, ported directly: never
destructive, always reversible from one file.

What this stage deliberately does NOT do:
  - Decide whether something IS a duplicate. That's Sentinel/CrossDupe/
    NearDupe's job, already done by the time this stage runs. This
    stage only acts on groups already staged in the duplicates table.
  - Touch groups already resolved (status != 'pending') -- re-running
    this stage is safe, already-moved files are not moved again.
"""

from __future__ import annotations

import csv
import logging
import os
import shutil
import stat
from datetime import datetime, timezone
from pathlib import Path

from ..config import LOSSLESS_CODECS
from ..context import RunContext, StageResult
from .base import BaseStage
from .organize import build_track_filename, sanitize_path_component, unique_path

logger = logging.getLogger(__name__)


def _batch_date(ctx: RunContext) -> str:
    """Same convention as FinalizeStage._batch_date -- overridable for
    tests, defaults to today's real UTC date, computed once per run."""
    override = ctx.get("finalize_batch_date")
    if override:
        return override
    return datetime.now(tz=timezone.utc).strftime("%Y-%m-%d")


def _get_pending_groups(conn) -> list[str]:
    rows = conn.execute(
        "SELECT DISTINCT group_id FROM duplicates WHERE status = 'pending' ORDER BY group_id"
    ).fetchall()
    return [r[0] for r in rows]


def _get_group_members(conn, group_id: str) -> list[dict]:
    """
    Members of one duplicate group, ordered so the best keeper candidate
    is always first: real lossless codec beats lossy UNCONDITIONALLY
    (a bitrate/size comparison across different codecs isn't a fair
    quality comparison -- a quiet, highly-compressible FLAC can report a
    lower bitrate than a dense, less-compressible lossy file despite
    being the objectively better copy), then bitrate/size as a tiebreak
    among files that are equally lossless or equally lossy.
    """
    rows = conn.execute(
        """
        SELECT d.file_path, d.duplicate_type, d.confidence, d.status AS dup_status,
               a.artist, a.album, a.title, a.ext, a.codec, a.bitrate, a.size_bytes
          FROM duplicates d
          LEFT JOIN archive a USING (file_path)
         WHERE d.group_id = ?
        """,
        (group_id,),
    ).fetchall()
    members = [dict(r) for r in rows]
    members.sort(
        key=lambda m: (
            0 if (m.get("codec") or "").lower() in LOSSLESS_CODECS else 1,
            -(m.get("bitrate") or 0),
            -(m.get("size_bytes") or 0),
        )
    )
    return members


def _pick_keeper_and_losers(members: list[dict]) -> tuple[dict | None, list[dict]]:
    """
    Same rule as dedupe.py's _auto_keep_best: members are already sorted
    bitrate DESC, size_bytes DESC by the query above, so the first row is
    the keeper. CROSS_BATCH groups only ever have one member in THIS
    batch's duplicates table (the incoming file -- the prior-batch copy
    isn't a row here at all), so that lone member is always the "loser"
    relative to the untouched, already-safe library copy.
    """
    if not members:
        return None, []
    keep = members[0]
    losers = members[1:] if len(members) > 1 else members
    if len(members) == 1:
        return None, members  # CROSS_BATCH: nothing to keep, incoming file moves
    return keep, losers


class DupeResolverStage(BaseStage):
    """
    DupeResolver — physically relocate duplicate-group losers into
    ALAC-Library/DUPES_MOVED_FOR_REVIEW/, mirroring ALAC-Library's own
    Artist/Album/Track shape. Never deletes; always reversible.
    """

    NAME = "dupe-resolver"

    def validate(self, ctx: RunContext) -> None:
        count = len(_get_pending_groups(ctx.conn))
        logger.info("[dupe-resolver] %d pending duplicate group(s)", count)

    def _target_path(self, ctx: RunContext, member: dict, source: Path, batch_date: str) -> Path:
        artist = member.get("artist") or "Unknown Artist"
        album = member.get("album") or "Unsorted"
        title = member.get("title") or "Unknown Title"

        new_filename = build_track_filename(artist, title, source.suffix)
        artist_safe = sanitize_path_component(artist)
        album_safe = sanitize_path_component(album)

        target_dir = ctx.config.dupes_review_dir / batch_date / artist_safe / album_safe
        candidate = target_dir / new_filename
        return unique_path(candidate)

    def _write_manifest_and_restore_script(
        self, ctx: RunContext, batch_date: str, moves: list[tuple[str, str, str, str]]
    ) -> tuple[Path, Path]:
        """
        moves: list of (source, destination, group_id, duplicate_type).
        Returns (manifest_path, restore_script_path).
        """
        review_dir = ctx.config.dupes_review_dir / batch_date
        review_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(tz=timezone.utc).strftime("%Y%m%dT%H%M%SZ")

        manifest_path = review_dir / f"moved_manifest_{stamp}.csv"
        with open(manifest_path, "w", newline="", encoding="utf-8") as fh:
            writer = csv.writer(fh)
            writer.writerow(["source", "destination", "group_id", "duplicate_type"])
            for src, dst, gid, dtype in moves:
                writer.writerow([src, dst, gid, dtype])

        restore_path = review_dir / f"restore_{stamp}.sh"
        lines = ["#!/usr/bin/env bash", "set -euo pipefail", ""]
        for src, dst, _gid, _dtype in moves:
            src_dir = os.path.dirname(src)
            lines.append(f'mkdir -p "{src_dir}"')
            lines.append(f'mv -n "{dst}" "{src}"')
        restore_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        restore_path.chmod(restore_path.stat().st_mode | stat.S_IEXEC)

        return manifest_path, restore_path

    def _resolve(self, ctx: RunContext, dry_run: bool) -> StageResult:
        result = self._make_result(dry_run=dry_run)
        groups = _get_pending_groups(ctx.conn)
        result.notes.append(f"pending duplicate group(s): {len(groups)}")

        if not groups:
            result.notes.append("nothing to resolve — no pending duplicate groups")
            ctx.record_stage(result)
            return result

        batch_date = _batch_date(ctx)
        moved: list[tuple[str, str, str, str]] = []

        for group_id in groups:
            members = _get_group_members(ctx.conn, group_id)
            if not members:
                continue
            keeper, losers = _pick_keeper_and_losers(members)

            for loser in losers:
                result.files_processed += 1
                source = Path(loser["file_path"])

                if not source.exists():
                    result.files_errored += 1
                    result.errors.append(f"{source}: file missing on disk")
                    continue

                target = self._target_path(ctx, loser, source, batch_date)
                keeper_desc = keeper["file_path"] if keeper else "(already in ALAC-Library, prior batch)"

                if dry_run:
                    result.notes.append(
                        f"[{loser['duplicate_type']}] would move {source.name} -> {target} "
                        f"(keeping {keeper_desc})"
                    )
                    result.files_changed += 1
                    continue

                try:
                    target.parent.mkdir(parents=True, exist_ok=True)
                    shutil.move(str(source), str(target))
                except OSError as exc:
                    result.files_errored += 1
                    result.errors.append(f"{source}: {exc}")
                    logger.warning("[dupe-resolver] move failed %s: %s", source, exc)
                    continue

                ctx.conn.execute(
                    "UPDATE duplicates SET status = 'archive' WHERE group_id = ? AND file_path = ?",
                    (group_id, str(source)),
                )
                # The archive row's status must change too, and file_path
                # must follow the file to its new location -- otherwise a
                # later stage's WHERE status='CATALOGUED' query has no way
                # to know this row moved, and would try to act on a path
                # that no longer has a file (confirmed as a real failure
                # during a full-chain dry run: Canonicalize picked up a
                # DupeResolver-relocated row and errored on the missing
                # source). status='DUPE_REVIEW' is a new, distinct status
                # (not CATALOGUED, not GHOST -- this is an intentional,
                # tracked relocation, not a disappearance).
                ctx.conn.execute(
                    "UPDATE archive SET status = 'DUPE_REVIEW', file_path = ? WHERE file_path = ?",
                    (str(target), str(source)),
                )
                ctx.log_event(
                    "DUPE_MOVED_FOR_REVIEW",
                    file_path=str(target),
                    old_value=str(source),
                    new_value=str(target),
                    stage=self.NAME,
                    note=f"group={group_id} type={loser['duplicate_type']} kept={keeper_desc}",
                )
                moved.append((str(source), str(target), group_id, loser["duplicate_type"] or ""))
                result.files_changed += 1
                logger.info("[dupe-resolver] moved %s -> %s", source, target)

            if keeper and not dry_run:
                ctx.conn.execute(
                    "UPDATE duplicates SET status = 'keep' WHERE group_id = ? AND file_path = ?",
                    (group_id, keeper["file_path"]),
                )

        if not dry_run:
            ctx.conn.commit()

        if moved:
            manifest_path, restore_path = self._write_manifest_and_restore_script(ctx, batch_date, moved)
            result.notes.append(f"moved {len(moved)} file(s) to review")
            result.notes.append(f"manifest: {manifest_path}")
            result.notes.append(f"restore script: {restore_path}")
        elif dry_run and result.files_changed:
            result.notes.append(f"[DRY RUN] would move {result.files_changed} file(s) — no manifest written")

        if result.files_errored:
            result.success = False

        ctx.record_stage(result)
        return result

    def dry_run(self, ctx: RunContext) -> StageResult:
        return self._resolve(ctx, dry_run=True)

    def run(self, ctx: RunContext) -> StageResult:
        return self._resolve(ctx, dry_run=False)
