#!/usr/bin/env python3
"""
MUSAEUS — Typed run modes and a pure preview planner (P0-04).

The problem this replaces: `--dry-run` used to mean "run the pipeline with
a flag set", which is reduced execution, not preview. Every dry_run=True
call still ran cfg.ensure_dirs() (creating the real vault skeleton) and
RunContext.new()/record_stage() (creating the real SQLite DB and committing
RUN_START/STAGE_COMPLETE events) before any stage executed. P0-02 dealt
with that by refusing --dry-run outright, which was honest but left the
project with no preview at all.

A preview should not be an execution that promises to behave. It should be
a different KIND of thing: a plan computed from read-only inputs, that has
no way to mutate because it never constructs anything that can.

So this module:

  - never calls ensure_dirs(), never creates a RunContext, never opens a
    writable connection, never logs an event;
  - opens the database with mode=ro, and reports honestly when there is no
    database yet rather than creating one;
  - asks each stage CLASS what it would consider, via a classmethod, and
    never instantiates a stage. A stage object is the mutation-capable
    thing; not having one is what makes the guarantee structural rather
    than a matter of discipline.

A stage opts in by defining:

    @classmethod
    def plan_candidates(cls, conn, cfg) -> tuple[int, str]:
        '''Return (items this stage would act on, one-line description).'''

Both arguments are read-only inputs: `conn` is opened mode=ro, and `cfg`
is used for path lookups only. Ingest needs `cfg` because its real work is
"files waiting in INBOX" -- a filesystem question. The first version passed
only `conn`, so Ingest counted PENDING rows and reported 0 while 20 files
sat in the inbox. For the stage that matters most at the start of a run,
that is the worst possible number to get wrong.

Stages that do not define it are reported as "not previewable" rather than
as zero -- an unknown is not a zero, and reporting it as one is how a
preview starts lying.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any

from .config import MusicConfig

# Text every preview must carry. P0-05 requires the output state that no
# managed state or external lookup changed; keeping the sentence in one
# constant means it cannot drift between renderers.
SAFETY_STATEMENT = (
    "PREVIEW ONLY — nothing was changed. No files were written, moved or deleted, "
    "no database or event was created or modified, no directory was made, and no "
    "network lookup was performed."
)


class RunMode(Enum):
    """What authority a command has.

    PREVIEW has none, structurally: the preview path never builds a stage.
    """

    EXECUTE = "execute"
    PREVIEW = "preview"

    @classmethod
    def resolve(cls, *, preview: bool = False, dry_run: bool = False) -> RunMode:
        """Map CLI flags to a mode. `preview` and `--dry-run` are the same thing."""
        return cls.PREVIEW if (preview or dry_run) else cls.EXECUTE


#: Flags that ask for something durable. Combining them with preview is a
#: contradiction, and a contradiction should be refused rather than
#: silently resolved in one direction -- the user cannot tell which way it
#: went.
PERSISTENCE_FLAGS: tuple[str, ...] = (
    "force",
    "apply",
    "auto",
    "promote",
    "replace",
    "consolidate",
    "embed_from_db",
)


class PreviewConflict(ValueError):
    """Raised when preview is combined with a flag that asks to persist."""


def reject_persistence_flags(mode: RunMode, args: Any) -> None:
    """Refuse preview + a persistence flag. Fail closed, name the flag."""
    if mode is not RunMode.PREVIEW:
        return
    bad = [f for f in PERSISTENCE_FLAGS if getattr(args, f, False)]
    if bad:
        raise PreviewConflict(
            "preview cannot be combined with "
            + ", ".join(f"--{f.replace('_', '-')}" for f in bad)
            + ". Preview never changes anything; drop the flag to preview, "
            "or drop --dry-run/--preview to run for real."
        )


@dataclass(frozen=True)
class StagePlan:
    stage: str
    candidates: int | None  # None = this stage cannot be previewed
    description: str

    @property
    def previewable(self) -> bool:
        return self.candidates is not None


@dataclass
class Plan:
    """A deterministic, in-memory description of what a run would do."""

    mode: RunMode
    vault_root: Path
    stages: list[StagePlan] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    @property
    def total_candidates(self) -> int:
        return sum(s.candidates or 0 for s in self.stages)

    def render(self) -> str:
        lines = [f"  mode: {self.mode.value}", f"  vault: {self.vault_root}", ""]
        width = max((len(s.stage) for s in self.stages), default=10)
        for s in self.stages:
            count = "—" if s.candidates is None else f"{s.candidates:,}"
            lines.append(f"  {s.stage:<{width}}  {count:>8}  {s.description}")
        lines.append("")
        for n in self.notes:
            lines.append(f"  ! {n}")
        if self.notes:
            lines.append("")
        lines.append(f"  {SAFETY_STATEMENT}")
        return "\n".join(lines)

    def to_json(self) -> str:
        return json.dumps(
            {
                "mode": self.mode.value,
                "vault_root": str(self.vault_root),
                "safety": SAFETY_STATEMENT,
                "notes": self.notes,
                "stages": [
                    {"stage": s.stage, "candidates": s.candidates, "description": s.description}
                    for s in self.stages
                ],
            },
            indent=2,
            # ensure_ascii=False so the safety statement appears verbatim
            # rather than as \u2014 escapes. A machine consumer checking for
            # that sentence should not have to know how it was encoded.
            ensure_ascii=False,
        )


def build_plan(cfg: MusicConfig, pipeline: list[type], mode: RunMode = RunMode.PREVIEW) -> Plan:
    """Compute what `pipeline` would do, touching nothing.

    Deliberately takes stage CLASSES and never instantiates them. Reading a
    classmethod off a type cannot run a stage; constructing one is the step
    that would make mutation possible, so the guarantee is structural.
    """
    plan = Plan(mode=mode, vault_root=cfg.vault_root)

    if not Path(cfg.db_path).exists():
        # An absent database is a fact to report, not a reason to create one.
        plan.notes.append(
            f"no database at {cfg.db_path} — nothing has been ingested yet, "
            "so every count below is zero by definition"
        )
        for st in pipeline:
            plan.stages.append(StagePlan(_name(st), 0, "no database yet"))
        return plan

    conn = sqlite3.connect(f"file:{cfg.db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        for st in pipeline:
            fn = getattr(st, "plan_candidates", None)
            if fn is None:
                plan.stages.append(
                    StagePlan(_name(st), None, "no preview available for this stage")
                )
                continue
            try:
                count, desc = fn(conn, cfg)
                plan.stages.append(StagePlan(_name(st), int(count), desc))
            except Exception as exc:  # a broken planner must not fake a zero
                plan.stages.append(StagePlan(_name(st), None, f"preview failed: {exc}"))
    finally:
        conn.close()

    unpreviewable = [s.stage for s in plan.stages if not s.previewable]
    if unpreviewable:
        plan.notes.append(
            f"{len(unpreviewable)} stage(s) cannot be previewed and are shown as '—', "
            "not as zero: " + ", ".join(unpreviewable[:6]) + ("…" if len(unpreviewable) > 6 else "")
        )
    return plan


def _name(stage: type) -> str:
    return str(getattr(stage, "NAME", stage.__name__))
