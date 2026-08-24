#!/usr/bin/env python3
"""
MUSAEUS — Deny List Stage

Refuses re-ingest of audio that was deliberately removed.

Why this exists
---------------
Removing a file does not stop it coming back. Established 2026-08-24 by
tracing every consumer of the finalized-hash ledger: a ledger hit is only
acted on when a **live file** backs it, because `CrossDupeStage` verifies
the path exists before believing it — which is the fix for the section
4.17 cascade and must stay. So a purged knock-off dropped back into the
INBOX is ingested exactly as if it had never been seen.

That surprised the owner, and reasonably: 96 ledger entries name removed
content, and keeping them reads like protection. It isn't. This stage is
the mechanism that actually was missing.

What it matches
---------------
`audio_hash` — the PCM hash — so a match survives re-tagging and container
rewriting, the same property that makes it the stable identity everywhere
else in the project.

**It does not catch a different rip or a different master of the same
song.** Those are different audio and hash differently. This is a "do not
re-add THIS recording" list, not a "never acquire this song" list. Saying
so plainly matters: the failure mode of a deny-list is someone trusting it
for a guarantee it never offered, then concluding it is broken when a
different rip of the same track arrives.

What it does on a match
-----------------------
Quarantines and reports. It never deletes: a deny-list acting on a false
positive would destroy the only copy of something the owner had chosen to
re-add on purpose, and there is no way to tell those two cases apart from
inside the pipeline. Quarantine is reversible; deletion is not.
"""

from __future__ import annotations

import logging
import shutil
from pathlib import Path

from ..context import RunContext, StageResult
from ..db import ensure_deny_list, lookup_denied_hash, open_hash_index
from .base import BaseStage

logger = logging.getLogger(__name__)

_COMMIT_EVERY = 25


class DenyListStage(BaseStage):
    """Quarantine freshly hashed files whose audio was deliberately removed."""

    NAME = "deny-list"

    def validate(self, ctx: RunContext) -> None:
        if not ctx.config.hash_index_path.exists():
            logger.info("[deny-list] no hash index yet — nothing to check against")
            return
        conn = open_hash_index(ctx.config.hash_index_path)
        try:
            ensure_deny_list(conn)
            n = conn.execute("SELECT COUNT(*) FROM denied_hashes").fetchone()[0]
        finally:
            conn.close()
        logger.info("[deny-list] %d denied recording(s) on the list", n)

    def _candidates(self, ctx: RunContext) -> list[dict]:
        # Anything hashed but not yet catalogued. Sentinel sets HASHED; a row
        # that already reached CATALOGUED is the owner's library and is not
        # this stage's business -- retroactively quarantining held music
        # because of a list entry would be a far worse failure than a
        # re-ingest slipping through.
        rows = ctx.conn.execute(
            "SELECT file_path, audio_hash, artist, title FROM archive "
            "WHERE status = 'HASHED' AND audio_hash IS NOT NULL AND trim(audio_hash) != ''"
        ).fetchall()
        return [dict(r) for r in rows]

    def _process(self, ctx: RunContext, dry_run: bool) -> StageResult:
        result = self._make_result(dry_run=dry_run)

        if not ctx.config.hash_index_path.exists():
            result.notes.append("no hash index — nothing to check against")
            ctx.record_stage(result)
            return result

        rows = self._candidates(ctx)
        result.notes.append(f"newly hashed files to check: {len(rows)}")
        if not rows:
            ctx.record_stage(result)
            return result

        conn = open_hash_index(ctx.config.hash_index_path)
        ensure_deny_list(conn)
        quarantine_dir = ctx.config.quarantine / "denied"

        blocked = 0
        try:
            for i, row in enumerate(rows, 1):
                result.files_processed += 1
                entry = lookup_denied_hash(conn, row["audio_hash"])
                if entry is None:
                    continue

                src = Path(row["file_path"])
                label = f"{row.get('artist') or '?'} — {row.get('title') or src.name}"
                logger.info(
                    "[deny-list] refusing %s (removed previously: %s)", label, entry["reason"]
                )
                blocked += 1
                result.files_changed += 1

                if dry_run:
                    result.notes.append(f"  [DRY] would quarantine {label}")
                    continue

                target = quarantine_dir / src.name
                try:
                    quarantine_dir.mkdir(parents=True, exist_ok=True)
                    if src.exists():
                        shutil.move(str(src), str(target))
                except OSError as exc:
                    result.files_errored += 1
                    result.errors.append(f"{src.name}: {exc}")
                    continue

                ctx.conn.execute(
                    "UPDATE archive SET status='QUARANTINED', file_path=? WHERE file_path=?",
                    (str(target), str(src)),
                )
                ctx.log_event(
                    "DENIED_REINGEST",
                    file_path=str(target),
                    old_value=str(src),
                    new_value=entry["reason"],
                    stage=self.NAME,
                    note=f"previously removed as {entry['source_path'] or 'unknown'}",
                )

                if i % _COMMIT_EVERY == 0:
                    ctx.conn.commit()
        finally:
            conn.close()

        if not dry_run:
            ctx.conn.commit()

        verb = "would refuse" if dry_run else "refused"
        result.notes.append(f"{verb}: {blocked}")
        if blocked:
            result.notes.append("quarantined, not deleted — reversible if any was wanted")
        ctx.record_stage(result)
        return result

    def verify_effect(self, ctx: RunContext, result: StageResult) -> list[str]:
        """No catalogued track may carry a denied hash.

        Asserted against the library rather than by re-reading what this
        stage wrote, so a stage that quarantined nothing cannot satisfy it.
        """
        problems: list[str] = []
        if not ctx.config.hash_index_path.exists():
            return problems

        conn = open_hash_index(ctx.config.hash_index_path)
        try:
            ensure_deny_list(conn)
            denied = {r[0] for r in conn.execute("SELECT audio_hash FROM denied_hashes")}
        finally:
            conn.close()
        if not denied:
            return problems

        live = ctx.conn.execute(
            "SELECT file_path, audio_hash FROM archive WHERE status='CATALOGUED' "
            "AND audio_hash IS NOT NULL"
        ).fetchall()
        leaked = [r["file_path"] for r in live if r["audio_hash"] in denied]
        if leaked:
            problems.append(
                f"{len(leaked)} catalogued track(s) carry a denied audio hash: "
                + ", ".join(Path(p).name for p in leaked[:3])
            )
        return problems

    def dry_run(self, ctx: RunContext) -> StageResult:
        return self._process(ctx, dry_run=True)

    def run(self, ctx: RunContext) -> StageResult:
        return self._process(ctx, dry_run=False)
