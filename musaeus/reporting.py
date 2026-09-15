"""
MUSAEUS — run-scoped reports and shareable redaction (P0-16)

Two audiences, one source of truth.

The **restricted** report is for the operator at the machine. It names
paths, because recovery needs them: "restore the quarantined item" is not
actionable without knowing where it came from.

The **shareable** report is for anywhere else -- a pasted excerpt, a
ticket, a future Thunderbird compose draft. It carries item references
rather than paths and redacts anything credential-shaped. Both are
rendered from the same `RunReport`, so they cannot drift into disagreeing
about what happened; the difference is a projection, not a second write-up.

The distinction the report exists to make is **planned versus applied**.
DR-08 asks for it explicitly and it is the difference between "we would
move 11,160 files" and "we moved 11,160 files". A report that blurs the
two is worse than no report, because it reads as authoritative.

Preview never writes a report file. A preview that leaves a report behind
has changed managed state, which is the one thing preview promises not to
do -- so `write_report` refuses for preview mode rather than quietly
choosing a different directory.

No mail is sent, no SMTP credential is read, and no compose draft is
generated. The redacted payload this produces is what a future P2 adapter
would consume; that adapter is not here.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

from musaeus.state.policy import RECOVERY_CAP_LABEL

REDACTED = "[redacted]"

# Keys whose values never appear in any report, restricted or shareable.
# A credential in the restricted report is still a credential written to
# disk in plain text.
CREDENTIAL_KEY_PATTERN = re.compile(
    r"(api[_-]?key|token|secret|password|passwd|credential)", re.IGNORECASE
)

MODE_PREVIEW = "preview"
MODE_EXECUTE = "execute"


class ReportError(RuntimeError):
    pass


def item_ref_for_path(path: str) -> str:
    """Stable, non-reversible reference for a path.

    Stable so the same item correlates across reports and across runs;
    non-reversible so a shareable report does not disclose the shape of
    someone's library. DR-02: shareable reports use the reference only."""
    return "item:" + hashlib.sha256(path.encode("utf-8")).hexdigest()[:16]


@dataclass(frozen=True)
class ActionCounts:
    """DR-08's required counts. Planned and applied are separate fields,
    never one number with a caveat."""

    planned: int = 0
    applied: int = 0
    skipped: int = 0
    failed: int = 0
    cancelled: int = 0
    rolled_back: int = 0

    def as_dict(self) -> dict[str, int]:
        return {
            "planned": self.planned,
            "applied": self.applied,
            "skipped": self.skipped,
            "failed": self.failed,
            "cancelled": self.cancelled,
            "rolled_back": self.rolled_back,
        }


@dataclass(frozen=True)
class StageReport:
    stage_id: str
    attempt: int
    status: str
    counts: ActionCounts = field(default_factory=ActionCounts)
    error_code: str | None = None
    blockers: tuple[str, ...] = ()
    recovery_action: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "stage_id": self.stage_id,
            "attempt": self.attempt,
            "status": self.status,
            "counts": self.counts.as_dict(),
            "error_code": self.error_code,
            "blockers": list(self.blockers),
            "recovery_action": self.recovery_action,
        }


@dataclass(frozen=True)
class RunReport:
    run_id: str
    mode: str
    scope_root: str
    classification: str
    started_at: str
    finished_at: str | None = None
    status: str = "pending"
    exit_code: int | None = None
    reason_code: str | None = None
    config_digest: str | None = None
    stages: tuple[StageReport, ...] = ()
    totals: ActionCounts = field(default_factory=ActionCounts)
    safety_blocks: tuple[dict[str, Any], ...] = ()
    checkpoint_id: str | None = None
    manifest_digest: str | None = None
    quarantine_refs: tuple[str, ...] = ()
    rollback_status: str | None = None
    lock_observation: dict[str, Any] | None = None
    authority: str | None = None
    recovery_target: str | None = None
    recovery_cap_label: str = RECOVERY_CAP_LABEL
    network_use: tuple[dict[str, Any], ...] = ()
    next_actions: tuple[str, ...] = ()
    # item_ref -> real path. Restricted only; never rendered in a
    # shareable report. Kept because recovery needs it and nothing else
    # does.
    path_map: dict[str, str] = field(default_factory=dict)
    shareable: bool = False

    def known_paths(self) -> tuple[str, ...]:
        """Every real path this report holds, for exact-match redaction."""
        paths = [self.scope_root, *self.path_map.values()]
        if self.recovery_target:
            paths.append(self.recovery_target)
        return tuple(p for p in paths if p)

    def as_dict(self) -> dict[str, Any]:
        body: dict[str, Any] = {
            "run_id": self.run_id,
            "mode": self.mode,
            "scope": {
                "root": self.scope_root,
                "classification": self.classification,
            },
            "timestamps": {"started_at": self.started_at, "finished_at": self.finished_at},
            "status": self.status,
            "exit_code": self.exit_code,
            "reason_code": self.reason_code,
            "config_digest": self.config_digest,
            "stages": [s.as_dict() for s in self.stages],
            "totals": self.totals.as_dict(),
            "safety_blocks": [dict(b) for b in self.safety_blocks],
            "recovery": {
                "checkpoint_id": self.checkpoint_id,
                "manifest_digest": self.manifest_digest,
                "quarantine_refs": list(self.quarantine_refs),
                "rollback_status": self.rollback_status,
                "recovery_target": self.recovery_target,
                "recovery_cap": self.recovery_cap_label,
            },
            "lock_observation": self.lock_observation,
            "authority": self.authority,
            "network_use": [dict(n) for n in self.network_use],
            "next_actions": list(self.next_actions),
        }
        if not self.shareable:
            body["restricted_path_map"] = dict(self.path_map)
        # Credential-shaped values are stripped from BOTH forms. A
        # credential in the restricted report is still a credential
        # written to disk in plain text -- only the PATH treatment differs
        # between restricted and shareable. Caught by a test asserting the
        # restricted file was clean, which it was not.
        redacted = _redact_credentials(body)
        assert isinstance(redacted, dict)
        return redacted


