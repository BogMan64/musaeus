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


# ── Edit-guard hook check (added 2026-09-05) ────────────────────────────────
#
# A long-running process executes the modules it imported at startup, so
# editing one it has not yet imported loads new code against old dependencies.
# That mixture silently cost the ForClaudeHandoff doc of a 42-hour run. The
# Claude Code hook warns about it; preflight checks the hook is actually
# there, because a hook that never loaded is indistinguishable from one that
# had nothing to say.


def _guard_env(tmp_path, monkeypatch, *, registered=True, script="exec", settings="json"):
    """Build an isolated settings.json + hook script and point preflight at it."""
    import json as _json
    import os as _os

    from musaeus.stages import preflight as pf

    hook = tmp_path / "musaeus_pipeline_guard.sh"
    if script != "absent":
        hook.write_text("#!/bin/bash\nexit 0\n")
        _os.chmod(hook, 0o755 if script == "exec" else 0o644)

    st = tmp_path / "settings.json"
    if settings == "malformed":
        st.write_text("{ not json")
    elif settings == "list":
        st.write_text("[1, 2, 3]")
    elif settings == "absent":
        pass
    else:
        entries = (
            [{"matcher": "Edit|Write|Bash",
              "hooks": [{"type": "command", "command": str(hook)}]}]
            if registered else []
        )
        st.write_text(_json.dumps({
            # A realistic settings file also holds secrets; they must never
            # reach a preflight message.
            "env": {"SECRET_TOKEN": "sk-must-not-leak"},
            "hooks": {"PreToolUse": entries},
        }))

    monkeypatch.setattr(pf, "_CLAUDE_SETTINGS", st)
    monkeypatch.setattr(pf, "_EDIT_GUARD_SCRIPT", hook)
    ok, warn, fail = [], [], []
    PreflightStage.__new__(PreflightStage)._check_edit_guard_hook(ok, warn, fail)
    return ok, warn, fail


def test_edit_guard_registered_and_executable_is_ok(tmp_path, monkeypatch):
    ok, warn, fail = _guard_env(tmp_path, monkeypatch)
    assert any("edit-guard hook" in m for m in ok)
    assert not warn and not fail


def test_edit_guard_not_registered_warns(tmp_path, monkeypatch):
    ok, warn, fail = _guard_env(tmp_path, monkeypatch, registered=False)
    assert any("not registered" in m for m in warn)
    assert not fail, "a missing hook must never fail the run"


def test_edit_guard_not_executable_warns(tmp_path, monkeypatch):
    ok, warn, fail = _guard_env(tmp_path, monkeypatch, script="noexec")
    assert any("not executable" in m for m in warn)


def test_edit_guard_script_missing_warns(tmp_path, monkeypatch):
    ok, warn, fail = _guard_env(tmp_path, monkeypatch, script="absent")
    assert any("missing" in m for m in warn)


def test_edit_guard_malformed_settings_warns(tmp_path, monkeypatch):
    """A settings file that does not parse is ignored wholesale, which takes
    every hook down silently -- exactly what this check exists to surface."""
    ok, warn, fail = _guard_env(tmp_path, monkeypatch, settings="malformed")
    assert any("does not parse" in m for m in warn)


def test_edit_guard_settings_not_an_object_warns(tmp_path, monkeypatch):
    ok, warn, fail = _guard_env(tmp_path, monkeypatch, settings="list")
    assert any("not a JSON object" in m for m in warn)


def test_edit_guard_absent_claude_code_is_silent(tmp_path, monkeypatch):
    """No Claude Code on the machine means nothing to guard -- not a warning.
    Grey runs MUSAEUS from a plain terminal too."""
    ok, warn, fail = _guard_env(tmp_path, monkeypatch, settings="absent")
    assert not ok and not warn and not fail


def test_edit_guard_never_leaks_settings_secrets(tmp_path, monkeypatch):
    """settings.json holds env blocks and API headers. Only hook COMMAND
    strings may be read, and no message may echo a secret."""
    ok, warn, fail = _guard_env(tmp_path, monkeypatch, registered=False)
    assert not any("must-not-leak" in m for m in ok + warn + fail)
