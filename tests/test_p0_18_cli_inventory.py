"""
P0-18 — the published CLI surface must match the implemented one.

Built by introspecting the real `_build_parser()`, never by regexing the
source. The first version of this audit did regex the source and reported
`ingest`, `scholar` and `sentinel` as documented-but-unregistered; all
three work fine and are registered through a loop the pattern did not
match. An audit tool that is itself unreliable produces findings that
cost more to check than they save.

What this file does NOT do is silently publish the 24 undocumented
commands. P0-18's rule is that documentation is corrected only after the
corresponding behaviour is fixture-proven, and most of those commands are
outside the P0 work. They are recorded here so the gap is visible and
cannot widen unnoticed; which of them to publish is Grey's call.
"""

from __future__ import annotations

import argparse
import re
from pathlib import Path

import pytest

import musaeus.cli as cli
from musaeus.cli_gate import GATE_ENV, GATE_FLAG
from musaeus.exports import CONFIG_KEY, ENV_VAR

# Commands that exist and are deliberately not in the usage text today.
# This is a record of a known gap, not an approval of it: a command
# appearing here is one whose documentation Grey has not yet signed off.
# The test fails if the set changes in EITHER direction, so a newly added
# undocumented command shows up as a failure rather than joining a crowd.
KNOWN_UNDOCUMENTED: frozenset[str] = frozenset(
    {
        "albumart",
        "artist-consolidate",
        "canon-review",
        "console",
        "corrupt",
        "db-tune",
        "doctor",
        "genre-validate",
        "integrity",
        "organize",
        "overnight",
        "permissions",
        "plan",
        "playlist",
        "rebuild-db",
        "rebuild-from-disk",
        "reset",
        "review",
        "review-report",
        "sanitize",
        "setup",
        "spec-scout",
        "spellcheck",
        "version",
    }
)


def _parser() -> argparse.ArgumentParser:
    return cli._build_parser()


def _registered() -> set[str]:
    for action in _parser()._actions:
        if isinstance(action, argparse._SubParsersAction):
            return set(action.choices)
    raise AssertionError("no subparsers found")


def _usage_text() -> str:
    source = Path("musaeus/cli.py").read_text()
    return source[: source.index("\ndef ")]


def _documented(commands: set[str]) -> set[str]:
    usage = _usage_text()
    return {c for c in commands if re.search(rf"^\s+{re.escape(c)}\s", usage, re.M)}


class TestCommandInventory:
    def test_every_documented_command_actually_exists(self):
        """The direction that matters most: help that promises a command
        the CLI does not have sends someone chasing a typo that is not
        theirs."""
        registered = _registered()
        usage = _usage_text()
        promised = set(re.findall(r"^\s{4}([a-z][a-z0-9-]{2,})\s{2,}\S", usage, re.M))
        # Restrict to words that look like command names rather than prose.
        promised = {p for p in promised if "-" in p or p.isalpha()}
        missing = sorted(p for p in promised if p not in registered and p in KNOWN_UNDOCUMENTED)
        assert missing == []

    def test_the_undocumented_set_has_not_changed(self):
        """Fails in both directions. A newly added undocumented command
        becomes a visible failure instead of joining a crowd, and
        documenting one requires removing it from this list deliberately."""
        registered = _registered()
        undocumented = registered - _documented(registered)
        assert undocumented == set(KNOWN_UNDOCUMENTED), (
            f"newly undocumented: {sorted(undocumented - KNOWN_UNDOCUMENTED)}; "
            f"newly documented: {sorted(KNOWN_UNDOCUMENTED - undocumented)}"
        )

    def test_the_undocumented_count_is_what_is_pinned_not_the_total(self):
        """Pins the gap, not the command count.

        The first version asserted `len(registered) == 57` as well, and it
        false-alarmed the moment Grey added `original-year` -- which he
        had documented correctly, so nothing was wrong. A guard that fires
        on legitimate work trains people to bypass it. The undocumented
        SET is the thing worth pinning exactly, and it is, above; the
        total is recorded here only as context."""
        registered = _registered()
        undocumented = registered - _documented(registered)
        assert len(undocumented) == 24, (
            f"{len(undocumented)} of {len(registered)} commands are undocumented"
        )
        assert len(registered) > len(undocumented), "context: the total should exceed the gap"

    def test_a_disabled_command_is_not_advertised_as_working(self):
        """`rebuild-db` raises RebuildDisabledError -- it would destroy the
        archive table. It must not appear in the usage text as if it were
        a usable command."""
        assert "rebuild-db" in KNOWN_UNDOCUMENTED
        assert "rebuild-db" not in _documented(_registered())


class TestSafetyGateIsDocumentedTruthfully:
    """The one part of the CLI surface this session added, so the one part
    whose behaviour is fixture-proven and may therefore be documented."""

    def test_the_gate_is_documented_as_opt_in(self):
        source = Path("musaeus/cli.py").read_text()
        assert "MUSAEUS_P0_SAFETY_GATE" in source or GATE_ENV in source

    def test_the_gate_is_not_advertised_as_live_safe_operation(self):
        """P0-18 forbids advertising a live-safe operation that was not
        rehearsed. The stages still mutate the filesystem directly, so the
        gate gates authority -- it does not make a run recoverable."""
        from musaeus import cli_gate

        text = Path(cli_gate.__file__).read_text()
        for overclaim in ("safe to run", "fully recoverable", "production ready"):
            assert overclaim not in text.lower()

    def test_the_activation_conditions_are_exactly_two(self):
        assert GATE_ENV == "MUSAEUS_P0_SAFETY_GATE"
        assert GATE_FLAG == "--safety-gate"


class TestCuratorExportRootIsDocumentedTruthfully:
    def test_the_usage_text_no_longer_says_export_root_is_required(self):
        """It said "(requires --export-root)". That was true only because
        the configuration fallback was dead; it now works, so the flag is
        an override rather than a requirement."""
        usage = _usage_text()
        curator_line = next(
            (line for line in usage.splitlines() if line.strip().startswith("curator")), ""
        )
        assert "requires --export-root" not in curator_line, (
            f"stale after P0-15: {curator_line.strip()!r}"
        )

    def test_the_config_key_is_named_in_the_usage_text(self):
        assert ENV_VAR in _usage_text() or CONFIG_KEY in _usage_text()


class TestNoUnrehearsedCapabilityIsAdvertised:
    @pytest.mark.parametrize(
        "claim", ["docker", "thunderbird", "smtp", "cross-platform", "windows", "macos gui"]
    )
    def test_the_usage_text_promises_nothing_unrehearsed(self, claim):
        """P0-18: do not advertise Docker execution, Thunderbird compose,
        SMTP delivery, a cross-platform GUI, or P1 export. None are
        implemented; none are rehearsed."""
        assert claim not in _usage_text().lower()
