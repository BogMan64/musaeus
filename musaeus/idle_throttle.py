#!/usr/bin/env python3
"""
MUSAEUS — yield the machine while someone is using it.

A full Car edition is ~44 hours of ffmpeg across 10,545 files, and it
drives load past 9 on an 8-core box. That is fine at 3am and miserable at
3pm, so long builds pause the moment the keyboard or mouse is touched and
resume once the machine goes quiet again.

SIGSTOP rather than `nice`: a reniced encode still competes for all eight
cores and still makes the desktop stutter. Stopping gives the machine back
completely. ffmpeg resumes mid-file with no loss -- SIGSTOP freezes the
process, it does not interrupt or truncate a write.

Only the ffmpeg children are stopped, never the coordinating process: it
has to stay alive to notice when the machine goes quiet again.

Idle time comes from the X Screen Saver extension through ctypes, so there
is nothing to install. Anywhere that cannot be queried -- a headless box,
a cron run, Wayland without the extension -- `available()` is False and
the throttle disables itself rather than guessing. A build that would have
run unthrottled still runs.

On by default. `MUSAEUS_NO_IDLE_THROTTLE=1` turns it off.
"""

from __future__ import annotations

import contextlib
import ctypes
import ctypes.util
import logging
import os
import signal
import subprocess
import threading
import time

logger = logging.getLogger(__name__)

#: Quiet period before work resumes. Long enough that a pause between
#: keystrokes does not restart the encode in someone's face.
DEFAULT_IDLE_S = 40.0
_POLL_S = 2.0
_WATCH = ("ffmpeg", "ffprobe", "fpcalc")


class _XIdle:
    """Idle milliseconds from the X Screen Saver extension."""

    class _Info(ctypes.Structure):
        _fields_ = [
            ("window", ctypes.c_ulong), ("state", ctypes.c_int),
            ("kind", ctypes.c_int), ("til_or_since", ctypes.c_ulong),
            ("idle", ctypes.c_ulong), ("eventMask", ctypes.c_ulong),
        ]

    def __init__(self) -> None:
        self._ok = False
        try:
            x11n = ctypes.util.find_library("X11")
            xssn = ctypes.util.find_library("Xss")
            if not (x11n and xssn):
                return
            self._x11 = ctypes.CDLL(x11n)
            self._xss = ctypes.CDLL(xssn)
            self._x11.XOpenDisplay.restype = ctypes.c_void_p
            self._dpy = self._x11.XOpenDisplay(
                os.environ.get("DISPLAY", ":0").encode()
            )
            if not self._dpy:
                return
            self._xss.XScreenSaverAllocInfo.restype = ctypes.POINTER(self._Info)
            self._info = self._xss.XScreenSaverAllocInfo()
            self._root = self._x11.XDefaultRootWindow(ctypes.c_void_p(self._dpy))
            self.idle_ms()
            self._ok = True
        except Exception:
            self._ok = False

    def close(self) -> None:
        """Release the X11 connection this instance opened.

        XOpenDisplay is never implicitly closed by process exit cleanup
        in a way that matters here -- see the module-level SHARED_XIDLE
        below for why this must be called at most once per Python
        process, not once per idle check.
        """
        if getattr(self, "_ok", False) and getattr(self, "_dpy", None):
            with contextlib.suppress(Exception):
                self._x11.XCloseDisplay(ctypes.c_void_p(self._dpy))
            self._ok = False

    @property
    def available(self) -> bool:
        return self._ok

    def idle_ms(self) -> int:
        self._xss.XScreenSaverQueryInfo(
            ctypes.c_void_p(self._dpy), ctypes.c_ulong(self._root), self._info
        )
        return int(self._info.contents.idle)


def _descendants(pid: int) -> list[int]:
    """Every descendant of *pid*, recollected each cycle because a worker
    pool starts new ffmpeg children continuously."""
    try:
        out = subprocess.run(
            ["ps", "-eo", "pid,ppid,comm"], capture_output=True, text=True, check=True
        ).stdout
    except Exception:
        return []
    kids: dict[int, list[int]] = {}
    names: dict[int, str] = {}
    for line in out.splitlines()[1:]:
        parts = line.split(None, 2)
        if len(parts) == 3 and parts[0].isdigit() and parts[1].isdigit():
            kids.setdefault(int(parts[1]), []).append(int(parts[0]))
            names[int(parts[0])] = parts[2].strip()
    found, stack = [], list(kids.get(pid, []))
    while stack:
        p = stack.pop()
        if any(w in names.get(p, "") for w in _WATCH):
            found.append(p)
        stack.extend(kids.get(p, []))
    return found


# A fresh _XIdle() calls XOpenDisplay, which opens a real X11 socket that
# is never closed anywhere on this path -- only IdleThrottle.__enter__
# constructed one per throttle CONTEXT and reused it for that context's
# lifetime, which is why that path never leaked.
#
# is_idle()/idle_seconds()/available() are meant to be called in a POLLING
# LOOP -- deep_scan.py's own resume loop calls is_idle() every 5 seconds
# for as long as the machine is in use, which can be hours. Each of those
# three functions used to construct its own _XIdle() per call, leaking one
# X11 connection every single time.
#
# Measured 2026-09-03: deep_scan, left running unattended, leaked a
# connection every 5 s and exhausted the X server's MaxClients ceiling in
# roughly 20 minutes -- at which point EVERY X client on the machine,
# including Grey's own desktop, started failing with "Maximum number of
# clients reached". `xdpyinfo` itself could not connect. Confirmed the
# leak was the sole cause: killing the one leaking process (which frees
# all its file descriptors, X sockets included, on exit) restored the X
# server immediately with no other intervention.
#
# One shared instance for the life of the process, not one per call.
_SHARED_XIDLE: _XIdle | None = None


