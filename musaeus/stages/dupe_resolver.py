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

Two resolution sources feed the same move+manifest logic (Grey's
2026-08-12 fix, after a confirmed incident -- see below):
  1. duplicates-table-driven groups (status='pending') -- NEAR,
     CROSS_BATCH, and any freshly-detected EXACT group.
  2. Live EXACT-hash clusters, derived directly from archive.audio_hash
     collisions among CATALOGUED rows (_get_live_exact_clusters),
     bypassing duplicates.status entirely. This exists because
     `musaeus dedupe --auto`/manual review only ever flips
     duplicates.status -- it never moves a file, and duplicates.file_path
     goes stale the moment a file is later finalized to a new path by
     the normal pipeline. Before this fix, that meant an EXACT decision
     made via `musaeus dedupe` was silently never enforced: confirmed in
     the real vault, 6,434 EXACT-type duplicates rows had a stale
     'archive' decision that nothing downstream ever acted on, and 3,480
     collision-suffixed filenames (e.g. "... (2).m4a") landed in
     ALAC-Library in one night as a direct, physical consequence -- both
     copies of each pair got canonicalized and finalized side by side.
     audio_hash survives every move Canonicalize/Finalize make, so
     re-deriving live duplicate clusters from it (rather than trying to
     reconcile a stale path back to an old decision) catches this
     historical backlog and any future recurrence in one mechanism.

Every move is recorded in a per-run manifest CSV (source, destination,
group_id, duplicate_type, moved_codec, moved_bitrate, kept_path,
kept_codec, kept_bitrate -- the codec/bitrate columns exist so a human
reviewing the CSV can see the actual signal behind each decision at a
glance, not just the decision itself) plus an auto-generated bash
restore script (mkdir -p + mv -n pairs, chmod +x) -- ORPHEUS's own
move_lesser_dedupe_candidates.py pattern, ported directly: never
destructive, always reversible from one file.

What this stage deliberately does NOT do:
  - Decide whether something IS a duplicate. That's Sentinel/CrossDupe/
    NearDupe's job, already done by the time this stage runs, for
    everything except the live-EXACT-hash-cluster path above, which
    re-derives the keeper fresh from current data since a stale prior
    decision can't be reliably replayed.
  - Touch groups/rows already resolved -- a duplicates-table group with
    nothing left in 'pending', or an archive row already at
    status='DUPE_REVIEW', is left alone. Re-running this stage is safe.
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
        return str(override)
    return datetime.now(tz=timezone.utc).strftime("%Y-%m-%d")


def _get_pending_groups(conn) -> list[str]:
    rows = conn.execute(
        "SELECT DISTINCT group_id FROM duplicates WHERE status = 'pending' ORDER BY group_id"
    ).fetchall()
    return [r[0] for r in rows]


def _keeper_sort_key(m: dict) -> tuple[int, int, int]:
    """Shared ordering rule: real lossless codec beats lossy
    UNCONDITIONALLY (a bitrate/size comparison across different codecs
    isn't a fair quality comparison -- a quiet, highly-compressible FLAC
    can report a lower bitrate than a dense, less-compressible lossy
    file despite being the objectively better copy), then bitrate/size
    as a tiebreak among files that are equally lossless or equally
    lossy. Used both for duplicates-table-driven groups and for
    audio_hash-derived live EXACT clusters (see
    _get_live_exact_clusters) -- one rule, not two copies of it."""
    return (
        0 if (m.get("codec") or "").lower() in LOSSLESS_CODECS else 1,
        -(m.get("bitrate") or 0),
        -(m.get("size_bytes") or 0),
    )


def _get_group_members(conn, group_id: str) -> list[dict]:
    """Members of one duplicate group, per the duplicates table, sorted
    by _keeper_sort_key (best keeper candidate first)."""
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
    members.sort(key=_keeper_sort_key)
    return members


