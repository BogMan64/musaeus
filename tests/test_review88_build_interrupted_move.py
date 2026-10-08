"""Review of #88, finding 11 (2026-10-07): a build's move phase first steps each
moving copy aside to <name>.<hash12>.edition_tmp. Ctrl-C between the two steps
left finished copies under that name, and the next build deleted every
*.edition_tmp as a half-made encode: hours of baking again, and a stick sync in
between dropped those songs. A step-aside copy the ledger still records, whose
marker says it is that copy, now goes back to its place.
"""

from __future__ import annotations

from pathlib import Path

from musaeus import edition_build as eb
from musaeus import edition_ledger as el

H = "abcdef0123456789" * 4


def _ledger_with_copy(tmp_path: Path, output: Path):
    ledger = el.open_ledger(tmp_path / el.LEDGER_FILENAME)
    el.record(ledger, el.Copy(edition="car", master_hash=H, master_path="/m/a.m4a",
                              master_mtime_ns=1, output_path=str(output), built_at="2026-10-01",
                              achieved_lufs=-14.0, mode="linear"))  # fmt: skip
    return ledger


def test_a_copy_stepped_aside_by_a_killed_move_goes_back(tmp_path, monkeypatch):
    root = tmp_path / "CAR_Library"
    old = root / "A" / "a.m4a"
    old.parent.mkdir(parents=True)
    aside = old.with_name(f"{old.name}.{H[:12]}{eb.TMP_SUFFIX}")
    aside.write_bytes(b"the finished copy")
    ledger = _ledger_with_copy(tmp_path, old)
    kind = eb.KINDS["car"]
    monkeypatch.setattr(eb.edition_bake, "read_marker", lambda p: eb.marker_for(H, kind))

    eb.execute(eb.Plan(kind_name="car", target_lufs=-14.0), ledger, root, kind=kind)

    assert old.read_bytes() == b"the finished copy", "a finished copy was deleted as a temp file"
    assert not aside.exists()


def test_a_half_made_encode_is_still_cleared(tmp_path, monkeypatch):
    root = tmp_path / "CAR_Library"
    root.mkdir()
    half = root / f"b.m4a{eb.TMP_SUFFIX}"
    half.write_bytes(b"half an encode")
    ledger = el.open_ledger(tmp_path / el.LEDGER_FILENAME)

    eb.execute(eb.Plan(kind_name="car", target_lufs=-14.0), ledger, root, kind=eb.KINDS["car"])

    assert not half.exists()
