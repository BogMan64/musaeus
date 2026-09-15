"""
MUSAEUS — recovery policy values (P0-06)

Policy-only constants. Nothing in this module creates, opens, probes,
stats, or otherwise touches the future recovery root. It is recorded as
a *value* so preflight (P0-11) and the recovery primitives (P0-12) can
report it, compare against it, and refuse to exceed it -- all while
operating exclusively on disposable fixture roots.

The spec is unusually specific about this (MCR-002, DR-05): the root is
exactly `/home/grey/Projects/MUSAEUS_RECOVERY` and the cap is exactly
100 GB. Both are fixed values, not defaults to be negotiated at runtime,
and recording them "does not create or probe the directory or grant live
authority".

`tests/disposable_vault.py` lists the same path in PROTECTED_REAL_ROOTS,
so the session-wide PathGuard raises RealPathAccessError if any test ever
does reach for it. That guard is the enforcement; this module is the
declaration.
"""

from __future__ import annotations

from typing import Final

# The fixed future recovery root. A string, deliberately NOT a Path --
# Path itself is inert, but keeping it as a string makes it visibly a
# recorded policy value rather than something a caller can casually
# .mkdir() or .exists() on by reflex.
FUTURE_RECOVERY_ROOT: Final[str] = "/home/grey/Projects/MUSAEUS_RECOVERY"

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
