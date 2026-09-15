"""
The console must grant network authority for a LIVE run, and take it back.

Before 2026-08-24 `console.py` never referenced network_policy at all, so
the gateway stayed at its LOCAL_ONLY default for everything launched from
the menu. Every network stage — Enrich, MBEnrich, OriginalYear — was
refused, logged a warning and carried on, and the run reported success
having done no enrichment whatever. The CLI had granted this at its
execute path all along; the console was simply never given the same
treatment.

Scoped rather than a bare set_policy, because a console session is long
lived: one live run must not leave the rest of the session permissive.
"""

from __future__ import annotations

import pytest

from musaeus.console import _network_authority
from musaeus.network_policy import NetworkPolicy, get_gateway, policy


def test_a_live_run_is_granted_network():
    assert get_gateway().policy is NetworkPolicy.LOCAL_ONLY
    with _network_authority(dry_run=False):
        assert get_gateway().policy is NetworkPolicy.ALLOWED


def test_a_preview_is_not():
    """Preview never dispatches transport — same rule as the CLI."""
    with _network_authority(dry_run=True):
        assert get_gateway().policy is NetworkPolicy.LOCAL_ONLY


def test_authority_is_handed_back_afterwards():
    with _network_authority(dry_run=False):
        pass
    assert get_gateway().policy is NetworkPolicy.LOCAL_ONLY


def test_authority_is_handed_back_when_the_run_raises():
    """The error path is exactly where "put it back" gets skipped."""
    with pytest.raises(RuntimeError), _network_authority(dry_run=False):
        raise RuntimeError("stage blew up")
    assert get_gateway().policy is NetworkPolicy.LOCAL_ONLY


def test_it_nests_without_clobbering_an_outer_grant():
    with policy(NetworkPolicy.ALLOWED):
        with _network_authority(dry_run=True):
            pass
        assert get_gateway().policy is NetworkPolicy.ALLOWED
    assert get_gateway().policy is NetworkPolicy.LOCAL_ONLY


def test_every_console_execute_call_is_inside_a_grant():
    """Pins the call sites, not just the helper.

    The helper existing proves nothing if a runner forgets to use it, and
    the console has three separate places that execute stages.
    """
    import inspect

    from musaeus import console

    src = inspect.getsource(console)
    executes = src.count("stage.execute(ctx)")
    guarded = src.count("with _network_authority(dry_run):")
    assert executes >= 2
    assert guarded >= 3, "a console stage runner is executing without a network grant"
