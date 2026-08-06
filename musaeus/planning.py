"""Typed, side-effect-free planning primitives for MUSAEUS commands.

This module deliberately accepts only value objects.  It does not resolve
configuration, open databases, instantiate stages, touch paths, or request
execution authority.  P0-05 will wire the planner into the public preview
boundary once its zero-side-effect proof is complete.
"""

from __future__ import annotations

import hashlib
import json
import sys
from dataclasses import dataclass
from enum import Enum
from typing import TextIO

from .network_policy import (
    LocalOnlyNetworkPolicyGateway,
    NetworkPolicyGateway,
    PreviewNetworkPolicy,
)


class RunMode(str, Enum):
    """The only command modes recognised at the P0 command boundary."""

    EXECUTE = "execute"
    PREVIEW = "preview"


class PreviewOutputFormat(str, Enum):
    """Supported in-memory/stdout renderings for a preview result."""

    HUMAN = "human"
    JSON = "json"


class PreviewUsageError(ValueError):
    """Invalid preview request that must be reported as command usage failure."""

    exit_code = 64


PREVIEW_COMPLETE_EXIT_CODE = 0
PREVIEW_BLOCKED_EXIT_CODE = 2
INVALID_USAGE_EXIT_CODE = 64

# These options select or create a persistent output/state boundary in the
# legacy CLI.  Preview rejects them rather than silently changing their meaning.
PERSISTENCE_OPTION_NAMES = frozenset(
    {
        "csv",
        "export-root",
        "report-path",
        "reset",
        "write-report",
    }
)


@dataclass(frozen=True)
class CommandRequest:
    """Typed command intent suitable for pure planning.

    ``declared_scope`` is reporting data only.  It is deliberately never used to
    derive execution authority, regardless of whether it resembles INBOX, a
    mounted path, or a container path.
    """

    command: str
    mode: RunMode
    stage_names: tuple[str, ...] = ()
    declared_scope: str = "undeclared"
    persistence_options: tuple[str, ...] = ()
    assumptions: tuple[str, ...] = ()


@dataclass(frozen=True)
class PlannedAction:
    """A proposed operation described without constructing a stage object."""

    stage_id: str
    operation: str
    item_reference: str | None = None
    requires_execution_authority: bool = False

    def to_dict(self) -> dict[str, object]:
        return {
            "plan": self.plan.to_dict(),
            "network_policy": {
                "name": self.network_policy.name,
                "external_lookup_permitted": self.network_policy.external_lookup_permitted,
            },
            "external_lookup_performed": self.external_lookup_performed,
            "managed_state_changed": self.managed_state_changed,
        }


@dataclass(frozen=True)
class Plan:
    """Deterministic, in-memory preview plan with no execution capability."""

    run_id: str
    mode: RunMode
    command: str
    declared_scope: str
    actions: tuple[PlannedAction, ...]
    assumptions: tuple[str, ...]
    execution_authority: str = "not_requested_or_granted"

    @property
    def proposed_action_count(self) -> int:
        return len(self.actions)

    def to_dict(self) -> dict[str, object]:
        return {
            "run_id": self.run_id,
            "mode": self.mode.value,
            "command": self.command,
            "declared_scope": self.declared_scope,
            "execution_authority": self.execution_authority,
            "proposed_action_count": self.proposed_action_count,
            "actions": [action.to_dict() for action in self.actions],
            "assumptions": list(self.assumptions),
        }


@dataclass(frozen=True)
class PreviewResult:
    """The complete non-persistent result returned by preview planning."""

    plan: Plan
    network_policy: PreviewNetworkPolicy
    external_lookup_performed: bool = False
    managed_state_changed: bool = False

    def to_dict(self) -> dict[str, object]:
        return {
            "plan": self.plan.to_dict(),
            "network_policy": {
                "name": self.network_policy.name,
                "external_lookup_permitted": self.network_policy.external_lookup_permitted,
            },
            "external_lookup_performed": self.external_lookup_performed,
            "managed_state_changed": self.managed_state_changed,
        }


