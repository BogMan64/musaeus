"""Shared fail-closed compatibility guard for unsafe legacy previews."""

from __future__ import annotations

import argparse
import sys
from collections.abc import Callable
from typing import Any

from .planning import RunMode

LEGACY_PREVIEW_EXIT_CODE = 2
LEGACY_PREVIEW_MESSAGE = (
    "Safety block: preview/dry-run is temporarily unavailable because the legacy "
    "implementation can persist state. No MUSAEUS managed configuration, database, "
    "library/files, logs, or network work is started for the blocked preview. Run "
    "without --dry-run only if you explicitly intend an authorised live run, or wait "
    "for the safe-preview repair."
)
LEGACY_PREVIEW_HELP = (
    "Temporarily unavailable: fails closed because legacy preview can persist state"
)
LEGACY_PREVIEW_GUARD_ATTR = "_legacy_preview_guard"
# Register recognised historical command spellings here. Parser tests require any
# accepted spelling in this registry to set the shared guard marker explicitly.
LEGACY_PREVIEW_COMMANDS = frozenset({"dry-run", "dryrun", "preview"})


class LegacyPreviewAction(argparse.Action):
    """Mark a legacy ``--dry-run`` option for the shared early compatibility guard."""

    def __init__(self, option_strings: list[str], dest: str, **kwargs: Any) -> None:
        kwargs["nargs"] = 0
        kwargs.setdefault("default", False)
        super().__init__(option_strings, dest, **kwargs)

    def __call__(
        self,
        parser: argparse.ArgumentParser,
        namespace: argparse.Namespace,
        values: object,
        option_string: str | None = None,
    ) -> None:
        del parser, values, option_string
        setattr(namespace, self.dest, True)
        namespace.run_mode = RunMode.PREVIEW
        setattr(namespace, LEGACY_PREVIEW_GUARD_ATTR, True)


def mark_legacy_preview_command(parser: argparse.ArgumentParser) -> None:
    """Mark a legacy preview command alias so command-only routes fail closed."""
    parser.set_defaults(
        **{
            LEGACY_PREVIEW_GUARD_ATTR: True,
            "run_mode": RunMode.PREVIEW,
        }
    )


def legacy_preview_requested(args: object) -> bool:
    """Return whether parsed CLI metadata selected an unsafe legacy preview route."""
    return bool(
        getattr(args, LEGACY_PREVIEW_GUARD_ATTR, False)
        or getattr(args, "dry_run", False)
        or getattr(args, "run_mode", None) is RunMode.PREVIEW
        or getattr(args, "command", None) in LEGACY_PREVIEW_COMMANDS
    )


def reject_legacy_preview(
    writer: Callable[[str], None] | None = None,
) -> int:
    """Render the stable fail-closed message and return its non-success exit status."""
    if writer is None:
        print(LEGACY_PREVIEW_MESSAGE, file=sys.stderr)
    else:
        writer(LEGACY_PREVIEW_MESSAGE)
    return LEGACY_PREVIEW_EXIT_CODE
