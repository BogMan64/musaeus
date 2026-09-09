"""A noise bed goes UNDER the whole library, so existence is not a good enough gate.

O-01/O-02 in the Repair Register. `copy_noise_tracks` selected beds with
`sorted(noise_src.glob("*.m4a")) if noise_src.exists() else []` — every file in
the directory shipped on the strength of its filename. A bed is not one track
among ten thousand; it is mixed under all of them, so a bad one is the only
defect in this build that damages the entire edition at once.

The generator already knew what "good" meant. It grew `_is_good_track` after
Pink_Noise_60min.m4a sat at 3,064 s of an intended 3,600 — looking finished, and
being skipped for ever (measured 2026-09-01). The consumer never learned.

The fixture below reproduces that exact shape, because it is the one that
matters and the one a careless test misses: **a file whose header still declares
the full duration while the audio is cut short**. MP4 keeps both the container
and stream durations in the `moov` atom, written before the audio, so metadata
cannot see truncation at all. Only a decode can. A test that truncates a file
crudely destroys the header too, gets refused by the probe, and proves nothing
about the decode check it meant to exercise.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]
                       / "scripts" / "car_library" / "vendor"))
from build_aac_library import _noise_bed_is_shippable  # noqa: E402

pytestmark = pytest.mark.skipif(
    not (shutil.which("ffmpeg") and shutil.which("ffprobe")),
    reason="ffmpeg/ffprobe not available",
)


@pytest.fixture(scope="module")
def complete_bed(tmp_path_factory) -> Path:
    """A short but genuinely complete bed, `moov` first so it survives a cut."""
    out = tmp_path_factory.mktemp("noise") / "bed.m4a"
    subprocess.run(
        ["ffmpeg", "-v", "error", "-y", "-f", "lavfi",
         "-i", "anoisesrc=d=5:c=pink:r=44100",
         "-c:a", "aac", "-b:a", "256k", "-movflags", "+faststart", str(out)],
        check=True, capture_output=True,
    )
    return out


@pytest.fixture(scope="module")
def truncated_bed(complete_bed: Path) -> Path:
    """The Pink_Noise_60min shape: header intact, audio cut short."""
    cut = complete_bed.with_name("bed_cut.m4a")
    data = complete_bed.read_bytes()
    cut.write_bytes(data[: int(len(data) * 0.7)])
    return cut


class TestTheFixtureIsTheRightShape:
    """If the fixture is not the incident's shape, the tests below prove nothing."""

    def test_the_truncated_bed_still_probes_as_complete(self, truncated_bed, complete_bed):
        """This is the whole reason a decode is required.

        Both files report the same duration and the same sample rate. Every
        metadata-only check passes the damaged one.
        """

        def probe(p: Path, entry: str, stream: bool) -> str:
            cmd = ["ffprobe", "-v", "error"]
            if stream:
                cmd += ["-select_streams", "a:0", "-show_entries", f"stream={entry}"]
            else:
                cmd += ["-show_entries", f"format={entry}"]
            cmd += ["-of", "csv=p=0", str(p)]
            return subprocess.run(cmd, capture_output=True, text=True).stdout.strip()

        assert probe(truncated_bed, "sample_rate", True) == probe(complete_bed, "sample_rate", True)
        assert probe(truncated_bed, "duration", False) == probe(complete_bed, "duration", False)
        assert truncated_bed.stat().st_size < complete_bed.stat().st_size


class TestTheGate:
    def test_a_complete_bed_is_shippable(self, complete_bed):
        ok, why = _noise_bed_is_shippable(complete_bed)
        assert ok, why

    def test_a_truncated_bed_is_refused(self, truncated_bed):
        """The one the old `.exists()` gate shipped."""
        ok, why = _noise_bed_is_shippable(truncated_bed)
        assert not ok
        assert "decode" in why

    def test_an_unprobeable_file_is_refused_not_shipped_unpinned(self, tmp_path):
        """M-12's rule, applied here.

        A None rate reaches car_sample_rate, which returns None ("do not
        guess"), which used to take the raw-copy branch — so the probe failing
        silently removed the very sample-rate cap its failure should have
        triggered. Refusing is the only safe reading of "I cannot tell".
        """
        bogus = tmp_path / "not_audio.m4a"
        bogus.write_text("this is not an MP4")

        ok, why = _noise_bed_is_shippable(bogus)
        assert not ok
        assert "sample rate" in why

    def test_a_refusal_always_says_why(self, tmp_path, truncated_bed):
        """A bed silently dropped is as bad as a bad bed silently shipped:
        both end with the operator believing the edition holds what it does not."""
        bogus = tmp_path / "x.m4a"
        bogus.write_text("nope")

        for path in (bogus, truncated_bed):
            ok, why = _noise_bed_is_shippable(path)
            assert not ok
            assert why and why.strip(), f"{path.name} was refused with no reason"


class TestItReusesRatherThanReimplements:
    def test_the_decode_check_is_the_generators_own(self):
        """CLAUDE.md's first rule. The generator grew this check for exactly
        this failure; a second copy here would be the fourth implementation of
        "did this actually decode" in the tree."""
        import build_aac_library
        import orpheus_noise_generator

        assert build_aac_library._decodes_cleanly is orpheus_noise_generator._decodes_cleanly

    def test_the_consumer_no_longer_ships_on_existence_alone(self):
        """Structural: the gate must be called on the way in.

        Guards the regression directly — someone restoring the old one-line
        glob would pass every test above, since those call the helper by hand.
        """
        src = (Path(__file__).resolve().parents[1] / "scripts" / "car_library"
               / "vendor" / "build_aac_library.py").read_text()
        body = src.split("def copy_noise_tracks(")[1].split("\ndef ")[0]

        assert "_noise_bed_is_shippable" in body
