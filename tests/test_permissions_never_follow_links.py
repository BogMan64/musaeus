"""Codex audit 2026-10-16, F4: the permissions sweep followed symlinks.

A link dropped into INBOX (or any swept tree) pointing at a private file
elsewhere -- a credentials file at 0600 -- had its TARGET changed to 0644,
and the stage reported a successful fix. Only objects inside the swept
trees, and never through a link, may change.
"""

from __future__ import annotations

import stat
import sys
from pathlib import Path

import pytest

from musaeus.config import MusicConfig
from musaeus.context import RunContext
from musaeus.db import open_db
from musaeus.stages.permissions import PermissionsStage


@pytest.fixture
def ctx(tmp_path: Path) -> RunContext:
    cfg = MusicConfig(
        vault_root=tmp_path / "VAULT",
        inbox=tmp_path / "VAULT" / "INBOX",
        staging=tmp_path / "VAULT" / "STAGING",
        quarantine=tmp_path / "VAULT" / "QUARANTINE",
        runs_root=tmp_path / "VAULT" / "RUNS",
        meta_dir=tmp_path / "VAULT" / "MetaData",
        alac_library=tmp_path / "VAULT" / "Libraries" / "ALAC_Library",
        db_path=tmp_path / "VAULT" / "musaeus.db",
    )
    cfg.ensure_dirs()
    return RunContext.new(cfg, open_db(cfg.db_path), dry_run=False)


def _mode(p: Path) -> int:
    return stat.S_IMODE(p.lstat().st_mode)


@pytest.mark.skipif(
    sys.platform == "win32",
    reason="Windows cannot represent the POSIX permission bits this test asserts.",
)
@pytest.mark.needs_symlinks
def test_a_link_to_a_private_file_outside_is_never_changed(ctx, tmp_path):
    secret = tmp_path / "elsewhere" / "credentials.env"
    secret.parent.mkdir()
    secret.write_text("KEY=dummy", encoding="utf-8")
    secret.chmod(0o600)
    private_dir = tmp_path / "elsewhere" / "private"
    private_dir.mkdir()
    (private_dir / "inner.txt").write_text("x", encoding="utf-8")
    (private_dir / "inner.txt").chmod(0o600)
    private_dir.chmod(0o700)
    own = ctx.inbox / "song.m4a"
    own.write_bytes(b"audio")
    own.chmod(0o600)
    (ctx.inbox / "readme.txt").symlink_to(secret)
    (ctx.inbox / "folder").symlink_to(private_dir, target_is_directory=True)

    result = PermissionsStage().run(ctx)

    assert _mode(secret) == 0o600, "a file outside the vault was opened up through a link"
    assert _mode(private_dir) == 0o700 and _mode(private_dir / "inner.txt") == 0o600
    assert _mode(own) == 0o644, "the vault's own file was not fixed"
    assert result.files_changed == 1


@pytest.mark.needs_symlinks
def test_a_dry_run_does_not_list_a_links_target(ctx, tmp_path):
    secret = tmp_path / "credentials.env"
    secret.write_text("KEY=dummy", encoding="utf-8")
    secret.chmod(0o600)
    (ctx.inbox / "readme.txt").symlink_to(secret)

    result = PermissionsStage().dry_run(ctx)

    assert result.files_processed == 0
    assert not any("credentials" in n for n in result.notes)


@pytest.mark.skipif(
    sys.platform == "win32",
    reason="Windows does not support POSIX named pipes.",
)
def test_a_fifo_swapped_in_after_the_scan_does_not_hang_and_is_not_changed(tmp_path):
    # Review of #203, finding 1: opening a FIFO with no writer blocks for ever.
    import os

    from musaeus.stages.permissions import _chmod_no_follow

    fifo = tmp_path / "song.m4a"
    os.mkfifo(fifo, 0o600)
    with pytest.raises(OSError, match="no longer a file"):
        _chmod_no_follow(fifo, 0o644, want_dir=False)
    assert _mode(fifo) == 0o600


@pytest.mark.skipif(
    sys.platform == "win32",
    reason="Windows cannot represent the POSIX permission bits this test asserts.",
)
@pytest.mark.needs_symlinks
def test_a_link_swapped_in_after_the_scan_is_left_alone(tmp_path):
    from musaeus.stages.permissions import _chmod_no_follow

    secret = tmp_path / "credentials.env"
    secret.write_text("KEY=dummy", encoding="utf-8")
    secret.chmod(0o600)
    link = tmp_path / "song.m4a"
    link.symlink_to(secret)
    with pytest.raises(OSError):
        _chmod_no_follow(link, 0o644, want_dir=False)
    assert _mode(secret) == 0o600


