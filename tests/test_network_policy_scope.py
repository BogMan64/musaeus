"""
The process-wide network gateway must not leak its policy.

`musaeus/network_policy.py` holds a module-level Gateway, so a caller
that sets ALLOWED and does not restore it leaves everything afterwards in
the process running permissive. In a test session that means a safety
assertion can pass in isolation and quietly stop meaning anything in the
full run -- which is how this was found: a P0-14 test read the policy,
passed alone, and failed in the suite.
"""

from __future__ import annotations

import pytest

from musaeus.network_policy import (
    Gateway,
    NetworkDenied,
    NetworkPolicy,
    get_gateway,
    policy,
    set_policy,
)


class TestScopedPolicy:
    def test_a_fresh_gateway_defaults_to_local_only(self):
        assert Gateway().policy is NetworkPolicy.LOCAL_ONLY

    def test_the_context_manager_restores_the_previous_policy(self):
        gateway = get_gateway()
        before = gateway.policy
        with policy(NetworkPolicy.ALLOWED):
            assert gateway.policy is NetworkPolicy.ALLOWED
        assert gateway.policy is before

    def test_it_restores_on_the_error_path_too(self):
        """The path where 'remember to put it back' actually gets skipped."""
        gateway = get_gateway()
        before = gateway.policy
        with pytest.raises(RuntimeError), policy(NetworkPolicy.ALLOWED):
            raise RuntimeError("stage blew up mid-lookup")
        assert gateway.policy is before

    def test_nesting_restores_to_the_enclosing_policy(self):
        gateway = get_gateway()
        with policy(NetworkPolicy.LOCAL_ONLY):
            with policy(NetworkPolicy.ALLOWED):
                assert gateway.policy is NetworkPolicy.ALLOWED
            assert gateway.policy is NetworkPolicy.LOCAL_ONLY

    def test_denial_still_raises_and_records_inside_the_scope(self):
        gateway = get_gateway()
        gateway.reset()
        with policy(NetworkPolicy.LOCAL_ONLY), pytest.raises(NetworkDenied):
            gateway.check("https://ws.audioscrobbler.com/2.0/")
        assert gateway.denials == ["https://ws.audioscrobbler.com/2.0/"]
        gateway.reset()


class TestAutouseRestoration:
    """These two tests are ordered deliberately: the first leaks on
    purpose, the second proves the autouse fixture cleaned up after it.

    Written as a pair because a single test cannot observe its own
    teardown, and the leak this guards against is precisely one that only
    shows up in a later test."""

    def test_a_deliberately_leaking_test_runs_first(self):
        set_policy(NetworkPolicy.ALLOWED)
        assert get_gateway().policy is NetworkPolicy.ALLOWED

    def test_the_next_test_is_not_permissive(self):
        assert get_gateway().policy is NetworkPolicy.LOCAL_ONLY, (
            "the previous test's ALLOWED leaked into this one"
        )
