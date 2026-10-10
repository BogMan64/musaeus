"""
MUSAEUS — read-only preflight and the execution-authority gate (P0-11)

Preflight answers one question -- "may this run be granted authority to
change anything?" -- and answers it without changing anything itself. It
opens the database read-only, observes the scope lock without acquiring
it, measures capacity, and returns a typed report. It creates no run, no
event, no checkpoint, no directory, and no lock.

Two properties are worth stating plainly because they are easy to lose:

**Every check reports measured and required values, not a verdict.** A
check that says "insufficient space" and nothing else cannot be acted on
and cannot be audited. MCR-002 asks for the measured safely usable
capacity, the estimated requirement, and the fixed cap in the report, so
each CheckResult carries them.

**Authority is granted, never inferred.** It does not follow from a path
looking like an INBOX, from a preview having succeeded, from a container
context, or from every check passing. Every check passing makes authority
*available*; an explicit `y` requests it; and only both together grant
it. Enter, "n", an empty line, absent input and EOF are all not-`y`, and
the default in `[y/N]` is not decoration.

The fixed future recovery root is reported as a policy value and never
touched. The tests run under the P0-01 PathGuard, which raises on any
access to it, so "we never probe it" is enforced rather than promised.
"""

from __future__ import annotations

import os
import shutil
import sqlite3
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import quote

from musaeus.safety.lock import FIXTURE, Scope, observe
from musaeus.state.policy import (
    RECOVERY_CAP_BYTES,
    RECOVERY_CAP_LABEL,
    describe_recovery_policy,
)
from musaeus.state.schema import (
    StateError,
    check_compatibility,
    check_ledger_clean,
    read_schema_version,
)

PASS = "pass"
BLOCK = "block"

# Space held back from "free" before calling any of it usable. Filling a
# filesystem to the last byte is how a recovery target stops being able
# to hold the recovery. Declared as visible policy with a named default
# rather than buried in an expression, so it can be argued with.
DEFAULT_SAFETY_RESERVE_BYTES: int = 2 * 10**9
DEFAULT_SAFETY_RESERVE_FRACTION: float = 0.05

# Rough per-item cost of manifest and journal material. MCR-002 requires
# capacity accounting to include manifest/journal overhead, not only the
# bytes of the files themselves.
MANIFEST_BYTES_PER_ITEM: int = 2048

AUTHORITY_PROMPT: str = "Proceed with authorised execution? [y/N] "


class PreflightError(StateError):
    reason_code = "preflight_blocked"


# ── Results ───────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class CheckResult:
    name: str
    outcome: str
    reason_code: str | None = None
    measured: Any = None
    required: Any = None
    remediation: str | None = None
    detail: dict[str, Any] = field(default_factory=dict)

    @property
    def blocked(self) -> bool:
        return self.outcome == BLOCK

    def as_payload(self) -> dict[str, Any]:
        """Shaped for the `checks` array in `run.preflight.completed`,
        which DR-02 requires to be typed objects rather than prose."""
        return {
            "name": self.name,
            "outcome": self.outcome,
            "reason_code": self.reason_code,
            "measured": self.measured,
            "required": self.required,
            "remediation": self.remediation,
        }


@dataclass(frozen=True)
class PreflightReport:
    scope_root: str
    scope_domain: str
    classification: str
    checks: tuple[CheckResult, ...]
    recovery_policy: dict[str, Any]
    lock_observation: dict[str, Any] | None
    database_version: int | None
    recovery_target: str

    @property
    def blocking(self) -> tuple[CheckResult, ...]:
        return tuple(c for c in self.checks if c.blocked)

    @property
    def blocked(self) -> bool:
        return bool(self.blocking)

    @property
    def outcome(self) -> str:
        return BLOCK if self.blocked else PASS

    def check(self, name: str) -> CheckResult:
        for result in self.checks:
            if result.name == name:
                return result
        raise KeyError(name)

    def as_event_payload(self) -> dict[str, Any]:
        """A valid `run.preflight.completed` payload."""
        return {
            "outcome": self.outcome,
            "checks": [c.as_payload() for c in self.checks],
            "database_version": self.database_version,
            "recovery_target": self.recovery_target,
            "lock_observation": self.lock_observation,
        }


