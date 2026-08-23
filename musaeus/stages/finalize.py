#!/usr/bin/env python3
"""
MUSAEUS — Finalize Stage (Act 3)

The last step before a file is considered done: physically moves a
canonicalized (ALAC-in-.m4a or AAC-in-.m4a) file from the mutable INBOX
into the canonical, trusted vault_root/ALAC-Library — Artist/Album/
structure, matching organize.py's naming rules exactly (same
build_track_filename/sanitize_path_component/unique_path helpers,
imported directly rather than reimplemented, so Finalize and Organize
can never silently disagree about what a "correct" path looks like).

Why this matters (Grey's explicit design decision, 2026-08-09/10
session): INBOX is working state, expected to trend toward empty.
ALAC-Library is the trusted result — the only thing downstream export
stages should ever read from. Physical presence of a file in
ALAC-Library is meant to be a DB-independent signal that processing is
actually complete, since a prior incident had an agent claim "100% done"
when nothing had physically moved. Nothing else in the pipeline gets to
call a file "finished" until it has actually arrived here.

What it does:
  - Processes CATALOGUED rows with canonicalized_at set and finalized_at
    NOT set (skips anything Canonicalize hasn't reached yet, or that's
    already finalized, unless --force). The source for a row is wherever
    archive.file_path currently points: STAGING for a CONVERTED/
    TRANSCODED row (Canonicalize's verified output), or still INBOX for
    a PASSTHROUGH row (nothing to stage, codec/container was already
    canonical). Finalize doesn't care which -- it just moves whatever is
    there into ALAC-Library.
  - Copies to a same-directory temp name at the destination, verifies
    size, then renames into place (same-filesystem atomic rename).
    Deleting the source is deliberately a SEPARATE, later step (see
    below) — safer than a raw shutil.move()/os.rename() if ALAC-Library
    lives on a different filesystem than the source and a cross-device
    copy is interrupted partway: nothing removes the source until both
    the destination copy AND the DB row are confirmed, so a crash
    anywhere in between leaves the source untouched rather than losing
    the only copy.
  - DB update is by rowid, disk-change-first (organize.py's
    _apply_rename pattern): if UPDATE ... WHERE id=? hits a
    sqlite3.IntegrityError (archive.file_path is UNIQUE; another row
    already claims that exact target path), the just-created
    ALAC-Library copy is deleted and the source is left completely
    untouched — the row is skipped, not the whole stage.
  - Uses organize.py's unique_path() to avoid clobbering an unrelated
    file that happens to sanitize to the same target filename (with the
    same self-is-not-a-collision guard organize.py needed, for the
    --force-on-an-already-finalized-file edge case)
  - Records archive.audio_hash (computed by Sentinel, unaffected by a
    lossless codec swap — see note below) into the persistent
    cross-batch hash index at config.hash_index_path, so a LATER batch
    (after this one's musaeus.db has been wiped) can still detect that
    this exact audio content already exists in ALAC-Library
  - Cleans up any directories left empty in INBOX after the run
    (ORPHEUS organize_only.py pattern: deepest-first, bare rmdir() in
    try/except OSError — rmdir() itself is the "is this actually empty"
    check, no separate scan needed)
  - dry_run() reports every move without copying/moving/writing anything

What it deliberately does NOT do:
  - Resolve duplicates. A same-batch exact/near duplicate (staged by
    Sentinel/NearDupe in the `duplicates` table) or a cross-batch
    duplicate (flagged separately by the early cross-batch dedupe check
    that runs in Act 2, before Canonicalize) is a human-review decision,
    not something Finalize decides on its own. Finalize only cares
    whether a file is ready to be physically archived, not whether it's
    a duplicate of something else.

Note on audio_hash validity after Canonicalize: Sentinel's audio_hash is
computed once, before Canonicalize runs, and is never recomputed. For a
CONVERTED file (lossless -> ALAC, a straight codec swap with no
resampling), the decoded PCM is unchanged, so the pre-conversion hash
still correctly identifies the audio content. For a TRANSCODED file
(lossy source -> lossy AAC), decoding two different lossy encodings of
the same source produces different PCM regardless — but using the
ORIGINAL source's hash is actually the semantically useful choice for
cross-batch dedup here: it identifies "the same underlying song",
independent of which lossy container it happened to arrive in.
"""

from __future__ import annotations

import logging
import shutil
import sqlite3
from pathlib import Path