@pytest.mark.skipif(
    sys.platform == "win32",
    reason="Windows cannot represent the POSIX permission bits this test asserts.",
)
def test_a_file_its_owner_cannot_read_is_still_fixed(ctx):
    own = ctx.inbox / "song.m4a"
    own.write_bytes(b"audio")
    own.chmod(0o200)
    PermissionsStage().run(ctx)
    assert _mode(own) == 0o644


def test_on_windows_only_a_read_only_file_is_changed(ctx, monkeypatch):
    # Windows trial 2026-11-12: Windows has no POSIX permission bits (every
    # file reads 0o666), so each file looked wrong, each fix failed, and every
    # run ended "with errors" -- 7 errors for 7 songs. The read-only flag is the
    # one thing the sweep can still mend there: a read-only song cannot be tagged.
    fine = ctx.inbox / "song.m4a"
    fine.write_bytes(b"audio")
    fine.chmod(0o600)
    locked = ctx.inbox / "from a cd.m4a"
    locked.write_bytes(b"audio")
    locked.chmod(0o444)
    before = _mode(fine)  # not 0o600 on real Windows: it has no such bits
    monkeypatch.setattr(sys, "platform", "win32")

    result = PermissionsStage().run(ctx)

    assert result.files_errored == 0 and not result.errors
    assert result.files_changed == 1, result.notes
    assert _mode(fine) == before, "a writable file was touched on Windows"
    assert _mode(locked) & stat.S_IWRITE, "the read-only file is still read-only"


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX permission bits")
@pytest.mark.parametrize("how", ["ignored", "refused"])
def test_a_disk_without_unix_permissions_is_left_alone_not_failed(ctx, monkeypatch, how):
    # INBOX on exFAT, NTFS or vfat: chmod does nothing, or the disk refuses its
    # own owner. Every file looked wrong and every run ended with N errors.
    import errno

    from musaeus.stages import permissions

    for name in ("a.m4a", "b.m4a", "c.m4a"):
        (ctx.inbox / name).write_bytes(b"audio")
        (ctx.inbox / name).chmod(0o600)
    calls = []

    def chmod(path, mode, want_dir):
        calls.append(path)
        if how == "refused":
            raise PermissionError(errno.EPERM, "Operation not permitted")

    monkeypatch.setattr(permissions, "_chmod_no_follow", chmod)
    monkeypatch.setattr(permissions, "_disk_has_modes", lambda folder: False)
    stage = PermissionsStage()

    result = stage.run(ctx)

    assert result.files_errored == 0 and not result.errors, result.errors
    assert result.files_changed == 0
    assert len(calls) == 1, "it kept trying on a disk that had already shown it cannot"
    assert any("no Unix permissions" in n for n in result.notes), result.notes
    assert stage.verify_effect(ctx, result) == []


@pytest.mark.skipif(
    sys.platform == "win32",
    reason="POSIX permission bits",
)
def test_a_failed_chmod_is_not_reported_as_all_correct(ctx, monkeypatch):
    # Same trial: "errors=7" printed under "All file/folder permissions correct."
    from musaeus.stages import permissions

    own = ctx.inbox / "song.m4a"
    own.write_bytes(b"audio")
    own.chmod(0o600)

    def refuse(path, mode, want_dir):
        raise OSError("refused")

    monkeypatch.setattr(permissions, "_chmod_no_follow", refuse)

    result = PermissionsStage().run(ctx)

    assert result.files_errored == 1
    assert not any("correct" in n for n in result.notes), result.notes


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX permission bits")
def test_one_refused_file_on_a_unix_disk_does_not_write_off_the_folder(ctx, monkeypatch):
    # CodeRabbit, 2026-11-13: an immutable file refuses its owner too. That is
    # one file's problem; the rest of the folder must still be mended.
    import errno

    from musaeus.stages import permissions

    for name in ("a.m4a", "b.m4a"):
        (ctx.inbox / name).write_bytes(b"audio")
        (ctx.inbox / name).chmod(0o600)
    real = permissions._chmod_no_follow

    def chmod(path, mode, want_dir):
        if path.name == "a.m4a":
            raise PermissionError(errno.EPERM, "Operation not permitted")
        real(path, mode, want_dir)

    monkeypatch.setattr(permissions, "_chmod_no_follow", chmod)

    result = PermissionsStage().run(ctx)

    assert result.files_errored == 1 and result.files_changed == 1
    assert _mode(ctx.inbox / "b.m4a") == 0o644
    assert not any("no Unix permissions" in n for n in result.notes)


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX permission bits")
def test_the_disk_probe_says_yes_on_a_unix_disk_and_leaves_nothing_behind(tmp_path):
    from musaeus.stages.permissions import _disk_has_modes

    assert _disk_has_modes(tmp_path) is True
    assert list(tmp_path.iterdir()) == []