@dataclass(frozen=True)
class PreflightRequest:
    scope: Scope
    source_root: Path
    destination_root: Path
    recovery_root: Path
    db_path: Path
    lock_dir: Path
    estimated_checkpoint_bytes: int = 0
    estimated_quarantine_bytes: int = 0
    estimated_items: int = 0
    configuration: Mapping[str, Any] = field(default_factory=dict)
    required_configuration_keys: Sequence[str] = ()
    required_providers: Sequence[str] = ()
    consented_providers: frozenset[str] = frozenset()
    allow_classifications: frozenset[str] = frozenset({FIXTURE})
    safety_reserve_bytes: int = DEFAULT_SAFETY_RESERVE_BYTES
    safety_reserve_fraction: float = DEFAULT_SAFETY_RESERVE_FRACTION


# ── Individual checks ─────────────────────────────────────────────────────────


def _check_scope(request: PreflightRequest) -> CheckResult:
    classification = request.scope.classification
    if classification in request.allow_classifications:
        return CheckResult(
            "scope_classification",
            PASS,
            measured=classification,
            required=sorted(request.allow_classifications),
        )
    return CheckResult(
        "scope_classification",
        BLOCK,
        reason_code="authority_denied",
        measured=classification,
        required=sorted(request.allow_classifications),
        remediation=(
            "this build is fixture-only; the canonical vault and INBOX remain "
            "read-only and no authority is available for them"
        ),
        detail={"scope_root": request.scope.root},
    )


def _check_source_readable(request: PreflightRequest) -> CheckResult:
    path = request.source_root
    if not path.is_dir():
        return CheckResult(
            "source_readable",
            BLOCK,
            reason_code="configuration_invalid",
            measured="absent",
            required="readable directory",
            remediation=f"create or correct the source root {path}",
        )
    if not os.access(path, os.R_OK | os.X_OK):
        return CheckResult(
            "source_readable",
            BLOCK,
            reason_code="configuration_invalid",
            measured="unreadable",
            required="readable directory",
            remediation=f"grant read/traverse permission on {path}",
        )
    return CheckResult("source_readable", PASS, measured="readable", required="readable directory")


def _check_destination_writable(request: PreflightRequest) -> CheckResult:
    path = request.destination_root
    if not path.is_dir():
        return CheckResult(
            "destination_writable",
            BLOCK,
            reason_code="configuration_invalid",
            measured="absent",
            required="writable directory",
            remediation=f"create the destination root {path}",
        )
    if not os.access(path, os.W_OK):
        return CheckResult(
            "destination_writable",
            BLOCK,
            reason_code="configuration_invalid",
            measured="unwritable",
            required="writable directory",
            remediation=f"grant write permission on {path}",
        )
    return CheckResult(
        "destination_writable", PASS, measured="writable", required="writable directory"
    )


def _check_recovery_target(request: PreflightRequest) -> CheckResult:
    from musaeus.state.migrator import _assert_usable_recovery_root

    try:
        _assert_usable_recovery_root(request.recovery_root)
    except StateError as exc:
        return CheckResult(
            "recovery_target",
            BLOCK,
            reason_code=exc.reason_code,
            measured=str(request.recovery_root),
            required="an existing, writable, disposable recovery root",
            remediation=str(exc.details.get("remediation", "supply a usable recovery root")),
        )
    return CheckResult(
        "recovery_target",
        PASS,
        measured=str(request.recovery_root),
        required="an existing, writable, disposable recovery root",
    )


def estimate_recovery_requirement(request: PreflightRequest) -> int:
    """Total bytes the recovery target must be able to hold.

    Checkpoint plus quarantine plus the database plus manifest/journal
    overhead. Counting only the file bytes would under-estimate exactly
    the material that makes a rollback possible."""
    db_bytes = request.db_path.stat().st_size if request.db_path.is_file() else 0
    return (
        request.estimated_checkpoint_bytes
        + request.estimated_quarantine_bytes
        + db_bytes
        + request.estimated_items * MANIFEST_BYTES_PER_ITEM
    )


def safely_usable_bytes(path: Path, request: PreflightRequest) -> int:
    """Free space minus a held-back reserve, floored at zero."""
    usage = shutil.disk_usage(str(path))
    reserve = max(request.safety_reserve_bytes, int(usage.total * request.safety_reserve_fraction))
    return max(0, usage.free - reserve)


