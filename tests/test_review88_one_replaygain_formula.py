"""Review of #88, finding 14 (2026-10-07): the ReplayGain-from-R128 relation
was written out twice, in forge.py and scripts/write_master_loudness_tags.py.
It is the relation behind the #73 bug (both tags written from the -23 gain, every
master 5 dB too quiet): two copies of it is the codebase's own defect shape. One
helper now, loudness.replaygain_from_r128.
"""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_the_relation_is_written_out_in_one_place_only():
    copies = [
        str(p.relative_to(ROOT))
        for p in [*ROOT.glob("musaeus/**/*.py"), *ROOT.glob("scripts/**/*.py")]
        if p.name != "loudness.py"
        and "R128_REFERENCE - R128_APPLE_REFERENCE" in p.read_text(encoding="utf-8")
    ]
    assert copies == [], f"the ReplayGain relation is written out again in {copies}"


def test_replaygain_is_r128_plus_five():
    from musaeus.loudness import replaygain_from_r128

    assert replaygain_from_r128(-5.0) == 0.0
    assert replaygain_from_r128(3.25) == 8.25
