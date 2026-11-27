"""Every script that writes to the masters takes the masters lock.

Review of #87 (2026-10-07) made the lock, but only for the pipeline and the
tools that already had it. Measured 2026-11-25: 16 of 55 scripts wrote audio
or moved files with no lock at all, so a second job could run beside them --
exactly what the lock exists to stop.

Seven of them are fixed here. The eighth, merge_case_duplicate_albums, is
retired: it exits 2 before it can touch anything. The test runs each one for real, with the lock
already held by another process, and expects the refusal: exit 75 and the
one-line NOT RUN message. It is the wiring that matters, not the message.

A dry run stays lock-free on purpose. Asking "what would you do?" must
always be possible, even while a real job is running -- so the same test
without the write flag asserts the script got past the lock.

Fails on 6cad046 for the right reason: the script never looks at the lock,
so it does its work (or dies on the empty vault) instead of refusing.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from musaeus.masters_lock import EXIT_BUSY, masters_lock

REPO = Path(__file__).resolve().parents[1]

#: script name -> the flag that turns it from a dry run into a real write.
WRITE_FLAGS = {
    "backfill_identity_tags": "--apply",
    "musaeus_migrate_to_archive": "--execute",
    "musaeus_relink_recovered_dupes": "--execute",
    "prune_recovery": "--apply",
    "repair_album_artist": "--apply",
    "repair_artist_casing": "--apply",
    "split_artist_sort_form": "--apply",
}


def _env(tmp_path: Path) -> dict[str, str]:
    # No MUSAEUS_* setting is inherited. A developer with
    # MUSAEUS_ALAC_LIBRARY or MUSAEUS_ALAC_ARCHIVE exported would otherwise
    # hand these subprocesses their real library (CodeRabbit on #337,
    # 2026-11-26) -- a test that can write to real music is not a test.
    inherited = {k: v for k, v in os.environ.items() if not k.startswith("MUSAEUS_")}
    return {
        **inherited,
        "MUSAEUS_VAULT_ROOT": str(tmp_path / "vault"),
        "MUSAEUS_RUNS_ROOT": str(tmp_path / "RUNS"),
        # Pinned for the same reason, and it matters most: musaeus_migrate_to_
        # archive connects to the catalogue at import time, before it looks at
        # --execute, so an unpinned database path means a dry run opens the
        # developer's real catalogue (CodeRabbit on #337, 2026-11-27).
        "MUSAEUS_DB_PATH": str(tmp_path / "musaeus.db"),
        # Pinned, not just unset: a dry run of backfill_identity_tags,
        # repair_album_artist, repair_artist_casing or split_artist_sort_form
        # walks the library with rglob, so the walk must be inside tmp_path.
        "MUSAEUS_ALAC_LIBRARY": str(tmp_path / "ALAC_Library"),
        "MUSAEUS_ALAC_ARCHIVE": str(tmp_path / "ALAC-Archival"),
        "PYTHONPATH": str(REPO),
    }


def _run(script: str, *flags: str, tmp_path: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(REPO / "scripts" / f"{script}.py"), *flags],
        capture_output=True,
        text=True,
        env=_env(tmp_path),
        cwd=str(tmp_path),
        timeout=180,
        check=False,
    )


@pytest.mark.parametrize("script", sorted(WRITE_FLAGS))
@pytest.mark.parametrize(
    "holder_exclusive", [True, False], ids=["exclusive-holder", "shared-holder"]
)
def test_a_write_run_refuses_while_another_job_holds_the_lock(script, holder_exclusive, tmp_path):
    """A writer must refuse under a shared holder too.

    An exclusive request is refused by either kind of holder, so a script
    that quietly asked for a shared lock would still pass the exclusive case
    and then run beside another lock holder (CodeRabbit on #337).
    """
    runs = tmp_path / "RUNS"
    runs.mkdir(parents=True)
    with masters_lock(runs, exclusive=holder_exclusive, what="test holder"):
        r = _run(script, WRITE_FLAGS[script], tmp_path=tmp_path)

    assert r.returncode == EXIT_BUSY, (
        f"{script} {WRITE_FLAGS[script]} ran while the masters were locked "
        f"(exit {r.returncode})\nstdout: {r.stdout[-400:]}\nstderr: {r.stderr[-400:]}"
    )
    assert "NOT RUN" in r.stderr


#: These two open the catalogue at import time and cannot finish a dry run
#: against an empty one. What the dry-run test proves for them is the lock:
#: no EXIT_BUSY and no "NOT RUN", which happens before any of that.
NEEDS_A_CATALOGUE = {"musaeus_migrate_to_archive", "musaeus_relink_recovered_dupes"}


@pytest.mark.parametrize("script", sorted(WRITE_FLAGS))
def test_a_dry_run_is_not_blocked_by_the_lock(script, tmp_path):
    runs = tmp_path / "RUNS"
    runs.mkdir(parents=True)
    # A real, empty library: without it backfill_identity_tags refuses at its
    # root check (exit 2) before it scans, and the test used to accept that as
    # a dry run that worked (CodeRabbit on #337, 2026-11-27).
    (tmp_path / "ALAC_Library").mkdir()
    (tmp_path / "ALAC-Archival").mkdir()
    with masters_lock(runs, exclusive=True, what="test holder"):
        r = _run(script, tmp_path=tmp_path)

    assert r.returncode != EXIT_BUSY, f"{script} took the lock on a dry run"
    assert "NOT RUN" not in r.stderr
    if script not in NEEDS_A_CATALOGUE:
        assert r.returncode == 0, (
            f"{script} dry run stopped early (exit {r.returncode}) instead of "
            f"reporting nothing to do\nstdout: {r.stdout[-400:]}\n"
            f"stderr: {r.stderr[-400:]}"
        )


def test_the_subprocess_environment_is_a_temporary_one(tmp_path):
    """A test that can reach real music, or the real catalogue, is not a test.

    ``musaeus.config`` restores any absent MUSAEUS_* key from settings.env,
    credentials.env and the project .env with os.environ.setdefault, so an
    unset key is not the same as a removed one (CodeRabbit on #337,
    2026-11-27). Every path these subprocesses can write to is pinned here.
    """
    env = _env(tmp_path)
    keys = (
        "MUSAEUS_VAULT_ROOT",
        "MUSAEUS_RUNS_ROOT",
        "MUSAEUS_DB_PATH",
        "MUSAEUS_ALAC_LIBRARY",
        "MUSAEUS_ALAC_ARCHIVE",
    )
    # Report the key names, never the mapping: this environment is the
    # developer's whole environment, API keys included, and pytest prints an
    # assertion's operands on failure.
    missing = [k for k in keys if k not in env]
    assert not missing, f"not pinned, so settings.env can supply the real path: {missing}"
    outside = {k: env[k] for k in keys if not Path(env[k]).is_relative_to(tmp_path)}
    assert not outside, f"pinned outside the test: {sorted(outside)}"


def _prune_env(tmp_path: Path) -> dict[str, str]:
    """The environment for prune_recovery with no vault configured.

    ``MUSAEUS_VAULT_ROOT`` is set to an **empty string**, not removed: it is
    absent from os.environ either way, and musaeus.config restores an absent
    key from settings.env and the project .env with os.environ.setdefault. An
    empty value blocks that, and from_env() treats it as unset, so the script
    still reaches its "no settings" path (CodeRabbit on #337, 2026-11-27).
    """
    env = _env(tmp_path)
    for key in ("MUSAEUS_RUNS_ROOT", "MUSAEUS_DB_PATH"):
        env.pop(key, None)
    env["MUSAEUS_VAULT_ROOT"] = ""
    return env


def _run_prune(tmp_path: Path, *flags: str) -> subprocess.CompletedProcess[str]:
    """prune_recovery with NO vault settings, to test what it says then."""
    return subprocess.run(
        [sys.executable, str(REPO / "scripts" / "prune_recovery.py"), *flags],
        capture_output=True,
        text=True,
        env=_prune_env(tmp_path),
        cwd=str(tmp_path),
        timeout=180,
        check=False,
    )


def test_prune_without_settings_cannot_inherit_a_vault(tmp_path):
    """A removed key is restored from settings.env; an empty one is not.

    Without this the apply case can resolve the developer's real vault, take
    the real masters lock and never reach the missing-settings message it is
    supposed to be testing (CodeRabbit on #337, 2026-11-27).
    """
    env = _prune_env(tmp_path)
    assert "MUSAEUS_VAULT_ROOT" in env, "removing the key lets settings.env restore it"
    assert env["MUSAEUS_VAULT_ROOT"] == "", (
        "an empty value is how the key is made unset-and-unrestorable"
    )


def test_prune_recovery_apply_without_settings_says_so(tmp_path):
    """CodeRabbit on #337, 2026-11-26: the lock lives under the vault's RUNS,
    so an --apply run cannot place it without settings. It used to die in an
    uncaught ValueError from get_config(); it now says so in one line and
    exits 2."""
    r = _run_prune(tmp_path, "--apply", "--root", str(tmp_path / "recovery"))

    assert r.returncode == 2, f"exit {r.returncode}\nstderr: {r.stderr[-400:]}"
    assert "Could not resolve config" in r.stderr
    assert "Traceback" not in r.stderr
    # The advice must not send the run somewhere else (CodeRabbit on #337):
    # dropping --root would prune cfg.runs_root/recovery instead of the folder
    # that was asked for.
    assert "set MUSAEUS_VAULT_ROOT" in r.stderr
    assert "pass no --root" not in r.stderr


def test_prune_recovery_dry_run_with_root_needs_no_settings(tmp_path):
    """Asking a question never needs the vault: a dry run with --root and no
    settings still reports."""
    (tmp_path / "recovery").mkdir()

    r = _run_prune(tmp_path, "--root", str(tmp_path / "recovery"))

    assert r.returncode == 0, f"exit {r.returncode}\nstderr: {r.stderr[-400:]}"
    assert "No checkpoints" in r.stdout