def _check_recovery_cap(request: PreflightRequest, required: int) -> CheckResult:
    if required > RECOVERY_CAP_BYTES:
        return CheckResult(
            "recovery_cap",
            BLOCK,
            reason_code="recovery_capacity_exceeded",
            measured=required,
            required=RECOVERY_CAP_BYTES,
            remediation=(
                f"the estimated checkpoint/quarantine requirement exceeds the fixed "
                f"{RECOVERY_CAP_LABEL} cap; reduce the scope of this run"
            ),
            detail={"cap_label": RECOVERY_CAP_LABEL},
        )
    return CheckResult(
        "recovery_cap",
        PASS,
        measured=required,
        required=RECOVERY_CAP_BYTES,
        detail={"cap_label": RECOVERY_CAP_LABEL},
    )


def _check_recovery_capacity(request: PreflightRequest, required: int) -> CheckResult:
    if not request.recovery_root.is_dir():
        return CheckResult(
            "recovery_capacity",
            BLOCK,
            reason_code="recovery_capacity_exceeded",
            measured=None,
            required=required,
            remediation="the recovery target is unavailable, so its capacity is unknown",
        )
    usable = safely_usable_bytes(request.recovery_root, request)
    if required > usable:
        return CheckResult(
            "recovery_capacity",
            BLOCK,
            reason_code="recovery_capacity_exceeded",
            measured=usable,
            required=required,
            remediation=(
                f"the recovery target has {usable} safely usable bytes and this run "
                f"needs {required}; free space or reduce the scope"
            ),
        )
    return CheckResult("recovery_capacity", PASS, measured=usable, required=required)


def _check_schema(request: PreflightRequest) -> tuple[CheckResult, int | None]:
    if not request.db_path.is_file():
        return (
            CheckResult(
                "schema_compatible",
                PASS,
                measured=None,
                required="a supported schema version",
                detail={"note": "no database yet; it will be created at the current version"},
            ),
            None,
        )
    try:
        conn = sqlite3.connect(f"file:{quote(str(request.db_path))}?mode=ro", uri=True, timeout=10)
    except sqlite3.Error as exc:
        return (
            CheckResult(
                "schema_compatible",
                BLOCK,
                reason_code="schema_incompatible",
                measured="unreadable",
                required="a readable database",
                remediation=f"the database could not be opened read-only: {exc}",
            ),
            None,
        )
    try:
        conn.row_factory = sqlite3.Row
        version = read_schema_version(conn)
        try:
            check_compatibility(version, db_path=request.db_path)
            check_ledger_clean(conn, db_path=request.db_path)
        except StateError as exc:
            return (
                CheckResult(
                    "schema_compatible",
                    BLOCK,
                    reason_code=exc.reason_code,
                    measured=version,
                    required="a supported schema version with no unfinished migration",
                    remediation=str(exc.details.get("remediation", str(exc))),
                ),
                version,
            )
    finally:
        conn.close()
    return (
        CheckResult(
            "schema_compatible",
            PASS,
            measured=version,
            required="a supported schema version",
        ),
        version,
    )


def _check_configuration(request: PreflightRequest) -> CheckResult:
    missing = [k for k in request.required_configuration_keys if k not in request.configuration]
    empty = [
        k
        for k in request.required_configuration_keys
        if k in request.configuration and request.configuration[k] in (None, "")
    ]
    if missing or empty:
        return CheckResult(
            "configuration_valid",
            BLOCK,
            reason_code="configuration_invalid",
            measured={"missing": missing, "empty": empty},
            required=list(request.required_configuration_keys),
            remediation=(
                f"set the missing configuration value(s): {', '.join(sorted(missing + empty))}"
            ),
        )
    return CheckResult(
        "configuration_valid",
        PASS,
        measured=sorted(request.required_configuration_keys),
        required=list(request.required_configuration_keys),
    )


def _check_provider_consent(request: PreflightRequest) -> CheckResult:
    missing = sorted(set(request.required_providers) - set(request.consented_providers))
    if missing:
        return CheckResult(
            "provider_consent",
            BLOCK,
            reason_code="provider_not_enabled",
            measured=sorted(request.consented_providers),
            required=sorted(request.required_providers),
            remediation=(
                f"this run needs consent for: {', '.join(missing)}; enable them or run "
                f"without the stages that use them"
            ),
        )
    return CheckResult(
        "provider_consent",
        PASS,
        measured=sorted(request.consented_providers),
        required=sorted(request.required_providers),
    )


