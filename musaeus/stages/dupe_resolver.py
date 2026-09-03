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
import re
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


def _connected_groups(conn, group_ids: list[str]) -> list[list[str]]:
    """Merge groups that share a file into single components.

    Groups overlap. NEAR matching is fuzzy, so one recording lands in
    several groups, and resolving each independently lets them contradict
    each other: measured on the live vault 2026-08-31, 2,918 NEAR files sit
    in more than one group, and one -- "Al Green - Let's Stay Together" --
    is marked `keep` in near_f122326e and `archive` in near_2231ee82 at the
    same time. Nothing reconciles those. Which one wins is decided by
    whichever group happens to be processed last.

    `already_moved` in _resolve() was the previous mitigation, but it only
    stops a later group MISREPORTING an earlier group's move as "file
    missing". It does not stop that group deciding to move a file an
    earlier group had chosen to keep -- it just makes the outcome quiet.

    Merging first makes the contradiction unrepresentable: one keeper per
    component, and every other member of it is a loser. This is the same
    correction applied by hand to the 102 ACOUSTIC groups on 2026-08-31,
    where 102 groups collapsed to 95 components.

    EXACT clusters keyed by audio_hash cannot overlap -- a file has exactly
    one hash -- so this only concerns the duplicates-table path.
    """
    if not group_ids:
        return []

    parent: dict[str, str] = {g: g for g in group_ids}

    def find(x: str) -> str:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a: str, b: str) -> None:
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[ra] = rb

    placeholders = ",".join("?" for _ in group_ids)
    rows = conn.execute(
        f"""
        SELECT file_path, group_id FROM duplicates
         WHERE group_id IN ({placeholders}) AND status = 'pending'
        """,
        group_ids,
    ).fetchall()

    by_path: dict[str, list[str]] = {}
    for r in rows:
        by_path.setdefault(r["file_path"], []).append(r["group_id"])
    for shared in by_path.values():
        for g in shared[1:]:
            if g in parent and shared[0] in parent:
                union(shared[0], g)

    components: dict[str, list[str]] = {}
    for g in group_ids:
        components.setdefault(find(g), []).append(g)
    return [sorted(v) for v in components.values()]


def _get_pending_groups(conn) -> list[str]:
    rows = conn.execute(
        "SELECT DISTINCT group_id FROM duplicates WHERE status = 'pending' ORDER BY group_id"
    ).fetchall()
    return [r[0] for r in rows]


# Markers of a reissued/reprocessed version rather than the original
# release. Grey's rule (2026-08-21): a remaster and its original are the
# SAME song for grouping purposes, but when one must be kept, "the
# original trumps the remaster".
_REISSUE_MARKERS: tuple[str, ...] = (
    "remaster",
    "remastered",
    "remix",
    "re-recorded",
    "rerecorded",
    "anniversary edition",
    "deluxe edition",
    "expanded edition",
    "digital remaster",
)
_REISSUE_RE = re.compile(
    r"\b(?:"
    + "|".join(re.escape(w) for w in sorted(_REISSUE_MARKERS, key=len, reverse=True))
    + r")\b",
    re.IGNORECASE,
)


# A live recording is a different performance, not a worse copy -- but when
# a duplicate group holds both and only one can be kept, Grey's rule
# (confirmed 2026-08-22) is studio first. Ranked BELOW the reissue test so
# a studio remaster does not beat a live original on this alone; the codec
# constraint still outranks both.
_LIVE_MARKERS: tuple[str, ...] = (
    "live",
    "in concert",
    "unplugged",
    "concert",
    "at the bbc",
    "bbc session",
    "radio session",
    "live session",
)
_LIVE_RE = re.compile(
    r"\b(?:"
    + "|".join(re.escape(w) for w in sorted(_LIVE_MARKERS, key=len, reverse=True))
    + r")\b",
    re.IGNORECASE,
)


def _is_live(m: dict) -> bool:
    """True if this copy advertises itself as a live recording.

    Read from title AND album: sources put it in either -- "Stormy Monday
    (live at Fillmore East)" as a title, "Unplugged" as an album.
    """
    haystack = f"{m.get('title') or ''} {m.get('album') or ''}"
    return bool(_LIVE_RE.search(haystack))


def _is_reissue(m: dict) -> bool:
    """True if this copy advertises itself as a remaster/reissue.

    Read from title AND album, because the marker lands in either
    depending on the source -- "A Monday Date (Remastered)" as a title,
    "Chicago High Life (2013 Remaster)" as an album.
    """
    haystack = f"{m.get('title') or ''} {m.get('album') or ''}"
    return bool(_REISSUE_RE.search(haystack))


