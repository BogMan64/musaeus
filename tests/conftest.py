"""
MUSAEUS — shared pytest configuration (P0-01)

Ordering matters in this file. Everything up to and including
_redirect_home_and_clear_env() below runs at MODULE IMPORT TIME, i.e.
during pytest's *collection* phase, before any test_*.py module is
imported and before any fixture (even an autouse session fixture) runs.

This matters because musaeus/config.py reads real config files
(~/.config/musaeus/settings.env, credentials.env) and injects their
values into os.environ at ITS OWN module-import time via a call at the
bottom of that module. Every existing test file does
`from musaeus.config import MusicConfig` at module level, so that
import — and therefore the real-value leak — happens during collection.
A HOME redirect placed inside a fixture function runs too late to
prevent it (fixtures execute during test *setup*, which is after
collection). It must happen here, before musaeus.config (or anything
that imports it) is ever imported in this process.

Confirmed empirically during P0-01 (see tasks.md completion evidence):
importing musaeus.config in an unmodified process injects
MUSAEUS_VAULT_ROOT=/mnt/FORGE2TB/Projects/MUSAEUS_VAULT plus real API
keys into os.environ, because config.py's _load_env() uses
os.environ.setdefault(). Redirecting HOME first means that leak reads
an empty, disposable settings.env location instead (nothing to set).
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

# ── Step 1: redirect HOME/XDG before anything can leak real config ───────────
#
# A fresh directory per test *session* (not per test) — env vars are
# process-global, so a per-test directory would require re-exporting the
# env on every test anyway. Individual tests that want a per-test config
# home use the `disposable_vault` fixture's `.config_home`, which they can
# point specific module globals at via monkeypatch (see
# tests/test_disposable_vault.py for an example).
_SESSION_FAKE_HOME = Path(tempfile.mkdtemp(prefix="musaeus_pytest_home_"))

os.environ["HOME"] = str(_SESSION_FAKE_HOME)
os.environ["XDG_CONFIG_HOME"] = str(_SESSION_FAKE_HOME / ".config")
os.environ["XDG_DATA_HOME"] = str(_SESSION_FAKE_HOME / ".local" / "share")
os.environ["XDG_CACHE_HOME"] = str(_SESSION_FAKE_HOME / ".cache")
os.environ["XDG_STATE_HOME"] = str(_SESSION_FAKE_HOME / ".local" / "state")

# Defense in depth: also explicitly clear anything that might already be
# set in the ambient shell environment pytest was launched from (config.py
# uses setdefault(), so these would otherwise survive the HOME redirect
# above if already present before pytest started).
for _env_key in (
    "MUSAEUS_VAULT_ROOT",
    "MUSAEUS_DB_PATH",
    "MUSAEUS_INBOX",
    "MUSAEUS_RUNS_ROOT",
    "MUSAEUS_STAGING",
    "MUSAEUS_QUARANTINE",
    "MUSAEUS_META_DIR",
    "MUSAEUS_ALAC_LIBRARY",
    "GROQ_API_KEY",
    "LASTFM_API_KEY",
    "OPENROUTER_API_KEY",
    "ACOUSTICID_API_KEY",
    "MUSICBRAINZ_API_KEY",
    "DISCOGS_API_KEY",
    "SPOTIFY_CLIENT_ID",
    "SPOTIFY_CLIENT_SECRET",
    "APIFY_API_KEY",
):
    os.environ.pop(_env_key, None)

# ── Step 2: now it's safe to import musaeus / the disposable-vault harness ───
# (must stay below Step 1 — see module docstring)

import pytest  # noqa: E402

from tests.disposable_vault import (  # noqa: E402
    DisposableVault,
    PathGuard,
    TransportDenialHarness,
    make_disposable_vault,
)

# ── Step 3: install (but do not yet enable) the session-wide guards ──────────

_path_guard = PathGuard()
_path_guard.install()

_transport_harness = TransportDenialHarness()
_transport_harness.install()


@pytest.fixture(scope="session", autouse=True)
def _musaeus_safety_net():
    """
    Session-wide, always-on safety net (autouse — no test needs to request
    this explicitly). Confirmed via the P0-01 characterization pass that
    the full existing 287-test suite makes zero real-path accesses under
    PROTECTED_REAL_ROOTS and zero outbound socket connections, so enabling
    both guards for the whole session is safe by default and matches the
    design's "local-only / no network by default" posture (MCR-001/DR-06),
    rather than requiring every test to opt in individually.

    Enabled once for the whole session rather than per-test: PathGuard's
    underlying sys.addaudithook() cannot be removed once added (no CPython
    API for that), so re-adding it per test would stack hooks and add
    per-event overhead across the whole run for no benefit — toggling the
    single instance's .enabled flag is what scopes it, not reinstalling.
    """
    _path_guard.enable()
    yield
    _path_guard.disable()
    _transport_harness.uninstall()


@pytest.fixture(scope="session")
def path_guard() -> PathGuard:
    """The session-wide PathGuard instance, for tests that want to inspect
    `.attempts` or deliberately provoke+catch RealPathAccessError."""
    return _path_guard


@pytest.fixture(autouse=True)
def _restore_network_policy():
    """Put the process-wide network policy back after every test.

    The gateway in musaeus/network_policy.py is module-level mutable
    state. Before this fixture, a test that set ALLOWED and did not
    restore it left every later test in the session running against a
    permissive gateway -- so a safety assertion could pass in isolation
    and silently stop meaning anything in the full run. Found when a
    P0-14 test that read the policy passed alone and failed in the suite.

    Autouse and unconditional: a fixture that has to be requested is one
    a new test can forget to request, and the failure mode is invisible.
    """
    from musaeus.network_policy import get_gateway

    gateway = get_gateway()
    previous = gateway.policy
    try:
        yield
    finally:
        gateway.policy = previous


@pytest.fixture(scope="session")
def transport_harness() -> TransportDenialHarness:
    """The session-wide TransportDenialHarness instance, for tests that
    want to inspect `.attempts` or deliberately provoke+catch
    NetworkAccessDeniedError."""
    return _transport_harness


@pytest.fixture
def disposable_vault(tmp_path: Path) -> DisposableVault:
    """
    Primary per-test fixture: an isolated vault directory, MusicConfig,
    and disposable config-home/recovery/report roots, all rooted under
    this test's own tmp_path. See tests/disposable_vault.py for the full
    API (open_db(), new_context(), etc).
    """
    return make_disposable_vault(tmp_path)
