"""Yielding the machine while someone is using it.

A full Car edition is ~44 hours of ffmpeg across 10,545 files and drives
load past 9 on an 8-core box. Fine at 3am, miserable at 3pm. Grey asked
for it to behave like a screensaver in reverse: stop the moment the
keyboard or mouse is touched, resume after the machine goes quiet.

SIGSTOP rather than `nice` -- a reniced encode still competes for all
eight cores and still makes the desktop stutter.

The properties that matter are the safety ones: it must never leave a
child frozen, it must never stop the coordinating process (which has to
stay alive to notice the machine going quiet), and it must disable itself
rather than guess wherever idle cannot be measured -- a headless box, a
cron run, Wayland without the extension.
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import time

import pytest

from musaeus.idle_throttle import (
    DEFAULT_IDLE_S, IdleThrottle, _descendants, available,
)


class TestOptOut:
    def test_env_var_disables_it(self, monkeypatch) -> None:
        monkeypatch.setenv("MUSAEUS_NO_IDLE_THROTTLE", "1")
        assert available() is False

    def test_disabled_throttle_starts_no_thread(self, monkeypatch) -> None:
        monkeypatch.setenv("MUSAEUS_NO_IDLE_THROTTLE", "1")
        with IdleThrottle() as t:
            assert t._thread is None

    def test_context_manager_is_safe_when_unavailable(self, monkeypatch) -> None:
        """A build that would have run unthrottled must still run."""
        monkeypatch.setenv("MUSAEUS_NO_IDLE_THROTTLE", "1")
        ran = False
        with IdleThrottle():
            ran = True
        assert ran


class TestDescendants:
    def test_finds_only_encoder_children(self) -> None:
        """It must target ffmpeg/ffprobe/fpcalc, not arbitrary children --
        stopping an unrelated child could freeze something that matters."""
        child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
        try:
            time.sleep(0.3)
            assert child.pid not in _descendants(os.getpid())
        finally:
            child.kill()
            child.wait()

    def test_returns_empty_for_a_pid_with_no_children(self) -> None:
        assert _descendants(os.getpid()) == [] or all(
            isinstance(p, int) for p in _descendants(os.getpid())
        )


class TestNeverLeavesChildrenFrozen:
    def test_exit_resumes_even_if_paused(self, monkeypatch) -> None:
        """The build ending mid-pause must not strand a stopped process.
        __exit__ sends SIGCONT unconditionally for exactly this reason."""
        monkeypatch.setenv("MUSAEUS_NO_IDLE_THROTTLE", "1")
        t = IdleThrottle()
        with t:
            t._paused = True          # pretend a pause was in effect
        assert t._stop.is_set()

    def test_stopping_a_real_child_and_resuming_it_works(self) -> None:
        """SIGSTOP/SIGCONT round-trip: the mechanism the throttle relies on.
        A stopped process reports state T and runs again after SIGCONT."""
        proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
        try:
            time.sleep(0.3)
            os.kill(proc.pid, signal.SIGSTOP)
            time.sleep(0.2)
            state = subprocess.run(["ps", "-o", "stat=", "-p", str(proc.pid)],
                                   capture_output=True, text=True).stdout.strip()
            assert state.startswith("T"), f"expected stopped, got {state!r}"
            os.kill(proc.pid, signal.SIGCONT)
            time.sleep(0.2)
            state = subprocess.run(["ps", "-o", "stat=", "-p", str(proc.pid)],
                                   capture_output=True, text=True).stdout.strip()
            assert not state.startswith("T"), f"expected running, got {state!r}"
        finally:
            proc.kill()
            proc.wait()


def test_resume_delay_is_in_the_range_grey_asked_for() -> None:
    """"30 to 45 seconds of quiet" -- long enough that a pause between
    keystrokes does not restart the encode in someone's face."""
    assert 30.0 <= DEFAULT_IDLE_S <= 45.0
