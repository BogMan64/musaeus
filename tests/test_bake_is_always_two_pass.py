"""A bake applies MEASURED values. It is never a single loudnorm pass.

Grey's standing rule, 2026-09-21: "it should always be two passes, not a
single pass."

`loudnorm=I=-14` in one pass is a dynamic normaliser, not a bake. It lands
where it lands: a hand repair that way put one car file 1.5 LU hot while its
peers sat at -13.9. The bake measures first (ffmpeg_measure_loudnorm) and
applies the measured_* values with linear=true (build_second_pass_filter).

The builders already do this. What they cannot prevent is someone -- a
future session, or a one-off repair script -- reaching for a command that
LOOKS equivalent. These guards fail if a single-pass filter is ever used to
write an output file.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
BUILDERS = [
    ROOT / "musaeus" / "library_bake.py",
    ROOT / "scripts" / "car_library" / "vendor" / "build_aac_library.py",
]


@pytest.mark.parametrize("path", BUILDERS, ids=lambda p: p.name)
def test_the_builder_measures_before_it_bakes(path: Path) -> None:
    src = path.read_text()
    assert "ffmpeg_measure_loudnorm" in src, "no measurement pass"
    assert "build_second_pass_filter" in src, "no second-pass filter"
    m = re.search(r"def build_second_pass_filter\(.*?\n(?=\n\n|\ndef )", src, re.S)
    assert m, "build_second_pass_filter not found"
    body = m.group(0)
    for field in ("measured_I", "measured_LRA", "measured_TP", "measured_thresh"):
        assert field in body, f"second pass does not apply {field}"
    assert "linear=true" in body, (
        "without linear=true loudnorm falls back to dynamic mode and the "
        "measured values stop being a bake"
    )


@pytest.mark.parametrize("path", BUILDERS, ids=lambda p: p.name)
def test_no_single_pass_loudnorm_writes_an_output(path: Path) -> None:
    """Any loudnorm string with a target but no measured_* must be analysis.

    An analysis pass is identifiable: it prints JSON and writes to null.
    Anything else carrying `I=` without `measured_I=` would be a single-pass
    bake.
    """
    src = path.read_text()
    for lit in re.findall(r'f?"loudnorm=[^"]*"', src):
        if "measured_I" in lit:
            continue  # the real bake
        if "print_format=json" in lit or "print_format=summary" in lit:
            continue  # measurement / reporting
        assert "I=" not in lit, f"single-pass loudnorm with a target in {path.name}: {lit[:80]}"
