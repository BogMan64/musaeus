"""A preflight FAIL must stop a real run before the next stage changes a file.

On 6cad046 only a failed ffmpeg self-test stopped the pipeline. Every other
FAIL -- the archive not writable, a critically full disk, a corrupt catalogue,
a command that could not be installed -- was printed and the run went on to
mutate files anyway (Grey, 2026-11-25).

The first test fails on 6cad046 for the right reason: the stage after preflight
runs, so `ran` is not empty and the exit code is 0.
"""

from __future__ import annotations

import pytest

from musaeus.stages.base import BaseStage
from musaeus.stages.preflight import PreflightStage, stops_the_run

FORCED = "disk free on the vault: 1.0 GB (< 50 GB -- critically low)"


def _forced_fail(self, _ctx, _ok, _warn, fail):
    fail.append(FORCED)


class WouldChangeFiles(BaseStage):
    NAME = "would_change_files"
    ran: list[bool] = []

    def validate(self, ctx):
        pass

    def run(self, ctx):
        type(self).ran.append(True)
        return self._make_result()

    def dry_run(self, ctx):
        type(self).ran.append(True)
        return self._make_result(dry_run=True)


@pytest.fixture
def ran(monkeypatch):
    """A fresh marker, and one deterministic FAIL in preflight."""
    WouldChangeFiles.ran = []
    monkeypatch.setattr(PreflightStage, "_check_disk_space", _forced_fail)
    return WouldChangeFiles.ran


def _cli(disposable_vault, monkeypatch):
    import musaeus.cli as cli

    monkeypatch.setattr(cli, "get_config", lambda: disposable_vault.cfg)
    monkeypatch.setattr(cli, "_RESUME_FILE", disposable_vault.config_home / "resume.json")
    return cli


def test_a_failed_preflight_stops_a_real_run(disposable_vault, monkeypatch, ran, capsys):
    cli = _cli(disposable_vault, monkeypatch)

    code = cli._run_pipeline_inner([PreflightStage, WouldChangeFiles], dry_run=False)

    assert code == 1
    assert ran == [], "a stage ran after preflight had failed"
    err = capsys.readouterr().err
    assert "STOPPED: preflight reported FAIL" in err
    assert FORCED in err


def test_a_preview_is_never_stopped_by_a_fail():
    from musaeus.stages.preflight import stops_the_run

    """A preview changes nothing, so a FAIL is reported, not fatal.

    Both runners send a dry run to their preview path, which runs no stage;
    the rule is kept here so a future runner that does execute stages in
    preview mode cannot stop halfway through a preview.
    """
    stage = PreflightStage()
    stage.failures = [FORCED]

    assert stops_the_run(stage, dry_run=True) is False
    assert stops_the_run(stage, dry_run=False) is True


def test_a_lone_preflight_is_still_only_a_report(disposable_vault, monkeypatch, ran, capsys):
    """`musaeus preflight` has nothing after it to protect: it reports, and
    keeps the exit code contract it had (0 with FAILs printed)."""
    cli = _cli(disposable_vault, monkeypatch)

    code = cli._run_pipeline_inner([PreflightStage], dry_run=False)

    assert code == 0
    assert "Preflight: " in capsys.readouterr().out


def test_the_console_stops_too(disposable_vault, monkeypatch, ran):
    from musaeus.console import Console

    disposable_vault.cfg.ensure_dirs()
    con = Console()
    con._config = disposable_vault.cfg
    con._run_pipeline_inner(False, [PreflightStage, WouldChangeFiles], "Pipeline")
    assert ran == [], "the console ran a stage after preflight had failed"


def test_a_warning_alone_does_not_stop_the_run(disposable_vault, monkeypatch):
    """Only FAIL stops; a WARN (the archive's lock, say) is a note, not a stop."""
    monkeypatch.setattr("sys.stdin.isatty", lambda: False)
    disposable_vault.cfg.ensure_dirs()
    conn = disposable_vault.open_db()
    ctx = disposable_vault.new_context(conn, dry_run=False)
    stage = PreflightStage()

    monkeypatch.setattr(
        PreflightStage,
        "_check_directories",
        lambda self, ctx, ok, warn, fail: (
            ok.append("inbox: exists"),
            warn.append("archive is locked: 7,975 master file(s) carry the immutable flag"),
        ),
    )
    for name in (
        "_check_ffmpeg_selftest",
        "_check_packages",
        "_check_disk_space",
        "_check_db_integrity",
        "_check_stale_wal",
        "_check_edit_guard_hook",
    ):
        monkeypatch.setattr(PreflightStage, name, lambda self, *a: None)
    monkeypatch.setattr(PreflightStage, "_check_commands", lambda self, ok, warn, fail: None)
    monkeypatch.setattr(
        PreflightStage, "_check_windows_long_paths", lambda self, ok, warn, fail: None
    )
    stage.run(ctx)
    conn.close()

    assert stage.failures == []
    assert stops_the_run(stage, dry_run=False) is False


def test_the_stop_message_names_the_first_failure():
    from musaeus.stages.preflight import stop_message

    stage = PreflightStage()
    stage.failures = [FORCED, "db integrity_check FAILED: boom"]

    assert stops_the_run(stage, dry_run=False) is True
    message = stop_message(stage)
    assert FORCED in message
    assert "1 more" in message
    assert "nothing was changed" in message
