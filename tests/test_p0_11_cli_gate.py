"""
P0-11 wiring — the CLI's execution-authority gate.

The property that matters most here is the one about *not* changing
anything: the gate is opt-in, and with it off `_run_pipeline` behaves
exactly as it did. Turning it on by default would make every run stop to
ask and would correctly make the unattended overnight script do nothing
every night. Both are right; both must be adopted deliberately.
"""

from __future__ import annotations

import io
from pathlib import Path

import pytest

from musaeus.cli_gate import (
    EXIT_REVIEW_ONLY,
    EXIT_SAFETY_BLOCKED,
    GATE_ENV,
    GATE_FLAG,
    build_request,
    enforce_execution_gate,
    gate_enabled,
)
from musaeus.state.migrator import migrate


@pytest.fixture
def ready_vault(disposable_vault):
    """A vault whose preflight passes cleanly."""
    vault = disposable_vault
    for d in (vault.root, vault.cfg.inbox, vault.cfg.alac_library, vault.cfg.runs_root):
        d.mkdir(parents=True, exist_ok=True)
    (vault.cfg.runs_root / "recovery").mkdir(parents=True, exist_ok=True)
    (vault.cfg.runs_root / "locks").mkdir(parents=True, exist_ok=True)
    conn = vault.open_db()
    conn.close()
    migrate(vault.cfg.db_path, recovery_root=vault.cfg.runs_root / "recovery")
    return vault


# ── Off by default ────────────────────────────────────────────────────────────


class TestGateIsOptIn:
    @pytest.mark.parametrize(
        "argv, env",
        [
            (["musaeus", "run"], {}),
            (["musaeus", "run", "--force"], {"MUSAEUS_VAULT_ROOT": "/x"}),
            (["musaeus", "run"], {GATE_ENV: ""}),
            (["musaeus", "run"], {GATE_ENV: "0"}),
            (["musaeus", "run"], {GATE_ENV: "false"}),
        ],
    )
    def test_it_is_off_unless_explicitly_turned_on(self, argv, env):
        assert gate_enabled(argv=argv, env=env) is False

    @pytest.mark.parametrize("value", ["1", "true", "yes", "on", " 1 "])
    def test_the_env_var_turns_it_on(self, value):
        assert gate_enabled(argv=["musaeus", "run"], env={GATE_ENV: value}) is True

    def test_the_flag_turns_it_on(self):
        assert gate_enabled(argv=["musaeus", "run", GATE_FLAG], env={}) is True

    def test_when_off_it_returns_none_without_touching_anything(self, path_guard):
        """The default path must not even read the config -- passing a bare
        object() proves nothing is dereferenced."""
        before = len(path_guard.attempts)
        assert enforce_execution_gate(object(), argv=["musaeus", "run"], env={}) is None
        assert len(path_guard.attempts) == before

    def test_the_splice_in_cli_is_two_statements_and_guarded(self):
        """Keeps the merge surface small and the default provably inert."""
        source = Path("musaeus/cli.py").read_text()
        assert "from .cli_gate import enforce_execution_gate" in source
        assert "gate_exit = enforce_execution_gate(cfg, dry_run=dry_run)" in source
        assert "if gate_exit is not None:\n        return gate_exit" in source


# ── On: blocking ──────────────────────────────────────────────────────────────


class TestGateBlocks:
    def test_a_blocked_preflight_exits_2_and_says_nothing_changed(self, ready_vault):
        out = io.StringIO()
        code = enforce_execution_gate(
            ready_vault.cfg,
            argv=["musaeus", "run", GATE_FLAG],
            env={},
            stream=out,
            required_providers=("lastfm",),
        )
        assert code == EXIT_SAFETY_BLOCKED
        text = out.getvalue()
        assert "REFUSED" in text
        assert "Nothing was changed" in text
        assert "provider_consent" in text

    def test_the_report_is_printed_with_measurements(self, ready_vault):
        out = io.StringIO()
        enforce_execution_gate(
            ready_vault.cfg, argv=["musaeus", "run", GATE_FLAG], env={}, stream=out
        )
        text = out.getvalue()
        assert "100 GB" in text
        assert "/home/grey/Projects/MUSAEUS_RECOVERY" in text
        assert "not created or probed" in text


