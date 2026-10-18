"""Codex audit 2026-10-16, F4: the permissions sweep followed symlinks.

A link dropped into INBOX (or any swept tree) pointing at a private file
elsewhere -- a credentials file at 0600 -- had its TARGET changed to 0644,
and the stage reported a successful fix. Only objects inside the swept
trees, and never through a link, may change.
"""

from __future__ import annotations

import stat
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


def test_a_link_to_a_private_file_outside_is_never_changed(ctx, tmp_path):
    secret = tmp_path / "elsewhere" / "credentials.env"
    secret.parent.mkdir()
    secret.write_text("KEY=dummy")
    secret.chmod(0o600)
    private_dir = tmp_path / "elsewhere" / "private"
    private_dir.mkdir()
    (private_dir / "inner.txt").write_text("x")
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


def test_a_dry_run_does_not_list_a_links_target(ctx, tmp_path):
    secret = tmp_path / "credentials.env"
    secret.write_text("KEY=dummy")
    secret.chmod(0o600)
    (ctx.inbox / "readme.txt").symlink_to(secret)

    result = PermissionsStage().dry_run(ctx)

    assert result.files_processed == 0
    assert not any("credentials" in n for n in result.notes)