def _get_live_exact_clusters(conn) -> list[list[dict]]:
    """
    Find clusters of CATALOGUED archive rows sharing the same audio_hash
    -- i.e. exact-content duplicates still sitting live in the pipeline
    right now -- derived directly from archive.audio_hash rather than
    the duplicates table's own bookkeeping.

    This is a deliberate, confirmed-necessary bypass, not a stylistic
    choice (2026-08-12 incident): `musaeus dedupe --auto`/manual review
    only ever flips duplicates.status to 'keep'/'archive' -- it never
    moves a file or touches archive.file_path or archive.status. And
    duplicates.file_path itself goes stale the moment a file is later
    finalized to a new path by the normal pipeline (INBOX -> STAGING ->
    ALAC-Library), which happens to EVERY file regardless of any
    dedupe.py decision, since nothing downstream consults
    duplicates.status at all. The result, confirmed in the real vault:
    6,434 EXACT-type duplicates rows had a stale 'archive' decision that
    was never physically enforced, and 3,480 collision-suffixed
    filenames (e.g. "... (2).m4a") landed in ALAC-Library in a single
    night as a direct, physical consequence -- both the "keeper" and the
    "archived" copy of each pair were canonicalized and finalized side
    by side, because nothing after Sentinel/dedupe.py ever looked at
    duplicates.status again.

    audio_hash is the fix: Canonicalize/Finalize update file_path and
    status but never touch audio_hash, so it stays a reliable identity
    key across any number of moves -- unlike file_path, which is exactly
    what goes stale. Querying live CATALOGUED rows by audio_hash finds
    every still-unresolved exact-content duplicate regardless of what
    duplicates.status claims, catching both this historical backlog and
    any future recurrence in one mechanism, rather than trying to
    reconcile stale paths back to old decisions that can no longer be
    reliably replayed.
    """
    rows = conn.execute(
        """
        SELECT audio_hash, COUNT(*) as n
          FROM archive
         WHERE status = 'CATALOGUED' AND audio_hash IS NOT NULL AND audio_hash != ''
         GROUP BY audio_hash
        HAVING COUNT(*) > 1
        """
    ).fetchall()

    clusters: list[list[dict]] = []
    for row in rows:
        members = conn.execute(
            """
            SELECT file_path, artist, album, title, ext, codec, bitrate, size_bytes
              FROM archive
             WHERE audio_hash = ? AND status = 'CATALOGUED'
            """,
            (row["audio_hash"],),
        ).fetchall()
        member_dicts = [dict(m) for m in members]
        member_dicts.sort(key=_keeper_sort_key)
        clusters.append(member_dicts)
    return clusters


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
        self, ctx: RunContext, batch_date: str, moves: list[dict]
    ) -> tuple[Path, Path]:
        """
        moves: list of dicts (source, destination, group_id,
        duplicate_type, moved_codec, moved_bitrate, kept_path,
        kept_codec, kept_bitrate). The codec/bitrate columns exist so a
        human reviewing the CSV can see, per row, the actual signal that
        drove each decision at a glance -- Grey's explicit ask
        (2026-08-12): without them, reviewing any single group meant
        manually joining archive.codec/bitrate by hand, which isn't
        workable across thousands of rows.
        Returns (manifest_path, restore_script_path).
        """
        review_dir = ctx.config.dupes_review_dir / batch_date
        review_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(tz=timezone.utc).strftime("%Y%m%dT%H%M%SZ")

        fieldnames = [
            "source",
            "destination",
            "group_id",
            "duplicate_type",
            "moved_codec",
            "moved_bitrate",
            "kept_path",
            "kept_codec",
            "kept_bitrate",
        ]
        manifest_path = review_dir / f"moved_manifest_{stamp}.csv"
        with open(manifest_path, "w", newline="", encoding="utf-8") as fh:
            writer = csv.DictWriter(fh, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(moves)

        restore_path = review_dir / f"restore_{stamp}.sh"
        lines = ["#!/usr/bin/env bash", "set -euo pipefail", ""]
        for m in moves:
            src, dst = m["source"], m["destination"]
            src_dir = os.path.dirname(src)
            lines.append(f'mkdir -p "{src_dir}"')
            lines.append(f'mv -n "{dst}" "{src}"')
        restore_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        restore_path.chmod(restore_path.stat().st_mode | stat.S_IEXEC)

        return manifest_path, restore_path

    def _move_losers(
        self,
        ctx: RunContext,
        result: StageResult,
        batch_date: str,
        moved: list[dict],
        group_id: str,
        keeper: dict | None,
        losers: list[dict],
        dry_run: bool,
        update_duplicates_table: bool,
        already_moved: dict[str, str],
    ) -> None:
        """
        Shared per-loser move+update+log+manifest-append logic, used by
        both resolution sources in _resolve() (duplicates-table-driven
        groups, and audio_hash-derived live EXACT clusters). Mutates
        result and moved in place.

        already_moved: source-path -> group_id that already resolved it,
        accumulated across every group processed so far THIS stage run.
        Members list is a snapshot taken once per group from _get_group_members/
        _get_live_exact_clusters -- it does not see moves made by other
        groups processed earlier in the same _resolve() call. A single
        physical file frequently gets staged into more than one group
        (e.g. flagged by both the EXACT/NEAR detector and CROSS_BATCH
        detector), so without this check, the second group to reach that
        file finds it already gone from its original path and misreports
        a legitimate prior move as "file missing on disk" -- confirmed
        as the dominant cause of a 51,310-error DupeResolver failure
        (2026-08-14 run): ~19,000 of those were exactly this, not real
        errors.
        """
        keeper_desc = keeper["file_path"] if keeper else "(no keeper on record)"
        for loser in losers:
            result.files_processed += 1
            source = Path(loser["file_path"])
            source_key = str(source)
            dtype = loser.get("duplicate_type") or ""

            prior_group = already_moved.get(source_key)
            if prior_group is not None:
                result.files_skipped += 1
                result.notes.append(
                    f"[{dtype}] skipped {source.name}: already resolved under group {prior_group}"
                )
                continue

            if not source.exists():
                result.files_errored += 1
                result.errors.append(f"{source}: file missing on disk")
                continue

            target = self._target_path(ctx, loser, source, batch_date)

            if dry_run:
                result.notes.append(
                    f"[{dtype}] would move {source.name} -> {target} (keeping {keeper_desc})"
                )
                result.files_changed += 1
                already_moved[source_key] = group_id
                continue

            try:
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(source), str(target))
            except OSError as exc:
                result.files_errored += 1
                result.errors.append(f"{source}: {exc}")
                logger.warning("[dupe-resolver] move failed %s: %s", source, exc)
                continue

            if update_duplicates_table:
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
                note=f"group={group_id} type={dtype} kept={keeper_desc}",
            )
            moved.append(
                {
                    "source": str(source),
                    "destination": str(target),
                    "group_id": group_id,
                    "duplicate_type": dtype,
                    "moved_codec": loser.get("codec") or "",
                    "moved_bitrate": loser.get("bitrate") or "",
                    "kept_path": keeper["file_path"] if keeper else "",
                    "kept_codec": (keeper.get("codec") or "") if keeper else "",
                    "kept_bitrate": (keeper.get("bitrate") or "") if keeper else "",
                }
            )
            result.files_changed += 1
            already_moved[source_key] = group_id
            logger.info("[dupe-resolver] moved %s -> %s", source, target)

    def _resolve(self, ctx: RunContext, dry_run: bool) -> StageResult:
        result = self._make_result(dry_run=dry_run)
        groups = _get_pending_groups(ctx.conn)
        live_exact_clusters = _get_live_exact_clusters(ctx.conn)
        result.notes.append(f"pending duplicate group(s): {len(groups)}")
        if live_exact_clusters:
            result.notes.append(
                f"live EXACT-hash cluster(s) needing resolution "
                f"(audio_hash collisions among CATALOGUED rows, independent of "
                f"duplicates-table state): {len(live_exact_clusters)}"
            )

        if not groups and not live_exact_clusters:
            result.notes.append(
                "nothing to resolve — no pending duplicate groups or live exact clusters"
            )
            ctx.record_stage(result)
            return result

        batch_date = _batch_date(ctx)
        moved: list[dict] = []
        # Source-path -> group_id, accumulated across every group processed
        # in this _resolve() call. See _move_losers' docstring: a single
        # physical file often gets staged into more than one group, and
        # without this a later group misreports an earlier group's
        # successful move as "file missing on disk".
        already_moved: dict[str, str] = {}

        # ── Source 1: duplicates-table-driven groups (NEAR, CROSS_BATCH,
        # and any freshly-detected EXACT group still genuinely 'pending') ──
        for group_id in groups:
            members = _get_group_members(ctx.conn, group_id)
            if not members:
                continue
            keeper, losers = _pick_keeper_and_losers(members)
            self._move_losers(
                ctx,
                result,
                batch_date,
                moved,
                group_id,
                keeper,
                losers,
                dry_run=dry_run,
                update_duplicates_table=True,
                already_moved=already_moved,
            )
            if keeper and not dry_run:
                ctx.conn.execute(
                    "UPDATE duplicates SET status = 'keep' WHERE group_id = ? AND file_path = ?",
                    (group_id, keeper["file_path"]),
                )

        # ── Source 2: live EXACT-hash clusters, derived directly from
        # archive.audio_hash -- catches both the historical backlog left
        # behind by a stale duplicates.status decision, and any future
        # recurrence, without trying to reconcile a path that may no
        # longer be reliable. See _get_live_exact_clusters' docstring. ──
        for idx, members in enumerate(live_exact_clusters):
            if len(members) < 2:
                continue
            for m in members:
                m["duplicate_type"] = "EXACT"
            keeper, losers = members[0], members[1:]
            synthetic_group_id = f"exacthash_{idx:06d}"
            self._move_losers(
                ctx,
                result,
                batch_date,
                moved,
                synthetic_group_id,
                keeper,
                losers,
                dry_run=dry_run,
                update_duplicates_table=False,
                already_moved=already_moved,
            )

        if not dry_run:
            ctx.conn.commit()

        if moved:
            manifest_path, restore_path = self._write_manifest_and_restore_script(
                ctx, batch_date, moved
            )
            result.notes.append(f"moved {len(moved)} file(s) to review")
            result.notes.append(f"manifest: {manifest_path}")
            result.notes.append(f"restore script: {restore_path}")
        elif dry_run and result.files_changed:
            result.notes.append(
                f"[DRY RUN] would move {result.files_changed} file(s) — no manifest written"
            )

        if result.files_errored:
            result.success = False

        ctx.record_stage(result)
        return result

    def dry_run(self, ctx: RunContext) -> StageResult:
        return self._resolve(ctx, dry_run=True)

    def run(self, ctx: RunContext) -> StageResult:
        return self._resolve(ctx, dry_run=False)