# ── On: authority ─────────────────────────────────────────────────────────────


class TestGateAuthority:
    def test_a_clean_preflight_without_y_is_review_only(self, ready_vault):
        out = io.StringIO()
        code = enforce_execution_gate(
            ready_vault.cfg,
            argv=["musaeus", "run", GATE_FLAG],
            env={},
            stream=out,
            response=None,
            response_supplied=True,
        )
        assert code == EXIT_REVIEW_ONLY
        assert "Review only" in out.getvalue()
        assert "Nothing was changed" in out.getvalue()

    def test_explicit_y_lets_the_run_proceed(self, ready_vault):
        """Negative control: the gate must be able to say yes, or it is a
        blanket refusal wearing a prompt."""
        assert (
            enforce_execution_gate(
                ready_vault.cfg,
                argv=["musaeus", "run", GATE_FLAG],
                env={},
                stream=io.StringIO(),
                response="y",
                response_supplied=True,
            )
            is None
        )

    @pytest.mark.parametrize("answer", [None, "", "n", "N", "Y", "yes", "\n"])
    def test_nothing_but_lowercase_y_proceeds(self, ready_vault, answer):
        assert (
            enforce_execution_gate(
                ready_vault.cfg,
                argv=["musaeus", "run", GATE_FLAG],
                env={},
                stream=io.StringIO(),
                response=answer,
                response_supplied=True,
            )
            == EXIT_REVIEW_ONLY
        )

    def test_a_non_interactive_invocation_never_prompts_or_proceeds(self, ready_vault):
        """The overnight script's case: no tty, no operator."""
        code = enforce_execution_gate(
            ready_vault.cfg,
            argv=["musaeus", "run", GATE_FLAG],
            env={},
            stream=io.StringIO(),
            interactive=False,
        )
        assert code == EXIT_REVIEW_ONLY

    def test_interactive_false_refuses_even_with_a_tty_present(self, ready_vault, monkeypatch):
        """Isolates the `interactive` flag from the tty check.

        Both guards exist, and under pytest stdin is never a tty -- so the
        earlier test passed even with the `interactive` check removed,
        because `isatty()` was refusing instead. Here stdin claims to be a
        tty AND would answer "y", and the invocation must still refuse
        because it declared itself non-interactive."""

        class _TtyThatWouldSayYes(io.StringIO):
            def isatty(self):
                return True

        monkeypatch.setattr("sys.stdin", _TtyThatWouldSayYes("y\n"))
        monkeypatch.setattr("builtins.input", lambda *a: "y")

        code = enforce_execution_gate(
            ready_vault.cfg,
            argv=["musaeus", "run", GATE_FLAG],
            env={},
            stream=io.StringIO(),
            interactive=False,
        )
        assert code == EXIT_REVIEW_ONLY, (
            "a non-interactive invocation must not consult stdin at all"
        )

    def test_a_closed_stdin_is_not_read_as_yes(self, ready_vault, monkeypatch):
        monkeypatch.setattr("sys.stdin", io.StringIO(""))
        code = enforce_execution_gate(
            ready_vault.cfg,
            argv=["musaeus", "run", GATE_FLAG],
            env={},
            stream=io.StringIO(),
            interactive=True,
        )
        assert code == EXIT_REVIEW_ONLY

    def test_preview_does_not_reach_the_authority_question(self, ready_vault):
        """There is nothing to authorise in a preview."""
        assert (
            enforce_execution_gate(
                ready_vault.cfg,
                argv=["musaeus", "run", GATE_FLAG],
                env={},
                stream=io.StringIO(),
                dry_run=True,
                response=None,
                response_supplied=True,
            )
            is None
        )


class TestRequestConstruction:
    def test_the_request_is_built_from_the_live_config(self, ready_vault):
        request = build_request(ready_vault.cfg)
        assert request.source_root == ready_vault.cfg.inbox
        assert request.destination_root == ready_vault.cfg.alac_library
        assert request.db_path == ready_vault.cfg.db_path
        assert request.scope.root == str(ready_vault.cfg.vault_root)
