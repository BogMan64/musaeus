"""No MUSAEUS_ path a test run could inherit survives conftest.

conftest.py used to clear a hand-kept list of 8 variables while config.py
read 14. The six it missed included MUSAEUS_ALAC_ARCHIVE, the masters tier the
pipeline writes to since 2026-09-25. This checks the real list, read from
config.py, so a new setting cannot slip past the same way.
"""

from __future__ import annotations

import os
import re
from pathlib import Path


def test_no_setting_config_reads_is_inherited_by_the_tests():
    src = (Path(__file__).resolve().parents[1] / "musaeus" / "config.py").read_text(
        encoding="utf-8"
    )
    read = set(re.findall(r'"(MUSAEUS_[A-Z_]+)"', src))
    assert len(read) >= 10, f"found only {sorted(read)} -- is the scan still reading config.py?"
    leaked = sorted(k for k in read if k in os.environ and k != "MUSAEUS_NO_IDLE_THROTTLE")
    assert not leaked, f"these reach the tests from the environment: {leaked}"


def test_the_tests_own_switches_are_not_cleared():
    """Clearing every MUSAEUS_ name also cleared MUSAEUS_WRITE_EVIDENCE and
    MUSAEUS_COVERAGE_SUBPROCESS, the opt-ins REVIEW_BRIEF and pyproject.toml
    document. conftest clears exactly what config.py reads."""
    import sys

    (conftest,) = [
        m
        for m in list(sys.modules.values())
        if str(getattr(m, "__file__", "")).endswith("tests/conftest.py")
    ]
    assert "MUSAEUS_ALAC_ARCHIVE" in conftest._CLEARED_MUSAEUS
    for switch in (
        "MUSAEUS_WRITE_EVIDENCE",
        "MUSAEUS_COVERAGE_SUBPROCESS",
        "MUSAEUS_NO_IDLE_THROTTLE",
    ):
        assert switch not in conftest._CLEARED_MUSAEUS, (
            f"{switch} would be wiped before any test reads it"
        )
