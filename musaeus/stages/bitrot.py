#!/usr/bin/env python3
"""
MUSAEUS — Bit-Rot Check Stage (standalone, not wired into DEFAULT_PIPELINE)

Detects silent corruption in ALAC_Archive -- the pristine, permanent
tier -- by comparing each file's current SHA-256 against a baseline
recorded in archive_tier_hashes. Ported from ORPHEUS's
orpheus_integrity_check.py (both --generate and --verify modes), per
tonight's 222-script ORPHEUS salvage audit.

Second design, same session (2026-08-19): the first version compared
against archive.full_hash instead. Live-vault testing found that stale
for nearly every finalized file -- full_hash is computed by Sentinel
early in the pipeline, well before Canonicalize/Forge/Tagger legitimately
rewrite file bytes, so it was never actually valid post-Finalize for
this purpose (it's fine for what it was actually built for: Sentinel's
own retag-vs-audio-change detection). Corrected per Grey's own framing:
trust ALAC_Archive's *current* state as the baseline (it's the pristine,
permanent tier -- nothing modifies it after migration) and establish a
*fresh* hash from that state, rather than reconciling against an
already-mismatched value.

Why this checks ALAC_Archive specifically, not ALAC-Library: the archive
tier is the one place in this project that's supposed to stay byte-
identical forever once a file lands there. ALAC-Library gets
legitimately rewritten by Phase 2A LUFS baking, so "did this file
change" isn't a meaningful question to ask about it -- of course it did,
on purpose. If ALAC-Library content is ever suspected corrupted, the
correct move is re-baking from the archive origin, not restoring a
byte-for-byte backup.

Third design (2026-09-08): still keyed by path, but a path is not an
identity. A verify run that day found ALL 1,385 baselined paths gone from
disk and ALL 15,816 files present reported as "new" -- the check compared
nothing and said so only by printing a large number beside a green tick.
The cause is that organize, canonicalize, finalize and the LUFS bake all
move or rename files as a matter of course, and every move orphaned a
baseline row. Same shape as the "library files with no row: 0" incident: a
green result meaning "I looked at nothing".

Two changes, neither of which requires an archive row:

  - the baseline now also records `audio_hash`, the PCM identity, which
    survives a move AND a legitimate re-tag. A file not found at its
    baselined path is looked up by that instead, and reported as MOVED
    rather than as new. The decode this costs is paid only for files that
    actually moved.
  - a byte mismatch is no longer automatically rot. If the PCM identity is
    unchanged the file was re-tagged, which is benign and is counted
    separately. If it differs, that is rot. If no PCM identity was
    baselined, the change is reported as UNCLASSIFIABLE and still fails --
    fail towards the human, never assume benign.

And the check now refuses to look green when it verified almost nothing:
more than half the corpus unbaselined marks the run failed, because a
number printed beside a tick is not a warning anyone reads.

Deliberately NOT tied to archive.id / archive.file_path: ALAC_Archive is
itself deliberately not DB-row-tracked (build_alac_library.py's own
docstring explains why -- avoiding a second path column that could
drift out of sync with real filesystem state, since archive.file_path
already gets repointed at the baked ALAC-Library copy once a row is
baked). Tying bit-rot verification to that same row/path would inherit
the same fragility; confirmed the hard way while investigating this
tonight -- reconstructing "what was in ALAC_Archive for this row" from
current archive.file_path or even the LUFS_BAKE event log's old_value
was unreliable once other stages (dupe-resolver, etc.) had moved things
again since. A plain directory scan of ALAC_Archive, keyed by path in
its own dedicated table, sidesteps all of that.

Two modes:
  --rebaseline: scan ALAC_Archive, record each file's current SHA-256
    into archive_tier_hashes (INSERT OR REPLACE). Deliberate and
    explicit only -- never automatic, since silently re-baselining on
    every run would absorb real corruption into "the new normal"
    instead of ever catching it. Establishes ORPHEUS's --generate.
  (default): scan ALAC_Archive, compare each file's current SHA-256
    against its stored baseline. Mismatch = flagged corrupt, unless the
    PCM identity is unchanged, in which case it was re-tagged. A file
    with no baseline entry AT ITS PATH is looked up by PCM identity
    first and reported as moved if found; only a file that matches
    nothing is reported as new (needs a --rebaseline pass), and none of
    these are treated as corrupt. A baseline entry with no matching
    file on disk is reported as missing, separately. Establishes
    ORPHEUS's --verify (its --deep frame-level scan is dropped for the
    same reason the first design dropped it: CorruptStage's real
    ffprobe decode-test already covers that ground more reliably than
    hand-rolled frame-sync byte scanning).
"""

from __future__ import annotations

import logging
from pathlib import Path

