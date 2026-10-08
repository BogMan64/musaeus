"""Stick-sync findings of the review of #88 (2026-10-07): 1, 5, 6, 7, 8, 9.

5. Same size counted as unchanged, but a re-tag keeps the size, so artist and
   genre fixes never reached the stick.
7. A copy that failed its hash check was left on the stick, and the check hashed
   the page cache, not the stick.
8. FAT cannot tell "USHER" from "Usher": a case-only difference was deleted and
   copied again on every run (the dry run of 2026-10-07: 181 of them).
6. The vault's old playlists were written over the edition's checked ones.
1. --no-format --dest checked nothing about the destination: pointed at its own
   source it emptied every file and reported them "copied+verified".
9. The transfer took no edition build lock.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import scripts.usb_transfer.transfer_to_usb as usb  # noqa: E402
from musaeus import edition_build  # noqa: E402


def _w(root: Path, rel: str, data: bytes, mtime: float | None = None) -> Path:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(data)
    if mtime is not None:
        os.utime(p, (mtime, mtime))
    return p


@pytest.fixture
def trees(tmp_path):
    src, dst = tmp_path / "CAR_Library", tmp_path / "stick"
    src.mkdir()
    dst.mkdir()
    return src, dst


# ── 5 ───────────────────────────────────────────────────────────────────────


def test_a_same_size_retag_reaches_the_stick(trees):
    src, dst = trees
    _w(dst, "A/a.m4a", b"old tags", mtime=1_700_000_000)  # copied to the stick
    s = _w(src, "A/a.m4a", b"new tags", mtime=1_700_100_000)  # re-tagged since, same size

    plan = usb.plan_sync([s], src, dst)

    assert plan.copy == [s], "a re-tag that kept the size never reached the stick"


def test_a_newer_source_with_the_same_bytes_is_not_copied_again(trees):
    """A re-tag that wrote identical tags changes the time, not the song."""
    src, dst = trees
    _w(dst, "A/a.m4a", b"same bytes", mtime=1_700_000_000)
    s = _w(src, "A/a.m4a", b"same bytes", mtime=1_700_100_000)

    plan = usb.plan_sync([s], src, dst)

    assert plan.copy == [] and plan.current == [s]


def test_an_untouched_song_is_left_alone(trees):
    src, dst = trees
    s = _w(src, "A/a.m4a", b"same", mtime=1_700_000_000)
    _w(dst, "A/a.m4a", b"same", mtime=1_700_100_000)  # copied after its last change

    plan = usb.plan_sync([s], src, dst)

    assert plan.copy == [] and plan.unchanged == 1


# ── 8 ───────────────────────────────────────────────────────────────────────


def test_a_case_only_difference_on_fat_is_not_copied_again(trees, monkeypatch):
    src, dst = trees
    s = _w(src, "Usher/a.m4a", b"same", mtime=1_700_000_000)
    _w(dst, "USHER/a.m4a", b"same", mtime=1_700_100_000)
    monkeypatch.setattr(usb, "_case_insensitive", lambda root: True, raising=False)

    plan = usb.plan_sync([s], src, dst)

    assert plan.delete == [] and plan.copy == [], "deleted and copied again on every run"


# ── 7 ───────────────────────────────────────────────────────────────────────


def test_a_copy_that_fails_its_check_leaves_the_old_file(trees, monkeypatch):
    src, dst = trees
    s = _w(src, "A/a.m4a", b"new content")
    old = _w(dst, "A/a.m4a", b"old content")
    real = usb.file_hash
    monkeypatch.setattr(usb, "file_hash", lambda p: "bad" if Path(p) != s else real(p))

    result = usb.copy_with_verification([s], src, dst, cooldown_seconds=0)

    assert result.failed and not result.ok
    assert old.read_bytes() == b"old content", "a copy that failed its check was left in place"
    assert not list(dst.rglob("*.part"))


def test_the_check_reads_the_stick_not_the_cache(trees, monkeypatch):
    src, dst = trees
    s = _w(src, "A/a.m4a", b"content")
    dropped = []
    real = os.posix_fadvise
    monkeypatch.setattr(
        os, "posix_fadvise", lambda fd, off, n, adv: dropped.append(adv) or real(fd, off, n, adv)
    )

    usb.copy_with_verification([s], src, dst, cooldown_seconds=0)

    assert os.POSIX_FADV_DONTNEED in dropped


# ── 6 ───────────────────────────────────────────────────────────────────────


def test_the_editions_own_playlists_are_not_overwritten(tmp_path):
    vault, src, dst = tmp_path / "VAULT", tmp_path / "CAR_Library", tmp_path / "stick"
    song = _w(src, "H/Honey.m4a", b"song")
    # An entry the rewriter resolves into this library: without one it skips
    # the playlist anyway, and the test would pass for the wrong reason.
    _w(vault, "Playlists/Holiday.m3u8", f"#EXTM3U\n#EXTINF:1,Honey\n{song}\n".encode())
    _w(src, "Playlists/Holiday.m3u8", b"#EXTM3U\nchecked edition entry\n")
    on_stick = _w(dst, "Playlists/Holiday.m3u8", b"#EXTM3U\nchecked edition entry\n")

    written = usb.copy_playlists(vault, src, dst)

    assert written == []
    assert on_stick.read_bytes() == b"#EXTM3U\nchecked edition entry\n"


# ── 1 and 9: through main() ─────────────────────────────────────────────────


@pytest.fixture
def run_main(tmp_path, monkeypatch):
    cfg = SimpleNamespace(vault_root=tmp_path / "VAULT", runs_root=tmp_path / "RUNS")
    cfg.vault_root.mkdir()
    monkeypatch.setattr(usb, "get_config", lambda: cfg)

    def run(*argv):
        monkeypatch.setattr(sys, "argv", ["transfer_to_usb.py", *argv])
        return usb.main()

    return cfg, run


def test_copying_onto_its_own_source_is_refused(tmp_path, run_main):
    _, run = run_main
    src = tmp_path / "CAR_Library"
    song = _w(src, "A/a.m4a", b"the only copy")

    rc = run("--library", "car", "--source-dir", str(src), "--no-format", "--dest", str(src),
             "--execute")  # fmt: skip

    assert rc == 1
    assert song.read_bytes() == b"the only copy", "the source was emptied"


def test_a_transfer_waits_for_no_build(tmp_path, run_main, monkeypatch):
    cfg, run = run_main
    src, dst = tmp_path / "CAR_Library", tmp_path / "stick"
    _w(src, "A/a.m4a", b"song")
    dst.mkdir()
    monkeypatch.setattr(usb, "check_copy_target", lambda *a, **k: None, raising=False)

    with edition_build.build_lock(cfg.runs_root / "locks", "car"):
        rc = run("--library", "car", "--source-dir", str(src), "--no-format", "--dest", str(dst),
                 "--execute")  # fmt: skip

    assert rc == 1, "copied while a car build held its lock"
    assert not (dst / "A" / "a.m4a").exists()
