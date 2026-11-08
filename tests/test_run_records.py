"""run_records: every run keeps its own log; a run that adds tracks leaves a copy
of its records beside the library; ten of each kind are kept (Grey, 2026-09-24)."""

from __future__ import annotations

import logging
import os
import sys
from pathlib import Path
from types import SimpleNamespace

from musaeus import run_records as rr


def _touch(p: Path, age: int, text: str = "x") -> Path:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text)
    t = 1_800_000_000 - age * 60
    os.utime(p, (t, t))
    return p


def test_the_run_log_captures_print_and_logging_and_restores_the_streams(tmp_path):
    before = sys.stdout, sys.stderr
    log = rr.RunLog(tmp_path / "RUNS", "run_20260924T150000Z_abc123")
    print("stage summary line")
    print("an error line", file=sys.stderr)
    logging.getLogger("musaeus.test").warning("a logged warning")
    log.close()
    log.close()  # idempotent: the run closes it, then atexit does
    text = log.path.read_text()
    assert log.path.name == "run_run_20260924T150000Z_abc123.log"
    assert "stage summary line" in text and "an error line" in text and "a logged warning" in text
    assert (sys.stdout, sys.stderr) == before


def test_only_finalize_placing_files_counts_as_adding_to_the_library():
    R = SimpleNamespace
    assert (
        rr.added_to_library(
            [R(stage_name="ingest", files_changed=50), R(stage_name="finalize", files_changed=0)]
        )
        == 0
    )
    assert rr.added_to_library([R(stage_name="finalize", files_changed=7, dry_run=False)]) == 7
    assert rr.added_to_library([R(stage_name="finalize", files_changed=7, dry_run=True)]) == 0


def test_publish_copies_this_runs_records_beside_the_library_not_inside_it(tmp_path):
    runs, libs = tmp_path / "RUNS", tmp_path / "Libraries"
    rid = "run_20260924T150000Z_abc123"
    log = _touch(runs / "LOGS" / f"run_{rid}.log", 0)
    main = _touch(runs / "HANDOFFS" / f"ForClaudeHandoff_{rid}.md", 0)
    act = _touch(runs / "HANDOFFS" / f"ForClaudeHandoff_{rid}_act2.md", 0)
    fail = _touch(runs / "FAILURES" / f"finalize_{rid}_x.json", 0)
    other = _touch(runs / "HANDOFFS" / "ForClaudeHandoff_run_20260923T010101Z_ffffff.md", 5)
    dest = rr.publish(libs, runs, rid, [log, main, None])
    assert dest == libs / "ALAC_Library_Run_Logs" / rid
    assert {p.name for p in dest.iterdir()} == {log.name, main.name, act.name, fail.name}
    assert not (dest / other.name).exists()
    assert not (libs / "ALAC_Library").exists(), "nothing may be written inside the library"


def test_prune_keeps_the_newest_ten_runs_counting_a_runs_reports_as_one(tmp_path):
    h = tmp_path / "HANDOFFS"
    for i in range(12):
        rid = f"run_202609{i + 10:02d}T000000Z_{i:06x}"
        _touch(h / f"ForClaudeHandoff_{rid}.md", 12 - i)
        _touch(h / f"ForClaudeHandoff_{rid}_act1.md", 12 - i)
    gone = rr.prune(h, 10, group_by_run=True)
    assert len(gone) == 4, "the two oldest runs go, both files of each"
    assert len(list(h.iterdir())) == 20
    assert not any("20260910" in p.name or "20260911" in p.name for p in h.iterdir())


def test_recovery_keeps_ten_of_each_kind(tmp_path):
    r = tmp_path / "recovery"
    for i in range(12):
        (r / f"finalize_run_202609{i + 10:02d}T000000Z_{i:06x}").mkdir(parents=True)
        os.utime(
            r / f"finalize_run_202609{i + 10:02d}T000000Z_{i:06x}",
            (1_800_000_000 + i, 1_800_000_000 + i),
        )
    for i in range(3):
        (r / f"canonicalize_run_2026091{i}T000000Z_{i:06x}").mkdir()
    rr.prune(r, 10, prefix_kinds=True)
    names = [p.name for p in r.iterdir()]
    assert sum(n.startswith("finalize_") for n in names) == 10
    assert sum(n.startswith("canonicalize_") for n in names) == 3, (
        "a small kind is not thinned to make room"
    )


def test_backups_are_thinned_per_file_and_a_live_rulings_file_is_never_touched(tmp_path):
    m = tmp_path / "MetaData"
    live = _touch(m / "MasterLaw.csv", 99)
    canon = _touch(m / "artist_canon.tsv", 99)
    for i in range(13):
        _touch(m / f"MasterLaw.csv.bak-2026091{i:02d}", i)
    for i in range(2):
        _touch(m / f"artist_canon.tsv.bak.{i}", i)
    rr.prune_backups(m, 10)
    assert live.exists() and canon.exists()
    assert len(list(m.glob("MasterLaw.csv.bak*"))) == 10
    assert len(list(m.glob("artist_canon.tsv.bak*"))) == 2
