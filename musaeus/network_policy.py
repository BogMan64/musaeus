#!/usr/bin/env python3
"""
MUSAEUS — Network policy gateway (P0-05).

Preview must be local-only: it may not dispatch transport. Enforcing that
by auditing call sites does not hold, because the set of call sites
changes -- mb_enrich, various_artists_fix and acousticid each reach the
network today and nothing stops a fourth being added tomorrow without
anyone remembering this rule.

So the policy is a gateway rather than a convention. Code asks permission
before connecting, and under LOCAL_ONLY the answer is no.

One detail that matters, learned from the disposable-vault work: several
stages wrap their network calls in `except Exception`. mb_enrich._search_artist
and acousticid._acousticid_lookup both do. A policy that raises a custom
exception is therefore SWALLOWED at the call site and the stage carries on
reporting a clean miss -- indistinguishable from "the artist wasn't found".
That is exactly the silent-no-op shape this project has been bitten by five
times.

Two things follow:

  1. Every denial is RECORDED before it is raised. The record survives the
     broad except, so a test can assert "no connection was attempted" even
     when the stage swallows the exception.
  2. The denial is reported as an attempt, not a failure -- because from
     the policy's point of view, code trying to reach the network during a
     preview is the finding, whether or not the caller noticed.
"""

from __future__ import annotations

import threading
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from enum import Enum


class NetworkPolicy(Enum):
    LOCAL_ONLY = "local-only"  # preview and any unattended default
    ALLOWED = "allowed"  # explicit, execute-mode only


class NetworkDenied(RuntimeError):
    """Raised when code attempts transport under LOCAL_ONLY."""


@dataclass
class Gateway:
    """Holds the policy and records every attempt made through it."""

    policy: NetworkPolicy = NetworkPolicy.LOCAL_ONLY
    attempts: list[str] = field(default_factory=list)
    denials: list[str] = field(default_factory=list)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def check(self, target: str) -> None:
        """Ask permission to reach *target*. Raises under LOCAL_ONLY.

        The attempt is recorded BEFORE raising, so a caller that wraps this
        in `except Exception` cannot erase the evidence -- see the module
        docstring.
        """
        with self._lock:
            self.attempts.append(target)
            if self.policy is NetworkPolicy.LOCAL_ONLY:
                self.denials.append(target)
        if self.policy is NetworkPolicy.LOCAL_ONLY:
            raise NetworkDenied(
                f"network access to {target!r} refused: policy is local-only. "
                "Preview never performs external lookups."
            )

    @property
    def clean(self) -> bool:
        """True when nothing even tried to reach the network."""
        return not self.attempts

    def reset(self) -> None:
        with self._lock:
            self.attempts.clear()
            self.denials.clear()


#: Process-wide default. Local-only, so anything that forgets to ask is
#: safe by omission rather than dangerous by omission.
_gateway = Gateway()


def get_gateway() -> Gateway:
    return _gateway


def set_policy(policy: NetworkPolicy) -> None:
    _gateway.policy = policy


def check(target: str) -> None:
    """Module-level convenience for call sites."""
    _gateway.check(target)


@contextmanager
def policy(policy: NetworkPolicy) -> Iterator[Gateway]:
    """Set the process-wide policy for a block, and restore it after.

    The gateway is module-level mutable state, which means a caller that
    sets ALLOWED and does not put it back leaves *everything afterwards in
    the process* running permissive -- including, in a test session, every
    test that happens to run later. That is not hypothetical: a P0-14 test
    read the policy and so passed in isolation and failed in the full
    suite, which was the leak showing itself.

    `set_policy` is kept for callers that genuinely want a lasting change,
    but anything scoped should use this instead, because "restore it
    afterwards" is exactly the discipline that gets skipped on the error
    path.
    """
    previous = _gateway.policy
    _gateway.policy = policy
    try:
        yield _gateway
    finally:
        _gateway.policy = previous
