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

    def summarise(self) -> str:
        mode = " [DRY RUN]" if self.dry_run else ""
        status = "OK" if self.success else "FAILED"
        return (
            f"{self.stage_name}{mode}: {status} | "
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
    ) -> "RunContext":
        """Create a fresh RunContext and log the RUN_START event."""
        rid = run_id or f"run_{datetime.now(tz=timezone.utc).strftime('%Y%m%dT%H%M%SZ')}_{uuid.uuid4().hex[:6]}"
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

    def finish(self) -> None:
        """Log RUN_END, commit, and close the DB connection."""
        success = all(r.success for r in self.stage_results)
        self.log_event(
            "RUN_END",
            note=f"success={success} stages={len(self.stage_results)}",
        )
        self.conn.commit()
        self.conn.close()

    def __repr__(self) -> str:
        return (
            f"RunContext(run_id={self.run_id!r}, "
            f"dry_run={self.dry_run}, "
            f"stages_run={len(self.stage_results)})"
        )