# ── Redaction ─────────────────────────────────────────────────────────────────


def _redact_credentials(value: Any) -> Any:
    """Strip credential-shaped values. Applied to every report, always."""
    if isinstance(value, dict):
        return {
            k: (REDACTED if CREDENTIAL_KEY_PATTERN.search(str(k)) else _redact_credentials(v))
            for k, v in value.items()
        }
    if isinstance(value, list):
        return [_redact_credentials(v) for v in value]
    return value


def _redact_any(value: Any, known_paths: tuple[str, ...] = ()) -> Any:
    """Credential AND path redaction. Applied only to shareable output."""
    if isinstance(value, dict):
        return {
            k: (REDACTED if CREDENTIAL_KEY_PATTERN.search(str(k)) else _redact_any(v, known_paths))
            for k, v in value.items()
        }
    if isinstance(value, list):
        return [_redact_any(v, known_paths) for v in value]
    if isinstance(value, str):
        if _looks_like_a_path(value):
            return item_ref_for_path(value)
        return _redact_paths_in_text(value, known_paths)
    return value


# An absolute path with at least one directory component and no
# whitespace. Used for paths appearing INSIDE prose; a string that is
# wholly a path is handled separately, which is what lets paths
# containing spaces be redacted at all.
_EMBEDDED_PATH = re.compile(r"(?<![\w])/(?:[^\s/]+/)+[^\s/]*")


def _looks_like_a_path(value: str) -> bool:
    """True when the whole string is an absolute POSIX path.

    Deliberately narrow. A redactor that guesses aggressively turns
    ordinary text -- a reason code, a stage name -- into opaque references
    and makes the shareable report unreadable, which is its own way of
    being useless."""
    return value.startswith("/") and len(value) > 1


def _redact_paths_in_text(value: str, known_paths: tuple[str, ...] = ()) -> str:
    """Replace paths appearing inside a sentence with item references.

    Two passes, because neither alone is enough. Known paths are replaced
    verbatim first -- that is the only way a path containing spaces can be
    delimited reliably, and library paths are full of spaces ("Bob Seger",
    "Eight Miles High"). The regex then catches whitespace-free paths the
    report did not already know about, such as one embedded in a
    remediation sentence.

    A path with spaces that the report has never seen before still cannot
    be delimited from surrounding prose. The answer to that is not a
    cleverer regex: remediation text should carry item references rather
    than raw paths in the first place.
    """
    for path in sorted(known_paths, key=len, reverse=True):
        if path and path in value:
            value = value.replace(path, item_ref_for_path(path))
    return _EMBEDDED_PATH.sub(lambda m: item_ref_for_path(m.group(0)), value)


def to_shareable(report: RunReport) -> RunReport:
    """
    Project *report* into its shareable form.

    Paths become stable item references, credential-shaped values become
    `[redacted]`, and the restricted path map is dropped entirely rather
    than redacted in place -- a map from references to paths is exactly
    the thing that would undo the redaction.
    """
    known = report.known_paths()
    return replace(
        report,
        scope_root=item_ref_for_path(report.scope_root),
        recovery_target=(
            item_ref_for_path(report.recovery_target) if report.recovery_target else None
        ),
        lock_observation=(
            _redact_any(report.lock_observation, known) if report.lock_observation else None
        ),
        network_use=tuple(_redact_any(dict(n), known) for n in report.network_use),
        safety_blocks=tuple(_redact_any(dict(b), known) for b in report.safety_blocks),
        stages=tuple(
            replace(
                stage,
                blockers=tuple(str(_redact_any(b, known)) for b in stage.blockers),
                recovery_action=(
                    str(_redact_any(stage.recovery_action, known))
                    if stage.recovery_action
                    else None
                ),
            )
            for stage in report.stages
        ),
        next_actions=tuple(str(_redact_any(a, known)) for a in report.next_actions),
        path_map={},
        shareable=True,
    )


