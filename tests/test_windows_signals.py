"""Signals Windows does not have (Windows port step 4, 2026-11-08).

Windows has SIGINT and SIGTERM but no SIGHUP, SIGSTOP or SIGCONT. Each test
removes the missing signal from the `signal` module, which is what running
there looks like, and asks the code to work anyway.
"""

from __future__ import annotations

import signal

from musaeus import dedupe, edition_build
from musaeus.idle_throttle import IdleThrottle


def test_the_carry_out_guard_works_without_sighup(monkeypatch):
    monkeypatch.delattr(signal, "SIGHUP", raising=False)
    with dedupe._not_interrupted():
        pass


def test_the_build_stop_handlers_work_without_sighup(monkeypatch):
    monkeypatch.delattr(signal, "SIGHUP", raising=False)
    restore = edition_build._stop_on_signals()
    assert signal.getsignal(signal.SIGTERM) is not signal.SIG_DFL
    restore()


def test_an_idle_throttle_that_never_ran_needs_no_sigcont(monkeypatch):
    monkeypatch.delattr(signal, "SIGCONT", raising=False)
    monkeypatch.setenv("MUSAEUS_NO_IDLE_THROTTLE", "1")
    with IdleThrottle():
        pass