from ..context import RunContext, StageResult
from ..db import open_hash_index, record_finalized_hash
from .base import BaseStage
from .organize import build_track_filename, sanitize_path_component, unique_path

logger = logging.getLogger(__name__)

_COMMIT_EVERY = 25


class FinalizeError(Exception):
    """Raised internally when a move or its verification fails."""


def _copy_then_verify_then_swap(source: Path, target: Path) -> None:
    """
    Copy source -> a same-directory temp name at target's destination,
    verify the copy landed intact (size match), rename into place (same
    filesystem, atomic). Deliberately does NOT remove source -- the
    caller only does that after the DB row has been safely updated by
    rowid, so a DB-write collision can still be reverted (by deleting
    this freshly-created target) while the source is completely intact.
    If anything fails before target exists, the source is left
    completely untouched — only the temp file (if any) is cleaned up.
    """
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp_target = target.with_name(target.name + ".finalize_tmp")

    try:
        shutil.copy2(str(source), str(tmp_target))

        src_size = source.stat().st_size
        tmp_size = tmp_target.stat().st_size
        if src_size != tmp_size:
            raise FinalizeError(
                f"size mismatch after copy: source={src_size} bytes, copy={tmp_size} bytes"
            )

        tmp_target.rename(target)  # tmp_target and target share a parent -> atomic

    except Exception:
        if tmp_target.exists():
            tmp_target.unlink(missing_ok=True)
        raise


def _cleanup_empty_dirs(root: Path) -> int:
    """
    Remove any now-empty directories under *root*, deepest first.
    rmdir() itself is the emptiness check — it raises OSError on a
    non-empty directory, which is caught and treated as expected, not
    an error. Returns the number of directories removed.
    """
    if not root.exists():
        return 0
    removed = 0
    dirs = sorted(
        (p for p in root.rglob("*") if p.is_dir()),
        key=lambda d: len(d.parts),
        reverse=True,
    )
    for d in dirs:
        if not d.exists():
            continue
        try:
            d.rmdir()
            removed += 1
        except OSError:
            pass  # not empty — expected, not a failure
    return removed


