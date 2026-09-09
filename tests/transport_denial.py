"""
Transport-denial harness for preview tests (P0-05).

Not a test module -- a fixture API, deliberately named so pytest does not
collect it.

Why it patches socket.connect rather than using an audit hook: several
stages already wrap their network calls in `except Exception`
(mb_enrich._search_artist, acousticid._acousticid_lookup). A hook-raised
exception is swallowed there before a test can observe it, and the stage
reports a clean miss -- indistinguishable from "not found". Patching
connect() raises the same exception those excepts still catch, but records
the attempt first, so the evidence survives the swallow.

That distinction is the whole point. "No exception escaped" is not the
same claim as "nothing tried to connect", and only the second one is worth
asserting.
"""

from __future__ import annotations

import socket
from contextlib import contextmanager
from dataclasses import dataclass, field


class TransportAttempted(RuntimeError):
    """Raised at the socket layer when a test forbids transport."""


@dataclass
class TransportLog:
    attempts: list[tuple[str, int]] = field(default_factory=list)

    @property
    def clean(self) -> bool:
        return not self.attempts

    def describe(self) -> str:
        if self.clean:
            return "no connection attempted"
        return "; ".join(f"{h}:{p}" for h, p in self.attempts)


@contextmanager
def deny_transport():
    """Forbid outbound connections and record every attempt.

    Yields a TransportLog. Assert `log.clean` -- not merely that no
    exception surfaced, which a broad `except` would render meaningless.

    Covers getaddrinfo as well as connect: DNS is a network call, and it
    happens first.
    """
    log = TransportLog()
    real_connect = socket.socket.connect
    real_connect_ex = socket.socket.connect_ex
    real_getaddrinfo = socket.getaddrinfo

    def _record(addr):
        try:
            host, port = addr[0], addr[1]
        except (TypeError, IndexError):
            host, port = str(addr), 0
        log.attempts.append((host, port))

    def fake_connect(self, addr):
        _record(addr)
        raise TransportAttempted(f"connection to {addr} denied by test harness")

    def fake_connect_ex(self, addr):
        _record(addr)
        raise TransportAttempted(f"connection to {addr} denied by test harness")

    def fake_getaddrinfo(host, port, *a, **kw):
        # DNS resolution is itself a network call, and socket.create_connection
        # performs it BEFORE connect() -- so a harness that patches only
        # connect records the resolved IP and misses the lookup entirely.
        # Found while writing these tests: the first version asserted on
        # "musicbrainz.org" and got "2a01:4f8:c011:f68::1".
        log.attempts.append((str(host), int(port) if isinstance(port, int) else 0))
        raise TransportAttempted(f"DNS lookup for {host!r} denied by test harness")

    socket.socket.connect = fake_connect  # type: ignore[method-assign]
    socket.socket.connect_ex = fake_connect_ex  # type: ignore[method-assign]
    socket.getaddrinfo = fake_getaddrinfo  # type: ignore[assignment]
    try:
        yield log
    finally:
        socket.socket.connect = real_connect  # type: ignore[method-assign]
        socket.socket.connect_ex = real_connect_ex  # type: ignore[method-assign]
        socket.getaddrinfo = real_getaddrinfo  # type: ignore[assignment]
