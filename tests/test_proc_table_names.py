"""The build's process scan reads /proc/<pid>/stat (CodeRabbit on #299).

The kernel cuts a long process name at 15 bytes, which can split a UTF-8
character. A strict read raised UnicodeDecodeError, which is not an OSError,
so one such process stopped the whole scan and the edition build with it.
"""

from __future__ import annotations

from pathlib import Path

from musaeus import edition_build


def test_a_name_cut_mid_character_does_not_stop_the_scan(tmp_path, monkeypatch):
    proc = tmp_path / "proc"
    (proc / "123").mkdir(parents=True)
    (proc / "123" / "stat").write_bytes(
        b"123 (caf\xc3) S 1 123 123 0 -1\n"
    )  # cut inside "\xc3\xa9"
    (proc / "123" / "cmdline").write_bytes(b"python3\0-m\0musaeus\0run\0")
    real_path = Path
    monkeypatch.setattr(edition_build, "Path", lambda p: proc if p == "/proc" else real_path(p))

    (row,) = list(edition_build._proc_table())

    assert row[0] == 123 and row[1] == 1
    assert row[2].startswith("caf")
    assert row[3] == ["python3", "-m", "musaeus", "run"]
