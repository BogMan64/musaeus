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


def available() -> bool:
    """True when idle can actually be measured and throttling is wanted."""
    if os.environ.get("MUSAEUS_NO_IDLE_THROTTLE"):
        return False
    return _XIdle().available


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
        return None

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
                    logger.info("[idle] paused %d encoder process(es) — machine in use", n)
            elif not busy and self._paused:
                n = self._signal(signal.SIGCONT)
                self._paused = False
                logger.info("[idle] resumed %d encoder process(es)", n)
            self._stop.wait(_POLL_S)
        if self._paused:
            self._signal(signal.SIGCONT)
            self._paused = False
