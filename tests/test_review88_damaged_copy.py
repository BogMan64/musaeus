"""Review of #88, finding 4 (2026-10-07): a build's retag, adopt and move save
tags into the finished copy in place. A kill part way through a save left a
copy that no longer reads as an MP4, and every later build blocked on it ("a
file with no record is in the way") or failed it ("something appeared"). A
damaged copy where a copy belongs is now made again, and the verified new copy
replaces it in one rename. Anything that is not a damaged MP4 is left alone.
"""

from __future__ import annotations

import os
import shutil
import sqlite3
import subprocess
from pathlib import Path

import pytest

from musaeus import edition_bake
from musaeus import edition_build as eb
from musaeus.config import MusicConfig
from musaeus.edition_ledger import ledger_path, open_ledger
from tests.test_lossless_edition import REL, _build, _master, _sql

pytestmark = pytest.mark.skipif(
    not (shutil.which("ffmpeg") and shutil.which("ffprobe")),
    reason="ffmpeg/ffprobe not available",
)


@pytest.fixture
def cfg(tmp_path: Path) -> MusicConfig:
    c = MusicConfig(
        vault_root=tmp_path,
        inbox=tmp_path / "INBOX",
        staging=tmp_path / "STAGING",
        quarantine=tmp_path / "QUARANTINE",
        runs_root=tmp_path / "RUNS",
        meta_dir=tmp_path / "MetaData",
        alac_library=tmp_path / "Libraries" / "ALAC_Library",
        db_path=tmp_path / "musaeus.db",
    )
    c.ensure_dirs()
    return c


def _cut_short(path: Path) -> None:
    """What a tag save killed part way leaves: the MP4's start, then nothing."""
    with open(path, "r+b") as fh:
        fh.truncate(100)


def _whole_copy_of(cfg, rel: Path, h: str) -> None:
    copy = cfg.alac_library / rel
    assert edition_bake.read_marker(copy) == eb.marker_for(h)
    assert not edition_bake.is_damaged(copy)


def test_a_retag_cut_short_is_made_again(cfg):
    from mutagen.mp4 import MP4

    master = _master(cfg, REL, "h1")
    _build(cfg)
    f = MP4(master)
    f.tags["\xa9nam"] = ["Brown Sugar (2009 Remaster)"]
    f.save()
    _cut_short(cfg.alac_library / REL)  # the next build's retag was killed
    plan, out, recorded = _build(cfg)
    assert not plan.blocked, plan.blocked
    assert out.baked == 1 and not out.failed, out.failed
    _whole_copy_of(cfg, REL, "h1")
    assert MP4(cfg.alac_library / REL).tags["\xa9nam"] == ["Brown Sugar (2009 Remaster)"]
    assert recorded["h1"].output_path == str(cfg.alac_library / REL)


def test_a_move_cut_short_is_made_again(cfg):
    master = _master(cfg, REL, "h1")
    _build(cfg)
    new_rel = Path("Rock") / "Rolling Stones" / "Sticky Fingers" / REL.name
    new_master = cfg.alac_archive / new_rel
    new_master.parent.mkdir(parents=True, exist_ok=True)
    master.rename(new_master)
    _sql(cfg, "UPDATE archive SET file_path = ?", str(new_master))
    # The move renamed the copy into its new place, then was killed tagging it.
    new_copy = cfg.alac_library / new_rel
    new_copy.parent.mkdir(parents=True, exist_ok=True)
    (cfg.alac_library / REL).rename(new_copy)
    _cut_short(new_copy)
    plan, out, recorded = _build(cfg)
    assert not plan.blocked, plan.blocked
    assert out.baked == 1 and not out.failed, out.failed
    _whole_copy_of(cfg, new_rel, "h1")
    assert recorded["h1"].output_path == str(new_copy)


