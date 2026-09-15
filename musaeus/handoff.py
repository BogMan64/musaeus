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

from .context import elision, head_with_remainder

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


def _capped(items: list[str], prefix: str = "") -> list[str]:
    """Render a bounded number of entries, then say how many are left.

    Unbounded here is not a cosmetic problem. A stage's error list is one
    entry per file -- scholar.py appends "Missing: <path>" for every row
    whose file has gone -- so one bad batch can put thousands of
    near-identical lines into a document whose whole purpose is to be
    PASTED into a session that has no file access. Past twenty the reader
    has learned everything the list can teach and the paste stops being
    possible; the count carries the scale, and the section header already
    carries files_errored.

    `... and N more` is the idiom this repo already uses for the same job
    in ingest.py, scholar.py, sentinel.py, tribute_quarantine.py and
    console.py -- matched here rather than invented again.
    """
    head, tail, hidden = head_with_remainder(items)
    lines = [f"- {prefix}{item}" for item in head]
    if hidden:
        # Between, never after: a trailing elision would read as though the
        # tail entries were the ones that followed the head.
        lines.append(f"- {elision(hidden)}")
    lines.extend(f"- {prefix}{item}" for item in tail)
    return lines


def _what_happened(stage_results: list[StageResult]) -> list[str]:
    """What the run DID, stage by stage -- not only what went wrong.

    Added 2026-09-14 on Grey's instruction. The original wrote nothing at
    all for a clean run, on the reasoning that an empty all-clear file is
    one more thing to notice is empty. True for a file whose only job is
    to carry problems -- but this file's job changed: it is now the thing
    he pastes into a tool-less session to ask "what happened last night?",
    and a successful run is exactly the case where he has no other way to
    see inside it.

    A run that changed 2,577 rows and a run that changed none both look
    identical from outside. That is the gap this closes.
    """
    lines = ["## What this run did", ""]
    if not stage_results:
        lines += ["No stages ran.", ""]
        return lines

    lines += ["| stage | processed | changed | errored | verified |",
              "|---|---:|---:|---:|---|"]
    for r in stage_results:
        verdict = {True: "yes", False: "**NO**", None: "-- (no claim)"}[r.verified]
        lines.append(
            f"| {r.stage_name} | {r.files_processed:,} | {r.files_changed:,} "
            f"| {r.files_errored:,} | {verdict} |"
        )
    lines.append("")

    total_changed = sum(r.files_changed for r in stage_results)
    lines += [f"**{total_changed:,} file(s) changed across {len(stage_results)} stage(s).**", ""]

    # Stage notes are where a stage says what it actually decided -- the
    # counts alone do not carry "881 genres filled" or "52 folders merged".
    noted = [r for r in stage_results if r.notes]
    if noted:
        lines += ["### What each stage reported", ""]
        for r in noted:
            lines.append(f"**{r.stage_name}**")
            lines.extend(_capped(list(r.notes)))
            lines.append("")
    return lines


