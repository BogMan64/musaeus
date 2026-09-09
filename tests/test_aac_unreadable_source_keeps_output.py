"""An unreadable source must not take its good encode down with it.

M-01 in the Repair Register, 2026-09-08. `convert_one`'s resume check was:

    if _output_matches_source(file_path, output_file):
        return "SKIP DONE ..."
    output_file.unlink()

`_output_matches_source()` returns False for two situations that are not
alike at all:

    the output is wrong        -> deleting it and re-encoding is correct
    the source cannot be read  -> deleting the output destroys the only
                                  playable copy that is left

Only the first justifies destroying anything. The second is precisely when
the car copy matters most: the master is gone or unreadable, and the encode
made from it while it was healthy is the last file that still plays.

It is reachable from an ordinary `--from-catalogue` build, not an edge case
— any row whose master has been deleted, moved, or has rotted since its
encode was made. It had NOT fired when it was found: no CATALOGUED row was
missing its file that day, so the trigger did not exist. A latent defect
whose cost is silent permanent loss is still worth a test, because the
condition that makes it fire is one file deletion away, and MUSAEUS deletes
files on rulings routinely.

The fix keeps `_output_matches_source` exactly as it was — it answers a
narrow question correctly — and stops the CALLER from reading a False as
permission to delete.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts" / "car_library" / "vendor"))
import build_aac_library as bal  # noqa: E402
from build_aac_library import _output_matches_source, _probe_duration  # noqa: E402

needs_ffmpeg = pytest.mark.skipif(
    not (shutil.which("ffmpeg") and shutil.which("ffprobe")),
    reason="requires ffmpeg/ffprobe",
)


def _tone(path: Path, seconds: float) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
         "-f", "lavfi", "-i", f"sine=frequency=440:duration={seconds}",
         "-c:a", "aac", str(path)],
        check=True, capture_output=True)
    return path


# ── the discrimination the fix rests on ──────────────────────────────────────

@needs_ffmpeg
def test_an_unreadable_source_is_indistinguishable_from_a_bad_output(tmp_path: Path) -> None:
    """Why the caller cannot use this helper's False as permission to delete.

    Both cases below return False. Only one of them is a reason to destroy
    anything, and the helper has no way to say which — that is the whole
    defect, pinned here so nobody 'simplifies' the caller back.
    """
    good_out = _tone(tmp_path / "good_out.m4a", 5.0)

    unreadable_src = tmp_path / "unreadable.m4a"
    unreadable_src.write_text("not audio at all")
    assert _output_matches_source(unreadable_src, good_out) is False

    real_src = _tone(tmp_path / "real.m4a", 5.0)
    truncated_out = _tone(tmp_path / "trunc.m4a", 1.0)
    assert _output_matches_source(real_src, truncated_out) is False


@needs_ffmpeg
def test_probe_returns_none_for_an_unreadable_source(tmp_path: Path) -> None:
    """The signal the fix keys on."""
    junk = tmp_path / "junk.m4a"
    junk.write_text("not audio at all")
    assert _probe_duration(junk) is None
    assert _probe_duration(tmp_path / "does_not_exist.m4a") is None
    assert _probe_duration(_tone(tmp_path / "ok.m4a", 2.0)) == pytest.approx(2.0, abs=0.3)


# ── the behaviour itself ─────────────────────────────────────────────────────

def _run_resume_branch(source: Path, output: Path) -> str:
    """Exercise convert_one's resume decision without a real encode.

    convert_one takes a wide signature and would run a two-pass loudnorm
    past this branch, so the branch is reproduced here against the REAL
    helpers. If the source of truth moves, this test must move with it —
    the assertions below are about which of the two outcomes happens, and
    that is what the fix changed.
    """
    if _output_matches_source(source, output):
        return "SKIP DONE"
    if _probe_duration(source) is None:
        raise RuntimeError("source could not be probed")
    output.unlink()
    return "REDO"


@needs_ffmpeg
def test_a_good_encode_survives_an_unreadable_source(tmp_path: Path) -> None:
    """The finding. Before the fix this deleted `output`."""
    source = tmp_path / "master.m4a"
    source.write_text("the master has rotted, or was replaced by junk")
    output = _tone(tmp_path / "car.m4a", 5.0)

    with pytest.raises(RuntimeError):
        _run_resume_branch(source, output)

    assert output.exists(), "the only playable copy was destroyed"


@needs_ffmpeg
def test_a_good_encode_survives_a_source_that_is_gone(tmp_path: Path) -> None:
    """The worse version: the master was deleted, so the encode is all there is."""
    source = tmp_path / "deleted_master.m4a"
    output = _tone(tmp_path / "car.m4a", 5.0)

    with pytest.raises(RuntimeError):
        _run_resume_branch(source, output)

    assert output.exists()


@needs_ffmpeg
def test_a_truncated_output_is_still_deleted_and_redone(tmp_path: Path) -> None:
    """The fix must not turn into 'never delete anything'.

    A readable source with a short output is a real bad encode, and keeping
    it would ship a half-track — the exact failure the resume check was
    written to prevent.
    """
    source = _tone(tmp_path / "master.m4a", 30.0)
    output = _tone(tmp_path / "car.m4a", 2.0)

    assert _run_resume_branch(source, output) == "REDO"
    assert not output.exists()


@needs_ffmpeg
def test_a_matching_output_is_kept_and_skipped(tmp_path: Path) -> None:
    source = _tone(tmp_path / "master.m4a", 10.0)
    output = _tone(tmp_path / "car.m4a", 10.0)

    assert _run_resume_branch(source, output) == "SKIP DONE"
    assert output.exists()


def test_the_guard_is_present_in_the_shipped_code() -> None:
    """The test above reproduces the branch; this asserts the real one has it.

    Without this, the reproduction could pass for ever while the shipped
    caller went back to deleting — which is how a fixed defect comes back.
    """
    src = Path(bal.__file__).read_text()
    branch = src.split("if output_file.exists() and not FORCE_REENCODE:")[1][:1400]
    guard = branch.index("_probe_duration(file_path) is None")
    unlink = branch.index("output_file.unlink()")
    assert guard < unlink, "the source-readability guard must precede the delete"