def test_an_adopt_cut_short_is_made_again(cfg):
    master = _master(cfg, REL, "h1")
    target = cfg.alac_library / REL
    target.parent.mkdir(parents=True, exist_ok=True)
    edition_bake.bake(master, target)
    edition_bake.copy_tags(master, target, eb.marker_for("h1"))  # landed, not recorded
    _cut_short(target)  # the next build's adopt was killed tagging it
    plan, out, recorded = _build(cfg)
    assert not plan.blocked, plan.blocked
    assert out.baked == 1 and not out.failed, out.failed
    _whole_copy_of(cfg, REL, "h1")
    assert recorded["h1"].output_path == str(target)


def test_a_rebake_over_a_damaged_copy_replaces_it(cfg):
    """A copy baked again in place (here: --rebake-compressed) whose file a
    killed retag damaged: the new copy replaces it rather than failing as
    "something appeared"."""
    _master(cfg, REL, "h1")
    _build(cfg)
    ledger = open_ledger(ledger_path(cfg))
    ledger.execute("UPDATE edition_copies SET mode = 'dynamic'")
    ledger.commit()
    ledger.close()
    _cut_short(cfg.alac_library / REL)
    plan, out, _ = _build(cfg, rebake_compressed=True)
    assert out.baked == 1 and not out.failed, out.failed
    _whole_copy_of(cfg, REL, "h1")


def _plain_m4a(path: Path) -> None:
    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi",
                    "-i", "sine=frequency=440:duration=1", "-c:a", "alac", str(path)],
                   check=True)  # fmt: skip


@pytest.mark.parametrize("what", ["not an MP4", "a whole MP4 with no marker", "cannot be opened"])
def test_anything_but_a_damaged_mp4_still_blocks(cfg, what):
    _master(cfg, REL, "h1")
    target = cfg.alac_library / REL
    target.parent.mkdir(parents=True, exist_ok=True)
    if what == "not an MP4":
        target.write_bytes(b"someone else's file")
    else:
        _plain_m4a(target)
    if what == "cannot be opened":
        _cut_short(target)
        target.chmod(0)
    before = os.stat(target)
    try:
        plan, out, recorded = _build(cfg)
    finally:
        target.chmod(0o644)
    assert [why for _, why in plan.blocked] == [f"a file with no record is in the way: {target}"]
    assert out.baked == 0 and "h1" not in recorded
    after = os.stat(target)
    assert (after.st_size, after.st_mtime_ns) == (before.st_size, before.st_mtime_ns)


def test_a_damaged_copy_changed_after_planning_is_left_alone(cfg):
    """Planned as damaged, then replaced by something else before the bake
    landed: that something is not overwritten."""
    master = _master(cfg, REL, "h1")
    target = cfg.alac_library / REL
    target.parent.mkdir(parents=True, exist_ok=True)
    edition_bake.bake(master, target)
    _cut_short(target)
    conn = sqlite3.connect(f"file:{cfg.db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    ledger = open_ledger(ledger_path(cfg))
    try:
        plan = eb.make_plan(conn, ledger, cfg.alac_archive, cfg.alac_library)
        assert [t for _, t in plan.bake] == [target]
        target.write_bytes(b"put here after the plan")
        out = eb.execute(plan, ledger, cfg.alac_library, progress=lambda s: None)
    finally:
        conn.close()
        ledger.close()
    assert out.baked == 0 and len(out.failed) == 1, out.failed
    assert target.read_bytes() == b"put here after the plan"


def test_a_dry_run_says_how_many_are_made_again(cfg):
    master = _master(cfg, REL, "h1")
    target = cfg.alac_library / REL
    target.parent.mkdir(parents=True, exist_ok=True)
    edition_bake.bake(master, target)
    _cut_short(target)
    conn = sqlite3.connect(f"file:{cfg.db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    ledger = open_ledger(ledger_path(cfg))
    try:
        plan = eb.make_plan(conn, ledger, cfg.alac_archive, cfg.alac_library)
    finally:
        conn.close()
        ledger.close()
    assert any("Remake damaged: 1" in line for line in eb.plan_lines(plan, workers=2))
