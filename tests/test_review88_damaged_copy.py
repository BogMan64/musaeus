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


# ── Review of #127 (2026-10-08): a save stopped after mutagen moved the audio
# but before it fixed the offsets that point into it. The copy still parses
# and keeps its marker, so it was trusted -- and its audio is broken. Car and
# iPhone copies keep moov before mdat, so a growing tag block moves the audio.


def _moov_first(cfg, master: Path, rel: Path, h: str) -> None:
    """Rewrite the built copy with moov before mdat, as the car copies are."""
    copy = cfg.alac_library / rel
    tmp = copy.with_name("faststart.m4a")
    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", str(copy),
                    "-c", "copy", "-movflags", "+faststart", str(tmp)], check=True)  # fmt: skip
    edition_bake.copy_tags(master, tmp, eb.marker_for(h))
    tmp.replace(copy)


def test_a_retag_stopped_mid_save_is_made_again_though_it_still_reads(cfg, monkeypatch):
    from mutagen.mp4 import MP4, MP4Tags

    from musaeus.duration import decodes_cleanly

    master = _master(cfg, REL, "h1")
    _build(cfg)
    _moov_first(cfg, master, REL, "h1")
    copy = cfg.alac_library / REL
    assert decodes_cleanly(copy)[0]
    f = MP4(master)
    f.tags["\xa9lyr"] = ["la " * 20000]  # the copy's tag block must grow
    f.save()

    def stopped(*args, **kwargs):
        raise KeyboardInterrupt  # the build is killed inside the save

    with monkeypatch.context() as mp:
        mp.setattr(MP4Tags, "_MP4Tags__update_offsets", stopped)
        with pytest.raises(KeyboardInterrupt):
            _build(cfg)
    assert edition_bake.read_marker(copy) == eb.marker_for("h1"), "still reads as the copy"
    assert not decodes_cleanly(copy)[0], "the setup must leave the audio broken"

    plan, out, recorded = _build(cfg)
    assert out.baked == 1 and out.retagged == 0 and not out.failed, (out, out.failed)
    assert decodes_cleanly(copy)[0]
    _whole_copy_of(cfg, REL, "h1")
    assert recorded["h1"].mode != eb.RETAGGING


def test_a_stopped_retag_whose_master_moved_since_is_made_at_its_new_place(cfg):
    master = _master(cfg, REL, "h1")
    _build(cfg)
    ledger = open_ledger(ledger_path(cfg))
    ledger.execute("UPDATE edition_copies SET mode = ?, master_mtime_ns = 0", (eb.RETAGGING,))
    ledger.commit()
    ledger.close()
    new_rel = Path("Rock") / "Rolling Stones" / "Sticky Fingers" / REL.name
    new_master = cfg.alac_archive / new_rel
    new_master.parent.mkdir(parents=True, exist_ok=True)
    master.rename(new_master)
    _sql(cfg, "UPDATE archive SET file_path = ?", str(new_master))
    plan, out, recorded = _build(cfg)
    assert plan.unfinished == 1 and not plan.move, plan.move
    assert any(
        "Stopped retag" in line and line.endswith(": 1") for line in eb.plan_lines(plan, workers=2)
    )
    assert out.removed == 1 and out.baked == 1 and not out.failed, out.failed
    assert not (cfg.alac_library / REL).exists()
    _whole_copy_of(cfg, new_rel, "h1")
    assert recorded["h1"].output_path == str(cfg.alac_library / new_rel)


def test_a_rebake_does_not_replace_a_damaged_copy_changed_since_the_plan(cfg):
    _master(cfg, REL, "h1")
    _build(cfg)
    ledger = open_ledger(ledger_path(cfg))
    ledger.execute("UPDATE edition_copies SET mode = 'dynamic'")
    ledger.commit()
    target = cfg.alac_library / REL
    _cut_short(target)
    conn = sqlite3.connect(f"file:{cfg.db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        plan = eb.make_plan(
            conn, ledger, cfg.alac_archive, cfg.alac_library, rebake_compressed=True
        )
        with open(target, "ab") as fh:
            fh.write(b"written after the plan")  # still damaged, but not the same file
        out = eb.execute(plan, ledger, cfg.alac_library, progress=lambda s: None)
    finally:
        conn.close()
        ledger.close()
    assert out.baked == 0 and len(out.failed) == 1, out.failed
    assert target.read_bytes().endswith(b"written after the plan")
    assert not list(target.parent.glob(f"*{eb.TMP_SUFFIX}"))


@pytest.mark.parametrize("form", ["plain OSError", "wrapped in mutagen's MP4 error"])
def test_an_io_error_while_reading_is_not_damage(tmp_path, monkeypatch, form):
    """mutagen 1.46 lets a read error out as OSError; newer releases may wrap
    it in mp4.error, the error a real parse failure raises (review of #127)."""
    import mutagen.mp4

    path = tmp_path / "a.m4a"
    _plain_m4a(path)
    _cut_short(path)
    assert edition_bake.is_damaged(path)

    def eio(*args, **kwargs):
        try:
            raise OSError(5, "Input/output error")
        except OSError as err:
            if form == "plain OSError":
                raise
            raise mutagen.mp4.error(err)  # noqa: B904 -- as mutagen's reraise does

    monkeypatch.setattr(mutagen.mp4, "Atoms", eio)
    assert not edition_bake.is_damaged(path)


def test_a_damaged_copy_removed_during_the_bake_is_made_and_no_tmp_left(cfg, monkeypatch):
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
        assert str(target) in plan.damaged
        real = eb._signature

        def removed_then_stat(p):
            Path(p).unlink(missing_ok=True)  # removed between the check and the stat
            return real(p)

        monkeypatch.setattr(eb, "_signature", removed_then_stat)
        out = eb.execute(plan, ledger, cfg.alac_library, progress=lambda s: None)
    finally:
        conn.close()
        ledger.close()
    assert out.baked == 1 and not out.failed, out.failed
    _whole_copy_of(cfg, REL, "h1")
    assert not list(target.parent.glob(f"*{eb.TMP_SUFFIX}"))
