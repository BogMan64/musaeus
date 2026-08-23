"""
Tests for the network-policy gateway and the transport-denial harness.

The failure mode being guarded: several stages wrap network calls in
`except Exception`, so an exception raised by a policy is swallowed and
the stage reports a clean miss. "No exception escaped" and "nothing tried
to connect" are different claims, and only the second is worth asserting.
"""

from __future__ import annotations

import contextlib
import socket

import pytest

from musaeus.network_policy import (
    Gateway,
    NetworkDenied,
    NetworkPolicy,
    get_gateway,
    set_policy,
)
from tests.transport_denial import TransportAttempted, deny_transport


class TestGateway:
    def test_local_only_refuses_and_names_the_target(self):
        gw = Gateway()
        with pytest.raises(NetworkDenied, match="musicbrainz"):
            gw.check("https://musicbrainz.org/ws/2")

    def test_default_policy_is_local_only(self):
        """Safe by omission: code that forgets to set a policy cannot reach out."""
        assert Gateway().policy is NetworkPolicy.LOCAL_ONLY

    def test_allowed_policy_permits(self):
        gw = Gateway(policy=NetworkPolicy.ALLOWED)
        gw.check("https://musicbrainz.org")
        assert gw.attempts == ["https://musicbrainz.org"]
        assert gw.denials == []

    def test_the_attempt_survives_a_broad_except(self):
        """The critical property.

        A stage that does `except Exception: pass` around its lookup
        destroys the exception. The record must outlive it, or a test
        cannot tell a denied call from a call that never happened.
        """
        gw = Gateway()
        # Semantically what mb_enrich._search_artist and
        # acousticid._acousticid_lookup do around their network calls.
        with contextlib.suppress(Exception):
            gw.check("https://api.acoustid.org/v2/lookup")
        assert gw.denials == ["https://api.acoustid.org/v2/lookup"]
        assert not gw.clean

    def test_clean_means_nothing_even_tried(self):
        gw = Gateway()
        assert gw.clean
        with pytest.raises(NetworkDenied):
            gw.check("http://example.com")
        assert not gw.clean


class TestTransportDenialHarness:
    def test_a_real_connection_attempt_is_recorded_and_blocked(self):
        with deny_transport() as log, pytest.raises(TransportAttempted):
            socket.create_connection(("musicbrainz.org", 443), timeout=1)
        assert not log.clean
        assert log.attempts[0][0] == "musicbrainz.org"

    def test_an_attempt_swallowed_by_broad_except_is_still_recorded(self):
        """The harness must survive the same swallow the gateway does."""
        with deny_transport() as log, contextlib.suppress(Exception):
            socket.create_connection(("api.acoustid.org", 443), timeout=1)
        assert not log.clean

    def test_doing_nothing_leaves_the_log_clean(self):
        with deny_transport() as log:
            pass
        assert log.clean
        assert log.describe() == "no connection attempted"

    def test_dns_lookup_alone_counts_as_an_attempt(self):
        """DNS happens before connect, and is itself a network call.

        The first version of this harness patched only connect() and so
        recorded a resolved IP while the lookup went out unnoticed.
        """
        with deny_transport() as log, contextlib.suppress(Exception):
            socket.getaddrinfo("musicbrainz.org", 443)
        assert not log.clean
        assert log.attempts[0][0] == "musicbrainz.org"

    def test_sockets_are_restored_afterwards(self):
        before = socket.socket.connect
        before_dns = socket.getaddrinfo
        with deny_transport():
            assert socket.socket.connect is not before
        assert socket.socket.connect is before
        assert socket.getaddrinfo is before_dns


class TestRealStagesRespectThePolicy:
    """The gateway wired into the stages that actually reach out.

    Demonstrated live on 2026-08-23: under LOCAL_ONLY,
    mb_enrich._search_artist swallowed the denial in its broad except and
    returned None, logging "artist search error" -- indistinguishable from
    a genuine not-found. The gateway had still recorded the attempt, which
    is the only reason the difference is observable at all.
    """

    def setup_method(self):
        get_gateway().reset()
        set_policy(NetworkPolicy.LOCAL_ONLY)

    def teardown_method(self):
        get_gateway().reset()
        set_policy(NetworkPolicy.LOCAL_ONLY)

    def test_mb_enrich_attempt_is_recorded_even_though_it_swallows(self):
        from musaeus.stages.mb_enrich import _search_artist

        result = _search_artist("Simon & Garfunkel")
        gw = get_gateway()
        # The stage reports a clean miss...
        assert result is None
        # ...but the policy knows better.
        assert not gw.clean
        assert any("musicbrainz" in a for a in gw.denials)

    def test_a_stage_that_never_looks_up_leaves_the_log_clean(self):
        """Guards against the assertion above passing for the wrong reason."""
        assert get_gateway().clean
