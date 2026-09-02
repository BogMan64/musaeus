"""Three answers to "how long is this file", and only one can be trusted.

The container header carries a duration and so does the audio stream. In
MP4 -- every file in this library -- both live in the same moov atom, so
both are written before the audio and both survive the audio being cut
off. Only decoding reads past the header.

Measured 2026-09-02 on a 30 s file truncated to a third of its bytes with
the header intact at the front:

    container : 30.0
    stream    : 30.0
    decoded   : 409 AAC frames, about 9.5 s

An earlier draft of duration.py asserted the stream was "the honest one
for truncation". It is not, and the test below is what proved it.

The second trap is worse: that truncated file makes ffmpeg print "Input
buffer exhausted before END element found" and "partial file" on stderr
and EXIT 0. A check reading the return code sees a clean decode.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from musaeus.duration import (
    CONTAINER,
    STREAM,
    container_seconds,
    decodes_cleanly,
    duration_with_source,
    stream_seconds,
)

pytestmark = pytest.mark.skipif(
    not (shutil.which("ffmpeg") and shutil.which("ffprobe")),
    reason="ffmpeg/ffprobe not available",
)


@pytest.fixture
def intact(tmp_path: Path) -> Path:
    """A 30 s AAC file with the header at the FRONT (+faststart).

    faststart matters: with moov at the end, truncation removes the header
    entirely and ffprobe cannot read the file at all -- a different, much
    more obvious failure. Header-at-front is the case that lies.
    """
    p = tmp_path / "intact.m4a"
    subprocess.run(
        ["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "sine=frequency=440:duration=30",
         "-c:a", "aac", "-movflags", "+faststart", str(p), "-y"],
        check=True, capture_output=True,
    )
    return p


@pytest.fixture
def truncated(intact: Path) -> Path:
    p = intact.with_name("truncated.m4a")
    data = intact.read_bytes()
    p.write_bytes(data[: len(data) // 3])
    return p


def test_an_intact_file_agrees_everywhere(intact: Path) -> None:
    assert container_seconds(intact) == pytest.approx(30.0, abs=0.5)
    assert stream_seconds(intact) == pytest.approx(30.0, abs=0.5)
    assert decodes_cleanly(intact)[0]


def test_neither_metadata_source_can_see_truncation(truncated: Path) -> None:
    """The whole reason this module exists. Both read the same moov atom."""
    assert container_seconds(truncated) == pytest.approx(30.0, abs=0.5)
    assert stream_seconds(truncated) == pytest.approx(30.0, abs=0.5)


def test_only_decoding_catches_it(truncated: Path) -> None:
    ok, err = decodes_cleanly(truncated)
    assert not ok
    assert err and ("exhausted" in err.lower() or "partial" in err.lower() or "invalid" in err.lower())


def test_a_clean_decode_reports_no_error(intact: Path) -> None:
    assert decodes_cleanly(intact) == (True, None)


def test_the_exit_code_alone_would_have_passed_it(truncated: Path) -> None:
    """ffmpeg exits 0 on this file. decodes_cleanly must not rely on that.

    This is the assertion that pins the actual bug shape: if someone
    "simplifies" decodes_cleanly to `return r.returncode == 0`, this fails.
    """
    r = subprocess.run(
        ["ffmpeg", "-v", "error", "-nostats", "-i", str(truncated), "-f", "null", "-"],
        capture_output=True, text=True,
    )
    assert r.returncode == 0, "if ffmpeg ever starts failing here, relax this test"
    assert r.stderr.strip(), "the evidence is on stderr, not in the exit code"
    assert not decodes_cleanly(truncated)[0]


def test_duration_with_source_says_which_it_used(intact: Path) -> None:
    secs, src = duration_with_source(intact)
    assert secs == pytest.approx(30.0, abs=0.5)
    assert src in (STREAM, CONTAINER)


def test_unreadable_file_answers_none_rather_than_guessing(tmp_path: Path) -> None:
    missing = tmp_path / "nope.m4a"
    assert container_seconds(missing) is None
    assert stream_seconds(missing) is None
    assert duration_with_source(missing) == (None, None)
    assert decodes_cleanly(missing)[0] is False


# ── One tolerance, not five ───────────────────────────────────────────────────


def test_the_tolerance_has_one_definition() -> None:
    """It was 1.5 in four places and 2.0 in a fifth, whose comment cited
    "the same rationale" as one of the 1.5s. Same stated reasoning,
    different number, and nobody had decided. Grey ruled 2.0, 2026-09-02.
    """
    from musaeus.duration import TOLERANCE_SEC

    assert TOLERANCE_SEC == 2.0


def test_the_package_stages_take_it_from_that_definition() -> None:
    """Not a copied literal -- the same object, so changing it moves them."""
    from musaeus.duration import TOLERANCE_SEC
    from musaeus.stages.auditor import _DEFAULT_TOLERANCE
    from musaeus.stages.canonicalize import _DURATION_TOLERANCE_SEC

    assert _DURATION_TOLERANCE_SEC == TOLERANCE_SEC
    assert _DEFAULT_TOLERANCE == TOLERANCE_SEC


def test_the_standalone_scripts_agree_on_the_value() -> None:
    """They run as subprocesses with their own sys.path, so they carry a
    literal rather than an import. It still has to be the same number."""
    import re
    from pathlib import Path

    root = Path(__file__).resolve().parent.parent
    for rel in (
        "scripts/alac_library/build_alac_library.py",
        "scripts/car_library/vendor/build_aac_library.py",
        "scripts/car_library/vendor/orpheus_noise_generator.py",
        "scripts/car_library/vendor/orpheus_noise_masker.py",
    ):
        src = (root / rel).read_text()
        found = re.findall(r"_DURATION_TOLERANCE_SEC\s*=\s*([0-9.]+)", src)
        assert found, f"{rel} lost its tolerance constant"
        assert float(found[0]) == 2.0, f"{rel} has {found[0]}, expected 2.0"