def normalise_persistence_options(options: tuple[str, ...]) -> tuple[str, ...]:
    """Return stable option names and reject any persistence-bearing preview input."""
    normalised = tuple(sorted({option.lstrip("-").replace("_", "-") for option in options}))
    rejected = tuple(option for option in normalised if option in PERSISTENCE_OPTION_NAMES)
    if rejected:
        labels = ", ".join(f"--{option}" for option in rejected)
        raise PreviewUsageError(
            f"Preview cannot be combined with persistence option(s): {labels}. "
            "Remove them; preview renders only to stdout or an in-memory result."
        )
    return normalised


def build_preview_plan(
    request: CommandRequest,
    network_policy: NetworkPolicyGateway | None = None,
) -> PreviewResult:
    """Create a deterministic preview without stage construction or authority.

    ``network_policy`` is injected so preview never reaches a transport client
    directly.  Omitting it selects the local-only gateway, and a policy that
    permits external lookup is rejected rather than silently changing preview's
    safety contract.

    The caller supplies stage *names*, not stage instances or classes requiring
    construction.  Planning therefore cannot trigger a mutation-capable stage's
    constructor or infer authority from any requested scope/context.
    """
    if request.mode is not RunMode.PREVIEW:
        raise PreviewUsageError("A preview plan requires RunMode.PREVIEW.")

    gateway = network_policy or LocalOnlyNetworkPolicyGateway()
    policy = gateway.preview_policy()
    if policy.external_lookup_permitted:
        raise PreviewUsageError(
            "Preview requires a local-only network policy; use a separately labelled "
            "network-preview mode when that capability is implemented."
        )

    persistence_options = normalise_persistence_options(request.persistence_options)
    stage_names = tuple(str(stage_name) for stage_name in request.stage_names)
    if any(not stage_name for stage_name in stage_names):
        raise PreviewUsageError("Preview stage names must be non-empty strings.")

    assumptions = tuple(
        sorted(
            {
                "External lookup was not performed.",
                "No execution authority is requested or granted.",
                "No managed state was changed.",
                *request.assumptions,
            }
        )
    )
    identity = {
        "command": request.command,
        "declared_scope": request.declared_scope,
        "persistence_options": persistence_options,
        "stage_names": stage_names,
        "assumptions": assumptions,
    }
    digest = hashlib.sha256(
        json.dumps(identity, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    actions = tuple(
        PlannedAction(stage_id=stage_name, operation="stage_evaluation")
        for stage_name in stage_names
    )
    plan = Plan(
        run_id=f"preview-{digest[:12]}",
        mode=RunMode.PREVIEW,
        command=request.command,
        declared_scope=request.declared_scope,
        actions=actions,
        assumptions=assumptions,
    )
    return PreviewResult(plan=plan, network_policy=policy)


def render_preview(
    result: PreviewResult,
    output_format: PreviewOutputFormat = PreviewOutputFormat.HUMAN,
    stream: TextIO | None = None,
) -> None:
    """Render a preview only to an injected stream (stdout by default)."""
    target = sys.stdout if stream is None else stream
    if output_format is PreviewOutputFormat.JSON:
        print(json.dumps(result.to_dict(), sort_keys=True), file=target)
        return

    plan = result.plan
    print(f"Preview {plan.run_id}", file=target)
    print(f"  Scope: {plan.declared_scope}", file=target)
    print(f"  Proposed actions: {plan.proposed_action_count}", file=target)
    print("  Action summary:", file=target)
    if plan.actions:
        for action in plan.actions:
            print(f"    - {action.stage_id}: {action.operation}", file=target)
    else:
        print("    - No stage action is defined for this command.", file=target)
    print("  Network policy: local-only", file=target)
    print("  Safety: No managed state was changed; external lookup was not performed.", file=target)
    print("  Managed state: unchanged", file=target)
    print("  External lookup: not performed", file=target)
    print("  Execution authority: not requested or granted", file=target)
    if plan.assumptions:
        print("  Assumptions:", file=target)
        for assumption in plan.assumptions:
            print(f"    - {assumption}", file=target)