def _render(run_id: str, issues: list[dict[str, Any]], crashes: list[dict[str, Any]],
            stage_results: list[StageResult] | None = None) -> str:
    now = datetime.now(tz=timezone.utc).isoformat(timespec="seconds")
    lines = [
        f"# MUSAEUS ForClaudeHandoff — {run_id}",
        "",
        f"Generated {now}. A complete account of this run: what it did, and",
        "anything that needs a human decision or a code fix.",
        "",
        "**Paste this whole file** into any AI session to ask what happened.",
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

    if not crashes and not issues:
        lines += ["## Nothing went wrong", "",
                  "No stage crashed, no stage reported failure, and every check",
                  "that made a claim held. The run summary below is the whole story.",
                  "", "---", ""]

    if stage_results is not None:
        lines.extend(_what_happened(stage_results))
        lines += ["---", ""]

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
        lines.append("A stage's OWN after-the-fact check found its claimed effect did")
        lines.append("not actually happen -- this is not a crash, the stage completed")
        lines.append("and reported success, but a second, independent check caught a")
        lines.append("mismatch. Treat these as the higher-priority half of this file.")
        lines.append("")
        for i in verify_failures:
            lines.append(f"### {i['stage']}")
            lines.append("")
            lines.extend(_capped(i["verify_notes"]))
            lines.append("")

    stage_failures = [i for i in issues if i["kind"] == "stage_reported_failure"]
    if stage_failures:
        lines.append(f"## Stages that reported failure ({len(stage_failures)})")
        lines.append("")
        for i in stage_failures:
            lines.append(f"### {i['stage']}  ({i['files_errored']} file(s) errored)")
            lines.append("")
            lines.extend(_capped(i["errors"], "ERROR: "))
            lines.extend(_capped(i["notes"]))
            lines.append("")

    return "\n".join(lines).rstrip() + "\n"


def write_handoff_doc(ctx: RunContext) -> Path | None:
    """Write RUNS/HANDOFFS/ForClaudeHandoff_<run_id>.md and return its path.

    **Always writes, as of 2026-09-14.** It used to return None for a clean
    run. Grey asked for the opposite: the file is what he pastes into a
    tool-less session to ask what happened overnight, and a successful run
    is precisely when he has no other window into it.

    Called once, at the end of the pipeline, after every stage has had
    the chance to run and record its result -- see cli.py's
    _run_pipeline.
    """
    issues = _stage_issues(ctx.stage_results)
    crashes = _crash_reports(ctx.runs_root, ctx.run_id)

    out_dir = ctx.runs_root / "HANDOFFS"
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"ForClaudeHandoff_{ctx.run_id}.md"
    path.write_text(
        _render(ctx.run_id, issues, crashes, ctx.stage_results), encoding="utf-8"
    )
    return path


# ── Standalone tools ─────────────────────────────────────────────────────────
#
# The pipeline gets its handoff from write_handoff_doc above, which reads
# ctx.stage_results. The long unattended jobs -- the LUFS bake, the CAR and
# iPhone builds, the album-name proposal -- are NOT pipeline stages and have
# no RunContext, so they produced nothing at all.
#
# They are also, in practice, the runs Grey most needs a morning summary of:
# they run for hours while he is asleep, and a terminal that has scrolled or
# a session that has ended takes the only account of them with it.


def write_tool_handoff(
    runs_root: Path,
    tool: str,
    *,
    summary: dict[str, Any],
    notes: list[str] | None = None,
    problems: list[str] | None = None,
    log_path: Path | None = None,
) -> Path | None:
    """Write a paste-able account of a standalone tool run.

    Same destination and shape as the pipeline's handoff, so there is one
    place to look and one format to read, whichever produced it.

    `summary` is the headline numbers -- whatever the tool counts. `notes`
    is what it decided. `problems` is what needs a human. All three are
    written even when `problems` is empty, because "it ran and here is what
    it did" is the common case and the one with no other record.

    Never raises: a tool that finished its real work must not be reported
    as failed because the report about it could not be written.
    """
    try:
        now = datetime.now(tz=timezone.utc).isoformat(timespec="seconds")
        stamp = datetime.now(tz=timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        out_dir = Path(runs_root) / "HANDOFFS"
        out_dir.mkdir(parents=True, exist_ok=True)
        path = out_dir / f"ForClaudeHandoff_{tool}_{stamp}.md"

        lines = [
            f"# MUSAEUS ForClaudeHandoff — {tool}",
            "",
            f"Generated {now}. A standalone tool run, not a pipeline run.",
            "",
            "**Paste this whole file** into any AI session to ask what happened.",
            "",
            "**If you are a session with no file or tool access:** everything",
            "needed is inlined below. You cannot verify any of it against the",
            "live system from here -- say so plainly rather than guessing with",
            "unstated confidence. MUSAEUS is at github.com/BogMan64/musaeus.",
            "",
            "---",
            "",
            "## What this run did",
            "",
            "| | |",
            "|---|---:|",
        ]
        for k, v in summary.items():
            shown = f"{v:,}" if isinstance(v, int) else str(v)
            lines.append(f"| {k} | {shown} |")
        lines.append("")

        if notes:
            lines += ["### What it reported", ""]
            lines.extend(_capped(list(notes)))
            lines.append("")

        if problems:
            lines += ["---", "", f"## Needs attention ({len(problems)})", ""]
            lines.extend(_capped(list(problems)))
            lines.append("")
        else:
            lines += ["---", "", "## Nothing went wrong", "",
                      "The tool reported no problems. The summary above is the whole story.",
                      ""]

        if log_path:
            lines += [f"Full log: `{log_path}`", ""]

        path.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")
        return path
    except Exception as exc:  # noqa: BLE001
        logger.warning("[handoff] could not write tool handoff for %s: %s", tool, exc)
        return None


# ── Per-Act reports ──────────────────────────────────────────────────────────


def act_of(stage_name: str) -> str | None:
    """Which Act a stage belongs to, by its class name.

    Derived from the stage tuples in musaeus.stages rather than a second
    hand-kept list -- a stage moved between Acts must not need remembering
    here as well. Imported lazily because musaeus.stages imports a great deal
    and handoff.py is also used by standalone tools that need none of it.
    """
    from . import stages as _s

    for label, group in (
        ("act1", _s.ACT1_INTAKE_CORRECTION),
        ("act2", _s.ACT2_DEDUP_STAGING),
        ("act3", _s.ACT3_CANONICALIZE_FINALIZE),
        ("enrichment", _s.ENRICHMENT),
    ):
        if any(cls.__name__ == stage_name for cls in group):
            return label
    return None


def write_act_handoff(ctx: RunContext, act: str) -> Path | None:
    """Write the report for one Act, as soon as that Act finishes.

    Grey asked for this on 2026-09-14: the run-level handoff is written at
    the very end, so a run that dies in Act 2 hands him nothing at all --
    including nothing about the Act 1 that completed perfectly well before
    it. A partial run is exactly when an account of what DID happen is worth
    most.

    Reads only ctx.stage_results, filtered to this Act. No new bookkeeping:
    the same rule the rest of this module follows, because a second source of
    truth is a second thing that can drift.
    """
    try:
        mine = [r for r in ctx.stage_results if act_of(r.stage_name) == act]
        if not mine:
            return None
        issues = _stage_issues(mine)
        crashes = [
            c for c in _crash_reports(ctx.runs_root, ctx.run_id)
            if act_of(c.get("stage", "")) == act
        ]
        out_dir = ctx.runs_root / "HANDOFFS"
        out_dir.mkdir(parents=True, exist_ok=True)
        path = out_dir / f"ForClaudeHandoff_{ctx.run_id}_{act}.md"
        path.write_text(
            _render(f"{ctx.run_id} — {act}", issues, crashes, mine), encoding="utf-8"
        )
        return path
    except Exception as exc:  # noqa: BLE001
        # Never let a report cost a run. The run-level handoff still follows.
        logger.warning("[handoff] could not write the %s report: %s", act, exc)
        return None