# ── Rendering ─────────────────────────────────────────────────────────────────


def render_json(report: RunReport) -> str:
    return json.dumps(report.as_dict(), sort_keys=True, indent=2)


def render_human(report: RunReport) -> str:
    """Operator-facing text. Planned and applied are always both shown,
    on the same line, so the difference cannot be read past."""
    lines = [
        f"MUSAEUS run {report.run_id}  [{report.mode}]",
        f"  scope        : {report.scope_root}  ({report.classification})",
        f"  started      : {report.started_at}",
        f"  finished     : {report.finished_at or '-'}",
        f"  status       : {report.status}"
        + (f"  exit={report.exit_code}" if report.exit_code is not None else ""),
        f"  authority    : {report.authority or 'not granted'}",
        f"  config digest: {report.config_digest or '-'}",
        "",
        "  Actions            planned  applied  skipped   failed  cancelled  rolled-back",
        "  {:<16} {:>8} {:>8} {:>8} {:>8} {:>10} {:>12}".format(
            "total",
            report.totals.planned,
            report.totals.applied,
            report.totals.skipped,
            report.totals.failed,
            report.totals.cancelled,
            report.totals.rolled_back,
        ),
    ]
    for stage in report.stages:
        counts = stage.counts
        lines.append(
            f"  {stage.stage_id[:16]:<16} {counts.planned:>8} {counts.applied:>8} {counts.skipped:>8} {counts.failed:>8} {counts.cancelled:>10} {counts.rolled_back:>12}"
        )
        if stage.status != "succeeded":
            lines.append(
                f"      status: {stage.status}"
                + (f"  ({stage.error_code})" if stage.error_code else "")
            )
        if stage.recovery_action:
            lines.append(f"      -> {stage.recovery_action}")

    lines.extend(
        [
            "",
            "  Recovery",
            f"    checkpoint     : {report.checkpoint_id or '-'}",
            f"    manifest digest: {report.manifest_digest or '-'}",
            f"    quarantined    : {len(report.quarantine_refs)} item(s)",
            f"    rollback       : {report.rollback_status or '-'}",
            f"    recovery target: {report.recovery_target or '-'} (cap {report.recovery_cap_label})",
        ]
    )

    if report.safety_blocks:
        lines.append("")
        lines.append("  Safety blocks")
        for block in report.safety_blocks:
            lines.append(f"    [{block.get('reason_code', '?')}] {block.get('name', '?')}")
            if block.get("remediation"):
                lines.append(f"      -> {block['remediation']}")

    if report.lock_observation:
        lines.append("")
        held = report.lock_observation.get("held")
        lines.append(f"  Lock: {'held' if held else 'free'}")
        owner = report.lock_observation.get("owner")
        if owner:
            lines.append(f"    owner: run {owner.get('run_id')} (pid {owner.get('pid')})")

    lines.append("")
    lines.append(f"  Network use: {len(report.network_use)} call(s)")
    for use in report.network_use:
        lines.append(f"    {use.get('provider', '?')}: {use.get('outcome', '?')}")

    if report.next_actions:
        lines.append("")
        lines.append("  Next actions")
        for action in report.next_actions:
            lines.append(f"    - {action}")

    return "\n".join(lines)


# ── Persistence ───────────────────────────────────────────────────────────────


def write_report(report: RunReport, report_root: Path) -> tuple[Path, Path]:
    """
    Write the restricted and shareable reports. Returns both paths.

    Refuses in preview mode. A preview that leaves a report behind has
    changed managed state, which is the one thing preview promises not to
    do -- so this raises rather than quietly writing somewhere else.
    """
    if report.mode == MODE_PREVIEW:
        raise ReportError(
            "preview does not write reports; render it to stdout instead. A preview that "
            "leaves a file behind has changed managed state."
        )
    report_root.mkdir(parents=True, exist_ok=True)
    restricted = report_root / f"{report.run_id}.restricted.json"
    shareable = report_root / f"{report.run_id}.shareable.json"
    restricted.write_text(render_json(report))
    shareable.write_text(render_json(to_shareable(report)))
    return restricted, shareable
