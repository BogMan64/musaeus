"""
MUSAEUS — Tests: Preflight

Covers the interactive auto-install/API-key-offer safety invariants added
2026-08-17: dry_run() must stay zero-mutation, a non-interactive run()
(no TTY) must stay report-only exactly as before this feature existed,
and declining the batch install prompt must never touch subprocess.
"""
from __future__ import annotations

from musaeus.stages.preflight import PreflightStage


def test_dry_run_untouched_and_no_prompt(disposable_vault, monkeypatch, capsys):
    """dry_run() must stay zero-mutation: no install offer, no API-key offer,
    even if stdin claims to be a TTY."""
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)

    def _boom(*a, **k):
        raise AssertionError("dry_run() must never prompt")

    monkeypatch.setattr(PreflightStage, "_offer_installs", _boom)
    monkeypatch.setattr(PreflightStage, "_offer_api_keys", _boom)

    conn = disposable_vault.open_db()
    ctx = disposable_vault.new_context(conn, dry_run=True)
    stage = PreflightStage()
    result = stage.dry_run(ctx)
    assert result.files_processed > 0
    conn.close()


def test_run_no_tty_stays_report_only(disposable_vault, monkeypatch):
    """Non-interactive run() (no TTY) must never call the new prompts --
    matches the existing report-only behavior for cron/overnight."""
    monkeypatch.setattr("sys.stdin.isatty", lambda: False)

    def _boom(*a, **k):
        raise AssertionError("run() without a TTY must never prompt")

    monkeypatch.setattr(PreflightStage, "_offer_installs", _boom)
    monkeypatch.setattr(PreflightStage, "_offer_api_keys", _boom)

    conn = disposable_vault.open_db()
    ctx = disposable_vault.new_context(conn, dry_run=False)
    stage = PreflightStage()
    result = stage.run(ctx)
    assert result.files_processed > 0
    conn.close()


def test_run_tty_declines_batch_prompt_is_report_only(disposable_vault, monkeypatch, capsys):
    """Interactive run(), but the user says no to the batch install prompt --
    must fall back to the old report-only text, no subprocess call."""
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    monkeypatch.setattr("builtins.input", lambda *_a: "n")

    def _boom(*a, **k):
        raise AssertionError("declining the batch prompt must never call subprocess")

    monkeypatch.setattr("musaeus.stages.preflight.subprocess.run", _boom)

    conn = disposable_vault.open_db()
    ctx = disposable_vault.new_context(conn, dry_run=False)
    stage = PreflightStage()
    result = stage.run(ctx)
    assert result.files_processed > 0
    conn.close()