def _shared_xidle() -> _XIdle:
    global _SHARED_XIDLE
    if _SHARED_XIDLE is None:
        _SHARED_XIDLE = _XIdle()
    return _SHARED_XIDLE


def available() -> bool:
    """True when idle can actually be measured and throttling is wanted."""
    if os.environ.get("MUSAEUS_NO_IDLE_THROTTLE"):
        return False
    return _shared_xidle().available


class IdleThrottle:
    """Pause this process's encoder children while the machine is in use.

    Used as a context manager around long work. Does nothing at all when
    idle cannot be measured, so callers need no conditional.
    """

    def __init__(self, idle_s: float = DEFAULT_IDLE_S) -> None:
        self.idle_s = idle_s
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._paused = False
        self.pause_count = 0
        # Cumulative seconds spent deliberately stopped.
        #
        # A caller that puts a timeout on a child needs this: SIGSTOP time
        # is wall-clock time in which the child was FORBIDDEN to work, and
        # counting it towards a hang deadline kills exactly the work the
        # throttle is protecting. The deadline has to measure working
        # time, and only this class knows the difference.
        self._paused_total = 0.0
        self._paused_since: float | None = None
        self._paused_lock = threading.Lock()

    def __enter__(self) -> IdleThrottle:
        if os.environ.get("MUSAEUS_NO_IDLE_THROTTLE"):
            logger.info("[idle] disabled by MUSAEUS_NO_IDLE_THROTTLE")
            return self
        probe = _XIdle()
        if not probe.available:
            logger.info("[idle] no X idle source — running at full speed")
            return self
        self._thread = threading.Thread(
            target=self._run, args=(probe,), daemon=True, name="idle-throttle"
        )
        self._thread.start()
        logger.info("[idle] throttle active — pausing while in use, "
                    "resuming after %.0fs quiet", self.idle_s)
        return self

    def __exit__(self, *exc) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=5)
        # Never leave children frozen because the build ended mid-pause.
        self._signal(signal.SIGCONT)
        self._mark_paused(False)
        return None

    @property
    def paused_seconds(self) -> float:
        """Seconds this throttle has held children stopped, so far."""
        with self._paused_lock:
            live = (time.monotonic() - self._paused_since) if self._paused_since else 0.0
            return self._paused_total + live

    def _mark_paused(self, paused: bool) -> None:
        with self._paused_lock:
            if paused and self._paused_since is None:
                self._paused_since = time.monotonic()
            elif not paused and self._paused_since is not None:
                self._paused_total += time.monotonic() - self._paused_since
                self._paused_since = None

    def _signal(self, sig) -> int:
        pids = _descendants(os.getpid())
        # Stop deepest-first so a parent cannot spawn past a frozen child.
        n = 0
        for p in (reversed(pids) if sig == signal.SIGSTOP else pids):
            try:
                os.kill(p, sig)
                n += 1
            except (ProcessLookupError, PermissionError):
                pass
        return n

    def _run(self, probe: _XIdle) -> None:
        while not self._stop.is_set():
            try:
                busy = probe.idle_ms() < self.idle_s * 1000
            except Exception:
                break
            if busy and not self._paused:
                n = self._signal(signal.SIGSTOP)
                if n:
                    self._paused = True
                    self.pause_count += 1
                    self._mark_paused(True)
                    logger.info("[idle] paused %d encoder process(es) — machine in use", n)
            elif not busy and self._paused:
                n = self._signal(signal.SIGCONT)
                self._paused = False
                self._mark_paused(False)
                logger.info("[idle] resumed %d encoder process(es)", n)
            self._stop.wait(_POLL_S)
        if self._paused:
            self._signal(signal.SIGCONT)
            self._paused = False
            self._mark_paused(False)


# ── Idle-only work ────────────────────────────────────────────────────────────
#
# The throttle above pauses work while the machine is in use. This is the
# inverse: work that runs ONLY while the machine is idle and yields the
# instant someone touches it.
#
# The distinction matters for a job with no deadline. A full decode of
# 10,446 masters is hours of CPU that nobody is waiting on, so it should
# never compete for the machine at all -- not "run more politely", but
# "do not exist while Grey is here". Anything using this must be resumable,
# because it will be interrupted constantly and may take days of real time
# to finish. That is fine; it is looking for damage that has already
# happened.


def is_idle(threshold_s: float = DEFAULT_IDLE_S) -> bool:
    """True when the machine has been untouched for *threshold_s*.

    False wherever idle cannot be measured -- headless, cron, Wayland
    without the extension. Idle-ONLY work must not run when it cannot tell,
    which is the opposite of the throttle's default: the throttle runs at
    full speed when blind, this stays stopped.

    Uses the one shared _XIdle connection (see _shared_xidle) rather than
    opening a new X11 connection on every call -- this is meant to be
    polled every few seconds for hours by deep_scan's resume loop.
    """
    probe = _shared_xidle()
    if not probe.available:
        return False
    try:
        return probe.idle_ms() >= threshold_s * 1000
    except Exception:
        return False


def idle_seconds() -> float | None:
    """Seconds since the last input, or None when unmeasurable.

    Uses the shared _XIdle connection -- see is_idle's docstring.
    """
    probe = _shared_xidle()
    if not probe.available:
        return None
    try:
        return probe.idle_ms() / 1000.0
    except Exception:
        return None