def _keeper_sort_key(m: dict) -> tuple[int, int, int, int, int]:
    """Shared ordering rule: real lossless codec beats lossy
    UNCONDITIONALLY (a bitrate/size comparison across different codecs
    isn't a fair quality comparison -- a quiet, highly-compressible FLAC
    can report a lower bitrate than a dense, less-compressible lossy
    file despite being the objectively better copy), THEN the original
    release beats a remaster/reissue, then bitrate/size as a tiebreak
    among files that are equally lossless or equally lossy.

    The original-over-remaster rank sits BELOW codec deliberately: a
    lossless remaster is still a better artifact than a lossy original,
    and Grey's preference is about which *release* to keep, not a licence
    to keep a worse file. Above bitrate, though -- a remaster is often
    louder and larger without being the version wanted.

    Third rank, studio over live, added 2026-08-22. It sits below the
    reissue test so it only decides groups the earlier tests tie on, and
    well below codec: a lossless live take still beats a lossy studio one,
    because the constraint that matters most is what was thrown away in
    encoding, not which room it was recorded in.

    Used both for duplicates-table-driven groups and for
    audio_hash-derived live EXACT clusters (see
    _get_live_exact_clusters) -- one rule, not two copies of it."""
    return (
        0 if (m.get("codec") or "").lower() in LOSSLESS_CODECS else 1,
        1 if _is_reissue(m) else 0,
        1 if _is_live(m) else 0,
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

    def verify_effect(self, ctx: RunContext, result: StageResult) -> list[str]:
        """A file this stage claims to have moved must be AT the new path.

        Moves are the costliest thing to get silently wrong: a stage that
        reports "moved 6,480 files" while the DB and disk disagree leaves
        rows pointing at nothing, and that is precisely how a file ended up
        treated as its own duplicate (scope doc section 4.17). Sampling a
        few is enough to catch a wholesale failure.
        """
        rows = ctx.conn.execute(
            "SELECT file_path FROM archive WHERE status = ? ORDER BY last_seen DESC LIMIT 5",
            ("DUPE_REVIEW",),
        ).fetchall()
        missing = [r["file_path"] for r in rows if not Path(r["file_path"]).exists()]
        if not rows or not missing:
            return []
        return [
            f"reported {result.files_changed} change(s) but {len(missing)} of "
            f"{len(rows)} sampled DUPE_REVIEW rows name a file that is not on disk"
        ]

    @classmethod
    def plan_candidates(cls, conn, cfg) -> tuple[int, str]:
        """Rows this stage would act on. Read-only; see planner.py."""
        n = conn.execute(
            "SELECT COUNT(DISTINCT group_id) FROM duplicates WHERE status='pending'"
        ).fetchone()[0]
        return int(n), "duplicate groups awaiting resolution"

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
                # Before treating this as a real error: the same physical
                # file may already have been resolved by a *different*
                # group in an earlier run (a title re-flagged as a
                # duplicate a second time after already being quarantined
                # once -- e.g. re-downloaded and re-detected). The
                # `already_moved` dict above only covers moves made earlier
                # in *this* _resolve() call; it can't see a prior run's
                # moves. The events log is the one place that history is
                # actually recorded (old_value=source at the time of the
                # original move), so check it before giving up. Confirmed
                # 2026-08-18: 422 duplicates-table rows stuck 'pending'
                # forever this way, all 370 distinct paths already moved
                # per a matching DUPE_MOVED_FOR_REVIEW event -- not lost
                # files, just a group that never got told its file was
                # already handled elsewhere.
                already_handled = ctx.conn.execute(
                    "SELECT 1 FROM events WHERE event_type = 'DUPE_MOVED_FOR_REVIEW' "
                    "AND old_value = ? LIMIT 1",
                    (source_key,),
                ).fetchone()
                if already_handled:
                    result.files_skipped += 1
                    result.notes.append(
                        f"[{dtype}] skipped {source.name}: already resolved by a prior run "
                        f"(stale duplicates-table row)"
                    )
                    if update_duplicates_table and not dry_run:
                        ctx.conn.execute(
                            "UPDATE duplicates SET status = 'archive' "
                            "WHERE group_id = ? AND file_path = ?",
                            (group_id, source_key),
                        )
                    continue

                # Before calling it lost, ask the archive row where the file
                # lives now. The events check above only recognises a move
                # this stage itself made; a file relocated by any other
                # stage -- ClassicalComposer refiling under a composer, a
                # manual DUPE_REVIEW_REVERSED restore -- is equally moved,
                # and equally not missing. Measured 2026-08-25: five such
                # rows failed the whole stage (rc=1) when every one of the
                # files was safely on disk under a new path.
                relocated = ctx.conn.execute(
                    "SELECT file_path FROM archive WHERE file_path != ? AND rowid IN "
                    "(SELECT rowid FROM archive WHERE file_path = ?) LIMIT 1",
                    (source_key, source_key),
                ).fetchone()
                moved_elsewhere = None
                if relocated is None:
                    ev = ctx.conn.execute(
                        "SELECT file_path FROM events WHERE old_value = ? "
                        "AND file_path IS NOT NULL ORDER BY id DESC LIMIT 1",
                        (source_key,),
                    ).fetchone()
                    if ev and Path(ev["file_path"]).exists():
                        moved_elsewhere = ev["file_path"]
                if moved_elsewhere:
                    result.files_skipped += 1
                    result.notes.append(
                        f"[{dtype}] skipped {source.name}: relocated by another stage"
                    )
                    if update_duplicates_table and not dry_run:
                        ctx.conn.execute(
                            "UPDATE duplicates SET status = 'archive' "
                            "WHERE group_id = ? AND file_path = ?",
                            (group_id, source_key),
                        )
                    continue

                # No file, and no archive row either: this duplicates-table
                # entry names a path the library stopped tracking long ago
                # -- an old INBOX path from before ingest moved the file, or
                # one already handled by a DUPE_RESTORED/STALE_ROW_DROPPED.
                # It is stale history, not a lost file.
                #
                # Safe to key on the missing row: a genuinely lost file
                # KEEPS its archive row pointing at the gone path, and
                # doctor's "rows with a missing file" check is what catches
                # that. This branch only fires where the library itself has
                # no record of the path at all.
                still_tracked = ctx.conn.execute(
                    "SELECT 1 FROM archive WHERE file_path = ? LIMIT 1", (source_key,)
                ).fetchone()
                if not still_tracked:
                    result.files_skipped += 1
                    result.notes.append(
                        f"[{dtype}] skipped {source.name}: stale duplicates-table row, "
                        f"no archive row for this path"
                    )
                    if update_duplicates_table and not dry_run:
                        ctx.conn.execute(
                            "UPDATE duplicates SET status = 'archive' "
                            "WHERE group_id = ? AND file_path = ?",
                            (group_id, source_key),
                        )
                    continue

                result.files_errored += 1
                result.errors.append(f"{source}: file missing on disk")
                continue

            # Inside the guard, not above it. The move below has isolated
            # per-item OSErrors since it was written, but _target_path was
            # one line outside that guard -- and it does filesystem work
            # (unique_path's exists() check), so it raises the same class of
            # error. On 2026-09-03 one 388-byte artist name raised OSError 36
            # here and aborted every remaining move in the run, leaving the
            # dedupe half-done. One bad path should cost one file, not the
            # stage. No rollback needed: nothing has been written yet.
            try:
                target = self._target_path(ctx, loser, source, batch_date)
            except OSError as exc:
                result.files_errored += 1
                result.errors.append(f"{source}: cannot build a target path: {exc}")
                logger.warning("[dupe-resolver] target path failed %s: %s", source, exc)
                continue

            if dry_run:
                result.notes.append(
                    f"[{dtype}] would move {source.name} -> {target} (keeping {keeper_desc})"
                )
                result.files_changed += 1
                already_moved[source_key] = group_id
                continue

            # Row first, then the move (scope section 4.25). A move cannot
            # be rolled back and a database write can, so this ordering
            # leaves neither half applied when something fails.
            ctx.conn.execute(
                "UPDATE archive SET status = 'DUPE_REVIEW', file_path = ? WHERE file_path = ?",
                (str(target), str(source)),
            )
            try:
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(source), str(target))
            except OSError as exc:
                ctx.conn.rollback()
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
        # Resolve COMPONENTS, not groups. Overlapping groups otherwise reach
        # contradictory verdicts on the same file -- see _connected_groups.
        for component in _connected_groups(ctx.conn, groups):
            group_id = component[0]
            members = []
            seen_paths: set[str] = set()
            for gid in component:
                for m in _get_group_members(ctx.conn, gid):
                    if m["file_path"] in seen_paths:
                        continue
                    seen_paths.add(m["file_path"])
                    members.append(m)
            if not members:
                continue
            # One keeper for the whole component, so a file kept by one of
            # its groups can no longer be moved as another's loser.
            members.sort(key=_keeper_sort_key)
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
