#!/usr/bin/env python3
"""
MUSAEUS — RunContext
Single shared-state object passed through every pipeline stage.

One RunContext per pipeline invocation.
One DB connection, one run_id, one config — no global state.
"""

from __future__ import annotations

import sqlite3
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .config import MusicConfig
from .db import log_event


def _utc_now() -> str:
    return datetime.now(tz=timezone.utc).isoformat(timespec="seconds")


MAX_LISTED = 20


def head_with_remainder(items: list[str], limit: int = MAX_LISTED) -> tuple[list[str], int]:
    """The first `limit` items, and how many were left out.

    Beside StageResult because notes/errors/verify_notes are its fields
    and every consumer needs the same answer. A stage's error list is one
    entry per file -- scholar.py appends "Missing: <path>" for every row
    whose file has gone -- so its length is bounded by the batch, not by
    anything a reader can use. Clearing one 3,142-file directory out of
    INBOX on 2026-09-03 put 3,133 near-identical lines on the console and
    would have put the same into the ForClaudeHandoff doc, whose whole
    purpose is to be pasted into a session with no file access.

    Returns the split rather than rendered lines because the callers
    format differently -- handoff.py wants markdown bullets, cli.py wants
    indented console lines across two streams. The `... and N more`
    wording is this repo's existing idiom (ingest.py, scholar.py,
    sentinel.py, tribute_quarantine.py, console.py); this is the one
    place the arithmetic behind it lives.
    """
    return items[:limit], max(0, len(items) - limit)


@dataclass
class StageResult:
    """What a stage returns after execution."""

    stage_name: str
    success: bool
    files_processed: int = 0
    files_changed: int = 0
    files_skipped: int = 0
    files_errored: int = 0
    dry_run: bool = False
    notes: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    # Effect verification (2026-08-22). None = the stage does not claim a
    # verifiable effect, or nothing changed. True/False = its claim was
    # checked against the filesystem or DB after the fact and held / did not.
    #
    # This exists because five separate components were found reporting
    # success while doing nothing at all: rebuild-db dispatching on event
    # names that did not exist, Forge writing to an unserialisable tag key,
    # GenreCanon parsing a separator the file never used, PermissionsStage
    # sweeping an empty directory, and an overnight helper silently dropping
    # its arguments. Every one passed its tests, because the tests asserted
    # the shape of the call rather than its effect on disk.
    verified: bool | None = None
    verify_notes: list[str] = field(default_factory=list)

    def summarise(self) -> str:
        mode = " [DRY RUN]" if self.dry_run else ""
        status = "OK" if self.success else "FAILED"
        seal = ""
        if self.verified is True:
            seal = " ✓verified"
        elif self.verified is False:
            seal = " ✗UNVERIFIED"
        return (
            f"{self.stage_name}{mode}: {status}{seal} | "
            f"processed={self.files_processed} "
            f"changed={self.files_changed} "
            f"skipped={self.files_skipped} "
            f"errors={self.files_errored}"
        )


@dataclass
class RunContext:
    """
    Shared state for a single Musaeus pipeline run.

    Lifecycle:
        ctx = RunContext.new(config, conn, dry_run=False)
        # pass ctx to every stage
        ctx.finish()

    Never create two RunContexts in the same process — use one and pass it around.
    """

    run_id: str
    config: MusicConfig
    conn: sqlite3.Connection
    dry_run: bool
    started_at: str
    stage_results: list[StageResult] = field(default_factory=list)
    _extra: dict[str, Any] = field(default_factory=dict, repr=False)

    # ── Factory ───────────────────────────────────────────────────────────────

    @classmethod
    def new(
        cls,
        config: MusicConfig,
        conn: sqlite3.Connection,
        dry_run: bool = False,
        run_id: str | None = None,
    ) -> RunContext:
        """Create a fresh RunContext and log the RUN_START event."""
        rid = (
            run_id
            or f"run_{datetime.now(tz=timezone.utc).strftime('%Y%m%dT%H%M%SZ')}_{uuid.uuid4().hex[:6]}"
        )
        started = _utc_now()
        ctx = cls(
            run_id=rid,
            config=config,
            conn=conn,
            dry_run=dry_run,
            started_at=started,
        )
        ctx.log_event(
            "RUN_START",
            note=f"dry_run={dry_run}",
        )
        conn.commit()
        return ctx

    # ── Event log passthrough ─────────────────────────────────────────────────

    def log_event(
        self,
        event_type: str,
        file_path: str | None = None,
        old_value: str | None = None,
        new_value: str | None = None,
        stage: str | None = None,
        note: str | None = None,
    ) -> None:
        """Append an event to the immutable log."""
        log_event(
            self.conn,
            run_id=self.run_id,
            event_type=event_type,
            file_path=file_path,
            old_value=old_value,
            new_value=new_value,
            stage=stage,
            note=note,
        )

    # ── Stage result tracking ─────────────────────────────────────────────────

    def record_stage(self, result: StageResult) -> None:
        """Store a stage result and commit the DB."""
        self.stage_results.append(result)
        self.log_event(
            "STAGE_COMPLETE",
            stage=result.stage_name,
            note=result.summarise(),
        )
        self.conn.commit()

    # ── Convenience accessors ─────────────────────────────────────────────────

    @property
    def vault_root(self) -> Path:
        return self.config.vault_root

    @property
    def inbox(self) -> Path:
        return self.config.inbox

    @property
    def staging(self) -> Path:
        return self.config.staging

    @property
    def quarantine(self) -> Path:
        return self.config.quarantine

    @property
    def runs_root(self) -> Path:
        return self.config.runs_root

    @property
    def alac_library(self) -> Path:
        return self.config.alac_library

    # ── Stash: stages can store cross-stage data here ─────────────────────────

    def set(self, key: str, value: Any) -> None:
        """Store a value in the per-run stash."""
        self._extra[key] = value

    def get(self, key: str, default: Any = None) -> Any:
        """Retrieve a value from the per-run stash."""
        return self._extra.get(key, default)

    # ── Run directory ─────────────────────────────────────────────────────────

    @property
    def run_dir(self) -> Path:
        """Dedicated directory for this run's logs/reports."""
        return self.config.runs_root / self.run_id

    def ensure_run_dir(self) -> Path:
        """Create and return the run directory."""
        d = self.run_dir
        d.mkdir(parents=True, exist_ok=True)
        return d

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    def finish(self, interrupted: bool = False) -> None:
        """Log RUN_END, commit, and close the DB connection.

        `interrupted` records that the run was cut short rather than
        reaching the end of the pipeline. Without it a Ctrl-C left no
        RUN_END at all, and an aborted run was indistinguishable from one
        still in progress -- the resume markers make relaunching safe, but
        nothing said which stages never ran. Measured 2026-08-26: a batch
        stopped after 22 of 27 stages, and only reading the stage list
        revealed that forge, tagger, audit, enrich and mb_enrich had been
        skipped.
        """
        success = all(r.success for r in self.stage_results) and not interrupted
        self.log_event(
            "RUN_END",
            note=(
                f"success={success} stages={len(self.stage_results)}"
                + (" interrupted=True" if interrupted else "")
            ),
        )
        self.conn.commit()
        self.conn.close()

    def __repr__(self) -> str:
        return (
            f"RunContext(run_id={self.run_id!r}, "
            f"dry_run={self.dry_run}, "
            f"stages_run={len(self.stage_results)})"
        )