class FinalizeStage(BaseStage):
    """
    Finalize — move canonicalized files from INBOX into ALAC-Library,
    the trusted, physically-verifiable canonical library.
    """

    NAME = "finalize"

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
            ("CATALOGUED",),
        ).fetchall()
        missing = [r["file_path"] for r in rows if not Path(r["file_path"]).exists()]
        if not rows or not missing:
            return []
        return [
            f"reported {result.files_changed} change(s) but {len(missing)} of "
            f"{len(rows)} sampled CATALOGUED rows name a file that is not on disk"
        ]

    def validate(self, ctx: RunContext) -> None:
        count = ctx.conn.execute(
            """
            SELECT COUNT(*) FROM archive
             WHERE status='CATALOGUED' AND canonicalized_at IS NOT NULL
            """
        ).fetchone()[0]
        logger.info("[finalize] %d canonicalized file(s) ready to finalize", count)

    def _get_pending(self, ctx: RunContext, force: bool) -> list[dict]:
        if force:
            rows = ctx.conn.execute(
                """
                SELECT id, file_path, artist, album, title, audio_hash
                  FROM archive
                 WHERE status='CATALOGUED' AND canonicalized_at IS NOT NULL
                 ORDER BY file_path
                """
            ).fetchall()
        else:
            rows = ctx.conn.execute(
                """
                SELECT id, file_path, artist, album, title, audio_hash
                  FROM archive
                 WHERE status='CATALOGUED'
                   AND canonicalized_at IS NOT NULL
                   AND (finalized_at IS NULL OR finalized_at = '')
                 ORDER BY file_path
                """
            ).fetchall()
        return [dict(r) for r in rows]

    def _batch_date(self, ctx: RunContext) -> str:
        """
        YYYY-MM-DD stamp for this batch's top-level ALAC-Library folder
        (Grey's explicit request: a dated folder above everything, so a
        whole batch can be copied to cold-storage archives in one shot).
        Overridable via ctx.set("finalize_batch_date", ...) for tests --
        without an override, every file finalized in the same run gets
        the same stamp (computed once, not per-file, so a run spanning
        midnight doesn't split one batch across two date folders).
        """
        override = ctx.get("finalize_batch_date")
        if override:
            return str(override)
        from datetime import datetime, timezone

        return datetime.now(tz=timezone.utc).strftime("%Y-%m-%d")

    def _target_path(self, ctx: RunContext, row: dict, source: Path) -> Path:
        artist = row.get("artist") or "Unknown Artist"
        album = row.get("album") or "Unsorted"
        title = row.get("title") or "Unknown Title"

        new_filename = build_track_filename(artist, title, source.suffix)
        artist_safe = sanitize_path_component(artist)
        album_safe = sanitize_path_component(album)

        target_dir = ctx.alac_library / self._batch_date(ctx) / artist_safe / album_safe
        candidate = target_dir / new_filename

        # Same self-is-not-a-collision guard organize.py needed: if the
        # file is already exactly where it belongs (e.g. --force on an
        # already-finalized row), unique_path()'s disk-existence check
        # would otherwise see the file's OWN current location as "taken"
        # and wrongly bump it to " (2)".
        if candidate == source:
            return candidate
        return unique_path(candidate)

    def _index_hash_before_finalizing(
        self, hash_conn: sqlite3.Connection, row: dict, target: Path
    ) -> None:
        """
        Write + commit the hash-index entry, synchronously, before the
        caller marks this row finalized_at. 2026-08-18 fix: previously
        this write happened AFTER finalized_at was set (and only batched
        into hash_conn.commit() every _COMMIT_EVERY rows, or skipped
        outright if audio_hash was somehow falsy) -- a crash between the
        two, or a genuinely missing audio_hash, meant "a row can be
        marked finalized without this write completing," exactly the gap
        MUSAEUS_OPEN_ITEMS.md tracked. Reversing the order means the
        worst case is now the safe direction: an indexed hash for a row
        not yet marked finalized (harmless -- record_finalized_hash is
        INSERT OR IGNORE, so a retry next run is a no-op), never a
        finalized row missing its index entry.

        A missing audio_hash is logged loudly rather than silently
        skipped -- Sentinel is expected to have hashed every row that
        reaches Finalize, so this would itself be worth investigating,
        not just a routine skip.
        """
        audio_hash = row.get("audio_hash")
        if not audio_hash:
            logger.warning(
                "[finalize] %s has no audio_hash at finalize time -- "
                "will NOT be in the cross-batch hash index",
                target,
            )
            return
        record_finalized_hash(hash_conn, audio_hash, str(target))
        hash_conn.commit()

    # ── run ───────────────────────────────────────────────────────────────────

    def run(self, ctx: RunContext) -> StageResult:
        result = self._make_result(dry_run=False)
        force: bool = ctx.get("finalize_force", False)
        pending = self._get_pending(ctx, force)

        total = len(pending)
        result.notes.append(f"files to finalize: {total}")
        if not total:
            result.notes.append("nothing to do — no canonicalized files pending finalize")
            ctx.record_stage(result)
            return result

        hash_conn = open_hash_index(ctx.config.hash_index_path)
        indexed = 0

        try:
            for i, row in enumerate(pending, 1):
                source = Path(row["file_path"])
                result.files_processed += 1

                if not source.exists():
                    result.files_errored += 1
                    result.errors.append(f"{source}: file missing on disk")
                    logger.warning("[finalize] missing: %s", source)
                    continue

                target = self._target_path(ctx, row, source)

                if target == source:
                    try:
                        self._index_hash_before_finalizing(hash_conn, row, target)
                    except sqlite3.Error as exc:
                        result.files_errored += 1
                        result.errors.append(f"{source.name}: hash-index write failed: {exc}")
                        logger.warning("[finalize] %s: hash-index write failed: %s", source, exc)
                        continue
                    result.files_skipped += 1
                    if row.get("audio_hash"):
                        indexed += 1
                    ctx.conn.execute(
                        "UPDATE archive SET finalized_at = datetime('now') WHERE id = ?",
                        (row["id"],),
                    )
                    continue

                try:
                    _copy_then_verify_then_swap(source, target)
                except (FinalizeError, OSError) as exc:
                    result.files_errored += 1
                    result.errors.append(f"{source.name}: {exc}")
                    logger.warning("[finalize] %s: %s", source, exc)
                    continue

                # Hash-index write happens BEFORE the row is marked
                # finalized, not after (2026-08-18 fix -- see
                # _index_hash_before_finalizing's docstring): a row must
                # never be able to reach finalized_at without its
                # cross-batch hash entry already durably committed. A
                # write failure here is a per-row error (file already
                # copied, but not yet finalized -- safe to retry next
                # run), not a whole-stage crash.
                try:
                    self._index_hash_before_finalizing(hash_conn, row, target)
                except sqlite3.Error as exc:
                    result.files_errored += 1
                    result.errors.append(f"{source.name}: hash-index write failed: {exc}")
                    logger.warning("[finalize] %s: hash-index write failed: %s", source, exc)
                    continue
                if row.get("audio_hash"):
                    indexed += 1

                # organize.py's _apply_rename pattern: disk change (the
                # copy into ALAC-Library) already happened above; the DB
                # write is the only thing that can still fail (a UNIQUE
                # collision on archive.file_path). If it does, delete the
                # just-created ALAC-Library copy and leave source
                # completely untouched, instead of losing track of it.
                try:
                    ctx.conn.execute(
                        """
                        UPDATE archive
                           SET file_path = ?, finalized_at = datetime('now')
                         WHERE id = ?
                        """,
                        (str(target), row["id"]),
                    )
                except sqlite3.IntegrityError as exc:
                    logger.error(
                        "[finalize] DB collision for row %s -> %s (%s); "
                        "reverting ALAC-Library copy, source untouched",
                        row["id"],
                        target,
                        exc,
                    )
                    try:
                        target.unlink(missing_ok=True)
                    except OSError as revert_exc:
                        logger.error(
                            "[finalize] COULD NOT REVERT %s -- disk/DB now out of "
                            "sync, needs manual fix: %s",
                            target,
                            revert_exc,
                        )
                    result.files_errored += 1
                    result.errors.append(f"{source.name}: DB collision on {target}: {exc}")
                    ctx.log_event(
                        "FINALIZE_DB_COLLISION",
                        file_path=str(target),
                        old_value=str(source),
                        new_value=None,
                        stage=self.NAME,
                        note=str(exc),
                    )
                    continue

                ctx.log_event(
                    "FINALIZE_MOVE",
                    file_path=str(target),
                    old_value=str(source),
                    new_value=str(target),
                    stage=self.NAME,
                )

                # Only now, with the DB row safely pointing at the
                # ALAC-Library copy, is it safe to remove the source --
                # STAGING for a converted/transcoded row, or INBOX for a
                # passthrough row. Either way this is what keeps STAGING
                # trending back to empty.
                try:
                    source.unlink()
                except OSError as exc:
                    logger.warning(
                        "[finalize] %s finalized to %s but source could not be removed: %s",
                        source,
                        target,
                        exc,
                    )
                    result.notes.append(f"  WARNING: {source} not removed after finalize: {exc}")

                result.files_changed += 1
                logger.info("[finalize] %s -> %s", source, target)

                if i % _COMMIT_EVERY == 0:
                    ctx.conn.commit()
                    hash_conn.commit()
                    logger.info("finalize: checkpoint %d/%d", i, total)

            ctx.conn.commit()
            hash_conn.commit()
        finally:
            hash_conn.close()

        removed_dirs = _cleanup_empty_dirs(ctx.inbox)

        result.notes.append(f"  moved: {result.files_changed}")
        result.notes.append(f"  hash-indexed: {indexed}")
        if removed_dirs:
            result.notes.append(f"  removed {removed_dirs} now-empty INBOX folder(s)")
        if result.files_errored:
            result.notes.append(f"  errors: {result.files_errored}")
            result.success = False

        ctx.record_stage(result)
        return result

    # ── dry_run ───────────────────────────────────────────────────────────────

    def dry_run(self, ctx: RunContext) -> StageResult:
        result = self._make_result(dry_run=True)
        force: bool = ctx.get("finalize_force", False)
        pending = self._get_pending(ctx, force)
        total = len(pending)

        result.files_processed = total
        result.files_changed = total
        result.notes.append(f"[DRY RUN] would finalize {total} file(s)")

        shown = 0
        for row in pending:
            source = Path(row["file_path"])
            if not source.exists():
                continue
            target = self._target_path(ctx, row, source)
            if shown < 10:
                result.notes.append(f"  {source.name} -> {target}")
                shown += 1
        if total > shown:
            result.notes.append(f"  ... and {total - shown} more")

        result.notes.append("  no files will be written, no DB changes")

        ctx.record_stage(result)
        return result