def _check_lock(request: PreflightRequest) -> tuple[CheckResult, dict[str, Any] | None]:
    """Observe the lock. Never acquire it.

    Preflight asks whether authority is *available*; acquiring here would
    mean preflight itself takes the scope, so a preflight that then
    declines to run would leave the scope held by nobody in particular."""
    from dataclasses import asdict

    holder = observe(request.scope, request.lock_dir)
    if holder is None:
        return (
            CheckResult("lock_observation", PASS, measured="free", required="no conflicting run"),
            {"held": False, "owner": None},
        )
    observation = {"held": True, "owner": asdict(holder)}
    return (
        CheckResult(
            "lock_observation",
            BLOCK,
            reason_code="lock_conflict",
            measured=holder.describe(),
            required="no conflicting run",
            remediation=(
                f"run {holder.run_id} (pid {holder.pid} on {holder.hostname}) holds this "
                f"scope; wait for it to finish or target a different scope"
            ),
        ),
        observation,
    )


# ── The gate ──────────────────────────────────────────────────────────────────


def run_preflight(request: PreflightRequest) -> PreflightReport:
    """
    Run every check and return a typed report. Read-only throughout.

    Checks are all evaluated rather than short-circuiting on the first
    block: an operator fixing one problem at a time, discovering the next
    only after another full attempt, is how a five-minute fix becomes an
    evening.
    """
    required = estimate_recovery_requirement(request)
    schema_check, version = _check_schema(request)
    lock_check, observation = _check_lock(request)

    checks = (
        _check_scope(request),
        _check_source_readable(request),
        _check_destination_writable(request),
        _check_recovery_target(request),
        _check_recovery_cap(request, required),
        _check_recovery_capacity(request, required),
        schema_check,
        _check_configuration(request),
        _check_provider_consent(request),
        lock_check,
    )

    return PreflightReport(
        scope_root=request.scope.root,
        scope_domain=request.scope.domain,
        classification=request.scope.classification,
        checks=checks,
        recovery_policy=describe_recovery_policy(),
        lock_observation=observation,
        database_version=version,
        recovery_target=str(request.recovery_root),
    )


@dataclass(frozen=True)
class AuthorityDecision:
    granted: bool
    reason_code: str
    report: PreflightReport
    response: str | None = None

    def require(self) -> None:
        """Raise unless authority was granted. For callers that would
        otherwise have to remember to check a boolean."""
        if not self.granted:
            raise PreflightError(
                f"execution authority not granted: {self.reason_code}",
                reason=self.reason_code,
                blocking=[c.name for c in self.report.blocking],
            )


def is_affirmative(response: str | None) -> bool:
    """
    True only for an explicit `y`.

    Everything else is no: Enter, an empty string, "n", "yes", "sure", and
    -- importantly for P0-17 -- `None`, which is what absent input and EOF
    look like to a scheduled run. A non-interactive invocation must never
    fall through to yes because nobody was there to say no.
    """
    if response is None:
        return False
    return response.strip() == "y"


def evaluate_authority(report: PreflightReport, response: str | None) -> AuthorityDecision:
    """
    Grant fixture mutation authority only when every check passed AND the
    operator answered `y`.

    The order matters for what gets reported: a blocked preflight is
    reported as blocked even if the answer was `y`, because "you cannot"
    is more useful than "you did not confirm" when both are true.
    """
    if report.blocked:
        return AuthorityDecision(
            granted=False,
            reason_code="preflight_blocked",
            report=report,
            response=response,
        )
    if not is_affirmative(response):
        return AuthorityDecision(
            granted=False,
            reason_code="authority_not_requested",
            report=report,
            response=response,
        )
    return AuthorityDecision(
        granted=True, reason_code="authority_granted", report=report, response=response
    )


def render_report(report: PreflightReport) -> str:
    """Human-readable preflight summary, safe to print to stdout."""
    lines = [
        f"Preflight for {report.scope_root} (domain {report.scope_domain})",
        f"  classification : {report.classification}",
        f"  database       : version {report.database_version}",
        f"  recovery target: {report.recovery_target}",
        f"  recovery policy: future root {report.recovery_policy['future_recovery_root']} "
        f"(not created or probed), cap {report.recovery_policy['recovery_cap_label']}",
        "",
    ]
    for check in report.checks:
        marker = "BLOCK" if check.blocked else "ok   "
        lines.append(
            f"  [{marker}] {check.name}: measured={check.measured!r} required={check.required!r}"
        )
        if check.remediation:
            lines.append(f"          -> {check.remediation}")
    lines.append("")
    lines.append(f"  outcome: {report.outcome}")
    return "\n".join(lines)