from ..config import AUDIO_EXTENSIONS
from ..context import RunContext, StageResult, elision
from ..hasher import audio_hash, file_hash
from .base import BaseStage, NO_VERIFICATION

logger = logging.getLogger(__name__)

_COMMIT_EVERY = 25


def _scan_archive_files(alac_archive: Path) -> list[Path]:
    if not alac_archive.exists():
        return []
    return sorted(
        p for p in alac_archive.rglob("*") if p.is_file() and p.suffix.lower() in AUDIO_EXTENSIONS
    )


class BitRotStage(BaseStage):
    """
    Detect silent corruption in ALAC_Archive by comparing current
    SHA-256 hashes against a baseline in archive_tier_hashes. Standalone
    -- not part of DEFAULT_PIPELINE, and does not touch archive.* rows
    at all (directory-scan based, keyed by path).

    Use ctx.set("bitrot_rebaseline", True) to (re)establish the baseline
    from the archive's current state instead of verifying against it.
    Use ctx.set("bitrot_limit", N) to cap how many files a single run
    processes (full-archive hashing is I/O heavy).
    """

    NAME = "bitrot"

    def validate(self, ctx: RunContext) -> None:
        """No external dependency to check -- pure Python hashing via
        the same helper Sentinel itself uses to compute full_hash."""

    def _backfill_pcm(self, ctx: RunContext, dry_run: bool) -> StageResult:
        """Fill in the PCM identity for rows that were baselined without one.

        The migration step for the 2026-09-08 change. A baseline recorded by
        the earlier code has a byte hash and no `audio_hash`, which means
        verify cannot recognise those files after a move and cannot tell a
        re-tag from rot -- the two things the change exists to do. Re-running
        --rebaseline would work but would also recompute every SHA-256, and
        on this vault that is 592 GB of already-done work.

        Touches `audio_hash` only. The byte baseline is never rewritten, so
        this cannot absorb a real change into the new normal -- the hazard
        that makes --rebaseline deliberate and explicit.
        """
        result = self._make_result(dry_run=dry_run)
        rows = ctx.conn.execute(
            "SELECT path FROM archive_tier_hashes "
            "WHERE audio_hash IS NULL OR audio_hash = '' ORDER BY path"
        ).fetchall()

        limit = ctx.get("bitrot_limit", 0)
        if limit:
            rows = rows[:limit]

        result.notes.append(f"rows without a PCM identity: {len(rows)}")
        if not rows:
            result.notes.append("nothing to do — every baseline row has one")
            ctx.record_stage(result)
            return result
        if dry_run:
            result.files_processed = len(rows)
            result.notes.append("[DRY RUN] no decoding, no DB changes")
            ctx.record_stage(result)
            return result

        gone = 0
        for i, row in enumerate(rows, 1):
            result.files_processed += 1
            path = Path(row["path"])
            if not path.exists():
                # Its file moved or was removed. Nothing to decode, and the
                # row keeps its byte hash -- a later verify will match it by
                # path or report it missing, exactly as before.
                gone += 1
                continue
            try:
                ah = audio_hash(path)
            except Exception as exc:  # noqa: BLE001 -- hasher raises its own type
                result.files_errored += 1
                result.errors.append(f"{path.name}: {exc}")
                continue
            ctx.conn.execute(
                "UPDATE archive_tier_hashes SET audio_hash = ? WHERE path = ?",
                (ah, str(path)))
            result.files_changed += 1
            if i % _COMMIT_EVERY == 0:
                ctx.conn.commit()
                logger.info("bitrot: pcm backfill %d/%d", i, len(rows))

        ctx.conn.commit()
        result.notes.append(f"PCM identity recorded: {result.files_changed}")
        if gone:
            result.notes.append(f"skipped, file no longer at that path: {gone}")
        if result.files_errored:
            result.success = False
        ctx.record_stage(result)
        return result

    def _rebaseline(self, ctx: RunContext, dry_run: bool) -> StageResult:
        result = self._make_result(dry_run=dry_run)
        files = _scan_archive_files(ctx.config.alac_archive)

        limit = ctx.get("bitrot_limit", 0)
        if limit:
            files = files[:limit]

        result.notes.append(f"files to baseline: {len(files)}")
        unhashable: list[str] = []
        if not files:
            result.notes.append("nothing to do — ALAC_Archive is empty or missing")
            ctx.record_stage(result)
            return result

        if dry_run:
            result.files_processed = len(files)
            result.notes.append("[DRY RUN] no hashing, no baseline written")
            ctx.record_stage(result)
            return result

        for i, path in enumerate(files, 1):
            result.files_processed += 1
            try:
                h = file_hash(path)
            except OSError as exc:
                result.files_errored += 1
                result.errors.append(f"{path.name}: could not read file: {exc}")
                continue

            # The PCM identity, recorded alongside the byte hash. It is what
            # lets a later verify recognise this file after a move, and tell
            # a legitimate re-tag from rot. A failure to compute it is NOT
            # fatal -- the byte baseline is still worth having -- but it
            # costs this row its move-resistance, so it is counted.
            try:
                ah = audio_hash(path)
            except Exception as exc:  # noqa: BLE001 -- hasher raises its own type
                ah = None
                unhashable.append(f"{path.name}: {exc}")

            ctx.conn.execute(
                """
                INSERT INTO archive_tier_hashes
                       (path, sha256, audio_hash, size_bytes, baselined_at)
                VALUES (?, ?, ?, ?, datetime('now'))
                ON CONFLICT(path) DO UPDATE SET
                    sha256       = excluded.sha256,
                    audio_hash   = excluded.audio_hash,
                    size_bytes   = excluded.size_bytes,
                    baselined_at = excluded.baselined_at
                """,
                (str(path), h, ah, path.stat().st_size),
            )
            result.files_changed += 1

            if i % _COMMIT_EVERY == 0:
                ctx.conn.commit()
                logger.info("bitrot: baseline checkpoint %d/%d", i, len(files))

        ctx.conn.commit()
        result.notes.append(f"baselined: {result.files_changed}")
        if unhashable:
            result.notes.append(
                f"no PCM identity recorded for {len(unhashable)} file(s) — these "
                f"will read as new if they are ever moved"
            )
            for line in unhashable[:10]:
                result.notes.append(f"  {line}")
            if len(unhashable) > 10:
                result.notes.append(f"  {elision(len(unhashable) - 10)}")
        if result.files_errored:
            result.success = False

        ctx.record_stage(result)
        return result

    def _verify(self, ctx: RunContext, dry_run: bool) -> StageResult:
        result = self._make_result(dry_run=dry_run)
        alac_archive = ctx.config.alac_archive
        files = _scan_archive_files(alac_archive)

        limit = ctx.get("bitrot_limit", 0)
        if limit:
            files = files[:limit]

        result.notes.append(f"files to verify: {len(files)}")

        rows = ctx.conn.execute(
            "SELECT path, sha256, audio_hash FROM archive_tier_hashes").fetchall()
        baseline = {r["path"]: r["sha256"] for r in rows}
        baseline_audio = {r["path"]: r["audio_hash"] for r in rows}
        # The reverse index is what makes a moved file recognisable. PCM
        # identity survives a move; a path does not.
        by_audio = {r["audio_hash"]: r["path"] for r in rows if r["audio_hash"]}

        # A baselined file can be missing on disk even when the current
        # scan finds nothing at all (e.g. every baselined file was
        # removed) -- that's still worth reporting, not "nothing to do".
        if not files and not baseline:
            result.notes.append("nothing to do — ALAC_Archive is empty or missing")
            ctx.record_stage(result)
            return result

        if dry_run:
            result.files_processed = len(files)
            result.notes.append("[DRY RUN] no hashing, no DB changes")
            ctx.record_stage(result)
            return result

        ok_count = 0
        new_files = 0
        moved = 0
        retagged = 0
        unclassified = 0
        matched_paths: set[str] = set()
        corrupt: list[tuple[str, str, str]] = []

        for i, path in enumerate(files, 1):
            result.files_processed += 1
            path_str = str(path)
            key = path_str
            pcm: str | None = None

            if key not in baseline:
                # Not where it was baselined. Before calling it new -- which
                # is what silently emptied this check on 2026-09-08 -- ask
                # whether it is the same recording somewhere else. Costs a
                # decode, and only for files that actually moved.
                try:
                    pcm = audio_hash(path)
                except Exception:  # noqa: BLE001 -- undecodable is not rot; CorruptStage owns that
                    pcm = None
                origin = by_audio.get(pcm) if pcm else None
                if origin is None:
                    new_files += 1
                    result.files_skipped += 1
                    continue
                moved += 1
                key = origin

            matched_paths.add(key)

            try:
                current_hash = file_hash(path)
            except OSError as exc:
                result.files_errored += 1
                result.errors.append(f"{path.name}: could not read file: {exc}")
                continue

            if current_hash == baseline[key]:
                ok_count += 1
            else:
                # The bytes changed. That is a re-tag if the audio underneath
                # is identical, and rot if it is not. Without a recorded PCM
                # identity the two cannot be told apart, and an unclassified
                # change is reported loudly rather than assumed benign.
                stored_pcm = baseline_audio.get(key)
                if pcm is None:
                    try:
                        pcm = audio_hash(path)
                    except Exception:  # noqa: BLE001
                        pcm = None
                if stored_pcm and pcm and pcm == stored_pcm:
                    retagged += 1
                elif not stored_pcm:
                    unclassified += 1
                    corrupt.append((path_str, baseline[key], current_hash))
                    ctx.log_event(
                        "BITROT_DETECTED",
                        file_path=path_str,
                        old_value=baseline[key],
                        new_value=current_hash,
                        stage=self.NAME,
                        note="bytes changed and no PCM identity was baselined, "
                             "so a re-tag cannot be distinguished from rot",
                    )
                    logger.warning("[bitrot] UNCLASSIFIED CHANGE: %s", path.name)
                else:
                    corrupt.append((path_str, baseline[key], current_hash))
                    ctx.log_event(
                        "BITROT_DETECTED",
                        file_path=path_str,
                        old_value=baseline[key],
                        new_value=current_hash,
                        stage=self.NAME,
                        note="ALAC_Archive audio changed since baseline "
                             "(PCM identity differs, so this is not a re-tag)",
                    )
                    logger.warning("[bitrot] MISMATCH: %s", path.name)

            if i % _COMMIT_EVERY == 0:
                logger.info("bitrot: verify checkpoint %d/%d", i, len(files))

        # A baselined row is only missing if nothing on disk claimed it --
        # a moved file claims its origin row, so it must not count as gone.
        missing = [
            p for p in baseline
            if p not in matched_paths and not Path(p).exists()
        ]

        result.notes.append(f"ok: {ok_count}")
        result.notes.append(f"corrupt (audio changed since baseline): "
                            f"{len(corrupt) - unclassified}")
        if unclassified:
            result.notes.append(
                f"changed, unclassifiable: {unclassified}  (baselined before the "
                f"PCM identity was recorded — re-baseline to enable re-tag "
                f"detection, but read these first)")
        if moved:
            result.notes.append(
                f"moved since baseline (recognised by PCM identity): {moved}")
        if retagged:
            result.notes.append(
                f"re-tagged, audio identical: {retagged}  (benign; re-baseline "
                f"to stop reporting them)")
        result.notes.append(f"new (no baseline yet — run --rebaseline): {new_files}")
        result.notes.append(f"missing from disk (was baselined, gone now): {len(missing)}")
        # The number that decides whether a clean run means anything. A
        # verify whose corpus is mostly unbaselined compared almost nothing,
        # and used to say so only by printing a large "new" count next to a
        # green tick. Now it refuses to look green.
        if files and new_files > len(files) // 2:
            result.success = False
            result.notes.append(
                f"NOT A CLEAN RESULT: {new_files} of {len(files)} files have no "
                f"baseline, so this run verified almost nothing. Run "
                f"--rebaseline before trusting a pass.")
        if corrupt:
            result.success = False
            for fp, stored, current in corrupt[:20]:
                result.notes.append(
                    f"  MISMATCH {fp}  (baseline {stored[:12]}… now {current[:12]}…)"
                )
            if len(corrupt) > 20:
                result.notes.append(f"  {elision(len(corrupt) - 20)}")

        ctx.record_stage(result)
        return result

    def verify_effect(self, ctx: RunContext, result: StageResult) -> list[str]:
        """A file this stage baselined must have a hash recorded for it.

        BitRot's value is entirely in the baseline: without a stored
        sha256 there is nothing for a later run to compare against, so a
        write that silently reaches nothing does not fail today -- it
        fails silently for ever, by never detecting the rot it exists to
        detect. That is the worst shape of unverified stage.
        """
        tables = {r[0] for r in ctx.conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        if "archive_tier_hashes" not in tables:
            return NO_VERIFICATION
        rows = ctx.conn.execute(
            "SELECT file_path FROM events WHERE run_id = ? "
            " AND event_type = 'BITROT_DETECTED' ORDER BY id DESC LIMIT 10",
            (ctx.run_id,),
        ).fetchall()
        if not rows:
            return []
        unbaselined = [
            Path(r["file_path"]).name
            for r in rows
            if not ctx.conn.execute(
                "SELECT 1 FROM archive_tier_hashes WHERE path = ? LIMIT 1",
                (r["file_path"],)).fetchone()
        ]
        if not unbaselined:
            return []
        return [
            f"{len(unbaselined)} of {len(rows)} file(s) were checked but have no "
            f"stored hash to compare against later: {', '.join(unbaselined[:3])}"
        ]

    def _dispatch(self, ctx: RunContext, dry_run: bool) -> StageResult:
        if ctx.get("bitrot_backfill_pcm", False):
            return self._backfill_pcm(ctx, dry_run=dry_run)
        if ctx.get("bitrot_rebaseline", False):
            return self._rebaseline(ctx, dry_run=dry_run)
        return self._verify(ctx, dry_run=dry_run)

    def run(self, ctx: RunContext) -> StageResult:
        return self._dispatch(ctx, dry_run=False)

    def dry_run(self, ctx: RunContext) -> StageResult:
        return self._dispatch(ctx, dry_run=True)
