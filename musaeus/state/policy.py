"""
MUSAEUS — recovery policy values (P0-06)

Policy-only constants. Nothing in this module creates, opens, probes,
stats, or otherwise touches the future recovery root. It is recorded as
a *value* so preflight (P0-11) and the recovery primitives (P0-12) can
report it, compare against it, and refuse to exceed it -- all while
operating exclusively on disposable fixture roots.

The future root is a per-user policy marker, not a configured recovery
target. Recording it does not create or probe the directory or grant live
authority. The capacity cap is 100 GB.

The migrator compares the path as text and refuses it before probing the
filesystem. The policy only declares the value.
"""

from __future__ import annotations

from pathlib import Path
from typing import Final

# The fixed future recovery root. A string, deliberately NOT a Path --
# Path itself is inert, but keeping it as a string makes it visibly a
# recorded policy value rather than something a caller can casually
# .mkdir() or .exists() on by reflex.
# Grey's ruling (2026-10-21): /home/grey/Projects/MUSAEUS_RECOVERY, as first
# recorded. Under each user's home, so the Debian package works for others.
FUTURE_RECOVERY_ROOT: Final[str] = str(Path.home() / "Projects" / "MUSAEUS_RECOVERY")

# The fixed capacity cap: exactly 100 GB.
#
# Decimal GB (10**9), not GiB (2**30). The spec says "100 GB" and the
# figure is a policy ceiling quoted to an operator, not a block-device
# measurement -- 100 GiB would silently be 7.4% more headroom than the
# number Grey approved, in the permissive direction. Where a check is
# about "may I use this much", rounding up is the wrong way to be wrong.
RECOVERY_CAP_BYTES: Final[int] = 100 * 10**9

# Human-facing rendering of the cap, so reports and error messages do not
# each re-derive it (and disagree about GB vs GiB while doing so).
RECOVERY_CAP_LABEL: Final[str] = "100 GB"


def describe_recovery_policy() -> dict[str, object]:
    """
    Return the recovery policy as a plain, reportable mapping.

    Used by preflight/report code that must state the fixed root and cap
    without touching them. Returns a new dict each call so a caller
    cannot mutate the module's declared policy in place.
    """
    return {
        "future_recovery_root": FUTURE_RECOVERY_ROOT,
        "recovery_cap_bytes": RECOVERY_CAP_BYTES,
        "recovery_cap_label": RECOVERY_CAP_LABEL,
        "probed": False,
    }
