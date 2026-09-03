#!/usr/bin/env python3
"""
MUSAEUS — one self-contained document per run, for a session with no tools.

Why this exists
----------------
Grey's Opus 5 access ends 2026-09-08. After that, whoever looks at a
failed or suspicious MUSAEUS run may be using claude.ai's free tier --
no CLI, no file access, no ability to grep the codebase or read a
traceback off disk. Handed only a copied-and-pasted error, that session
has to ask "what file, what line, what does the surrounding code do" and
get no answer.

So every run that has anything worth a second look writes ONE markdown
file with everything already assembled: which stage, what specifically
went wrong, on what file, and (for a hard crash) the traceback inline --
not a pointer to a JSON report that a tool-less session cannot open.

Deliberately NOT a new tracking mechanism
-------------------------------------------
This reads what already exists rather than adding a second system that
could drift from the first -- see CLAUDE.md's whole reason for existing.
Two sources, both already true after every run:

  - ctx.stage_results: each stage's own StageResult, in particular
    `verified` (the 2026-08-22 honesty fix: None means no claim, True
    means checked and held, False means checked and DID NOT hold -- a
    real problem a stage's own verify_effect caught) and `success`/
    `errors` (a stage that finished but reported an internal failure,
    no exception involved).
  - {runs_root}/FAILURES/*.json: base.py's own crash reports, written
    when a stage raises. Already structured (stage, phase, exception,
    traceback, last item); this reads them back in rather than
    re-deriving anything.

A run with nothing wrong writes nothing -- an empty "all clear" file is
one more thing to notice is empty, and the absence of a HANDOFFS entry
already says that.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .context import RunContext, StageResult

logger = logging.getLogger(__name__)


def _stage_issues(stage_results: list[StageResult]) -> list[dict[str, Any]]:
    """Everything from this run's StageResults worth a second look.

    Two DIFFERENT problems, kept distinct in the output rather than
    merged, because they mean different things to whoever reads this:
    a stage that crashed or reported its own internal failure
    (success=False) is one thing; a stage that ran fine but whose OWN
    after-the-fact check found the claimed effect did not actually
    happen (verified=False) is the more insidious one -- see
    base.py's verify_effect docstring for why that distinction exists
    at all.
    """
    issues: list[dict[str, Any]] = []
    for r in stage_results:
        if not r.success:
            issues.append(
                {
                    "kind": "stage_reported_failure",
                    "stage": r.stage_name,
                    "files_errored": r.files_errored,
                    "errors": list(r.errors),
                    "notes": list(r.notes),
                }
            )
        if r.verified is False:
            issues.append(
                {
                    "kind": "verification_failed",
                    "stage": r.stage_name,
                    "verify_notes": list(r.verify_notes),
                }
            )
    return issues


def _crash_reports(runs_root: Path, run_id: str) -> list[dict[str, Any]]:
    """FAILURES/*.json reports written by THIS run, read back in full --
    not just their paths, since a tool-less session cannot open them."""
    d = runs_root / "FAILURES"
    if not d.exists():
        return []
    reports = []
    for p in sorted(d.glob(f"*_{run_id}_*.json")):
        try:
            reports.append(json.loads(p.read_text(encoding="utf-8")))
        except (OSError, json.JSONDecodeError) as exc:
            logger.warning("[handoff] could not read failure report %s: %s", p, exc)
    return reports


def _render(run_id: str, issues: list[dict[str, Any]], crashes: list[dict[str, Any]]) -> str:
    now = datetime.now(tz=timezone.utc).isoformat(timespec="seconds")
    lines = [
        f"# MUSAEUS ForClaudeHandoff — {run_id}",
        "",
        f"Generated {now}. This file exists because something in this run",
        "needs a human decision or a code fix -- see the sections below.",
        "",
        "**If you are a fresh Claude session with no file or tool access:**",
        "everything needed to reason about each issue is inlined below --",
        "the stage name, what it does, the exact error or mismatch, and",
        "(for a crash) the full traceback. You cannot verify anything",
        "against the live codebase from here; say so plainly rather than",
        "guessing at a fix with unstated confidence. MUSAEUS is at",
        "github.com/BogMan64/musaeus if the repository itself is reachable.",
        "",
        "---",
        "",
    ]

    if crashes:
        lines.append(f"## Stage crashes ({len(crashes)})")
        lines.append("")
        for c in crashes:
            lines.append(f"### {c.get('stage', '?')} — {c.get('exception_type', '?')}")
            lines.append("")
            lines.append(f"- Phase: `{c.get('phase', '?')}`")
            lines.append(f"- Run: `{c.get('run_id', '?')}`  Occurred: {c.get('occurred_at', '?')}")
            if c.get("last_item"):
                lines.append(f"- Was working on: `{c['last_item']}`")
            lines.append(f"- Message: {c.get('exception_message', '(none)')}")
            lines.append("")
            lines.append("```")
            lines.append((c.get("traceback") or "(no traceback captured)").rstrip())
            lines.append("```")
            lines.append("")

    verify_failures = [i for i in issues if i["kind"] == "verification_failed"]
    if verify_failures:
        lines.append(f"## Verification failures ({len(verify_failures)})")
        lines.append("")
        lines.append(
            "A stage's OWN after-the-fact check found its claimed effect did"
        )
        lines.append(
            "not actually happen -- this is not a crash, the stage completed"
        )
        lines.append(
            "and reported success, but a second, independent check caught a"
        )
        lines.append("mismatch. Treat these as the higher-priority half of this file.")
        lines.append("")
        for i in verify_failures:
            lines.append(f"### {i['stage']}")
            lines.append("")
            for note in i["verify_notes"]:
                lines.append(f"- {note}")
            lines.append("")

    stage_failures = [i for i in issues if i["kind"] == "stage_reported_failure"]
    if stage_failures:
        lines.append(f"## Stages that reported failure ({len(stage_failures)})")
        lines.append("")
        for i in stage_failures:
            lines.append(f"### {i['stage']}  ({i['files_errored']} file(s) errored)")
            lines.append("")
            for err in i["errors"]:
                lines.append(f"- ERROR: {err}")
            for note in i["notes"]:
                lines.append(f"- {note}")
            lines.append("")

    return "\n".join(lines).rstrip() + "\n"


def write_handoff_doc(ctx: RunContext) -> Path | None:
    """Write RUNS/HANDOFFS/ForClaudeHandoff_<run_id>.md if this run has anything
    worth a second look; return its path, or None (writing nothing) for
    a clean run.

    Called once, at the end of the pipeline, after every stage has had
    the chance to run and record its result -- see cli.py's
    _run_pipeline.
    """
    issues = _stage_issues(ctx.stage_results)
    crashes = _crash_reports(ctx.runs_root, ctx.run_id)
    if not issues and not crashes:
        return None

    out_dir = ctx.runs_root / "HANDOFFS"
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"ForClaudeHandoff_{ctx.run_id}.md"
    path.write_text(_render(ctx.run_id, issues, crashes), encoding="utf-8")
    return path
