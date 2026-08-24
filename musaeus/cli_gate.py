"""
MUSAEUS — the CLI's execution-authority gate (P0-11 wiring)

This connects `cli.py` to the preflight and authority machinery. It is
**off by default and opt-in only**, and that is not timidity.

Turning it on by default would change what `musaeus run` does for
everyone at once: a run that has always started immediately would begin
asking `Proceed with authorised execution? [y/N]`, and the overnight
script -- which has no operator and no tty -- would correctly answer "no"
and stop doing anything every night. Both behaviours are *right*, and
both must be adopted deliberately rather than arriving with a merge.
There is a 15-hour job running on this machine as this is written.

So: `MUSAEUS_P0_SAFETY_GATE=1`, or `--safety-gate`, and nothing else
turns it on. `gate_enabled()` has no other branch. When it is off this
module returns None immediately and the CLI behaves exactly as it did.

The splice into `cli.py` is deliberately two lines inside
`_run_pipeline()`, so it merges cleanly alongside unrelated work in that
file.
"""

from __future__ import annotations

import os
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from musaeus.preflight import (
    AUTHORITY_PROMPT,
    PreflightRequest,
    evaluate_authority,
    render_report,
    run_preflight,
)
from musaeus.safety.lock import Scope

GATE_ENV = "MUSAEUS_P0_SAFETY_GATE"
GATE_FLAG = "--safety-gate"

EXIT_SAFETY_BLOCKED = 2
EXIT_REVIEW_ONLY = 0

DOMAIN_LIBRARY_MUTATION = "library-mutation"


def gate_enabled(argv: Sequence[str] | None = None, env: dict[str, str] | None = None) -> bool:
    """
    True only on an explicit opt-in.

    One environment variable, one flag, and no third branch -- no "on when
    a config key is present", no "on unless disabled". A safety gate whose
    activation condition is complicated is a safety gate nobody can say is
    on.
    """
    environment = os.environ if env is None else env
    arguments = sys.argv if argv is None else argv
    if str(environment.get(GATE_ENV, "")).strip() in ("1", "true", "yes", "on"):
        return True
    return GATE_FLAG in list(arguments)


def _default_response(interactive: bool) -> str | None:
    """
    The operator's answer, or None when there is no operator.

    `None` for anything non-interactive. A pipe at EOF yields "", and an
    empty string is one careless `.startswith()` away from being read as
    yes -- so the non-interactive case never produces a string at all.
    """
    if not interactive:
        return None
    if not sys.stdin.isatty():
        return None
    try:
        return input(AUTHORITY_PROMPT)
    except (EOFError, KeyboardInterrupt):
        return None


def build_request(
    config: Any,
    *,
    lock_dir: Path | None = None,
    estimated_checkpoint_bytes: int = 0,
    estimated_quarantine_bytes: int = 0,
    estimated_items: int = 0,
    required_providers: Sequence[str] = (),
    consented_providers: frozenset[str] = frozenset(),
) -> PreflightRequest:
    """Build the preflight request from a live MusicConfig."""
    return PreflightRequest(
        scope=Scope.build(config.vault_root, DOMAIN_LIBRARY_MUTATION),
        source_root=config.inbox,
        destination_root=config.alac_library,
        recovery_root=config.runs_root / "recovery",
        db_path=config.db_path,
        lock_dir=lock_dir if lock_dir is not None else config.runs_root / "locks",
        estimated_checkpoint_bytes=estimated_checkpoint_bytes,
        estimated_quarantine_bytes=estimated_quarantine_bytes,
        estimated_items=estimated_items,
        required_providers=tuple(required_providers),
        consented_providers=consented_providers,
    )


def enforce_execution_gate(
    config: Any,
    *,
    dry_run: bool = False,
    argv: Sequence[str] | None = None,
    env: dict[str, str] | None = None,
    response: str | None = None,
    response_supplied: bool = False,
    interactive: bool = True,
    stream: Any = None,
    **request_kwargs: Any,
) -> int | None:
    """
    Return an exit code the CLI should abort with, or None to proceed.

    None means one of exactly two things: the gate is off, or every check
    passed and the operator said `y`. Nothing else returns None.

    A preview never reaches the authority question -- there is nothing to
    authorise -- so it runs preflight for the report and proceeds.
    """
    if not gate_enabled(argv=argv, env=env):
        return None

    out = stream if stream is not None else sys.stderr
    report = run_preflight(build_request(config, **request_kwargs))
    print(render_report(report), file=out)

    if report.blocked:
        print(
            f"\nREFUSED: {len(report.blocking)} preflight check(s) block execution. "
            f"Nothing was changed.",
            file=out,
        )
        return EXIT_SAFETY_BLOCKED

    if dry_run:
        return None

    answer = response if response_supplied else _default_response(interactive)
    decision = evaluate_authority(report, answer)
    if decision.granted:
        return None

    print(
        "\nReview only: execution authority was not granted "
        f"({decision.reason_code}). Nothing was changed.",
        file=out,
    )
    return EXIT_REVIEW_ONLY
