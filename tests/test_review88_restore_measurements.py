"""Review of #88, finding 12 (2026-10-07): master_measurements.restore_ledger,
which puts a master's kept loudness measurements back into editions.db, had no
caller. Lose the ledger and every master is measured again, hours of work, with
the numbers sitting in each master's own tag. A build now restores a missing
measurement from the master before baking it.
"""

from __future__ import annotations

import shutil
import subprocess

import pytest
from mutagen.mp4 import MP4

from musaeus import edition_build as eb
from musaeus import edition_ledger as el
from musaeus import master_measurements as mm

pytestmark = pytest.mark.skipif(not shutil.which("ffmpeg"), reason="requires ffmpeg")
MEASURED = {"input_i": -21.5, "input_tp": -1.2, "input_lra": 6.0, "input_thresh": -32.0}


def test_a_build_uses_the_measurement_the_master_keeps(tmp_path, monkeypatch):
    master = tmp_path / "ALAC-Archival" / "A" / "song.m4a"
    master.parent.mkdir(parents=True)
    subprocess.run(["ffmpeg", "-nostdin", "-y", "-v", "error", "-f", "lavfi", "-i", "sine=d=1",
                    "-c:a", "alac", str(master)], check=True)  # fmt: skip
    kind = eb.KINDS["car"]
    recipe = f"{kind.recipe_family}-test"
    audio = MP4(str(master))
    if audio.tags is None:
        audio.add_tags()
    mm.put(audio.tags, {recipe: MEASURED})
    audio.save()
    ledger = el.open_ledger(tmp_path / el.LEDGER_FILENAME)  # a fresh ledger: nothing kept
    m = eb.Master(path=master, audio_hash="h" * 64, codec="alac", lufs=None, lufs_tp=None,
                  size_bytes=master.stat().st_size, mtime_ns=master.stat().st_mtime_ns)  # fmt: skip
    plan = eb.Plan(kind_name="car", target_lufs=-14.0)
    plan.bake.append((m, tmp_path / "CAR_Library" / "A" / "song.m4a"))
    seen = []

    def bake_one(m, target, kind, known):
        seen.append(dict(known))
        raise RuntimeError("stop after the lookup")

    monkeypatch.setattr(eb, "_bake_one", bake_one)
    eb.execute(plan, ledger, tmp_path / "CAR_Library", kind=kind, progress=lambda s: None)

    assert seen and seen[0].get(recipe) == MEASURED, "the master's kept measurement was not used"
