"""Run only while nobody is using the machine. The "screensaver" gate.

Grey's idea, and MUSAEUS already proved the mechanism: musaeus/idle_throttle.py
pauses a 44-hour Car build the moment the keyboard or mouse is touched, using
the X Screen Saver extension through ctypes so there is nothing to install.

THIS IS A SECOND IMPLEMENTATION AND THAT IS DELIBERATE.
Section 5 of the reconstruction document is largely a catalogue of what happens
when two places hold the same fact, so a copy needs a reason. The reason: this
is a standalone script that must run without MUSAEUS importable, and importing
a pipeline module to make an HTTP call wait would couple a research tool to a
production package for one function. What is copied is 20 lines of ctypes; what
is NOT copied is the SIGSTOP machinery, which exists to freeze ffmpeg children
and has no meaning for a process whose only cost is a network request.

If this file and idle_throttle.py ever disagree about what "idle" means, that
is a real problem -- but they disagree about nothing today, because both ask
the same X extension the same question.

Where the extension cannot be queried -- headless, cron, Wayland without it --
available() is False and the gate disables itself rather than guessing. A run
that would have gone unthrottled still runs.
"""

from __future__ import annotations

import ctypes
import ctypes.util
import os
import time

#: Below this many seconds of inactivity, someone is using the machine.
DEFAULT_IDLE_THRESHOLD = 120.0

#: How long to wait before asking again while the machine is busy.
POLL_SECONDS = 20.0

_DISABLE_VAR = "FMRADIO_NO_IDLE_GATE"


class _XScreenSaverInfo(ctypes.Structure):
    _fields_ = [
        ("window", ctypes.c_ulong),
        ("state", ctypes.c_int),
        ("kind", ctypes.c_int),
        ("since", ctypes.c_ulong),
        ("idle", ctypes.c_ulong),  # milliseconds
        ("event_mask", ctypes.c_ulong),
    ]


class IdleGate:
    """Ask X how long the machine has been untouched, and wait if it hasn't."""

    def __init__(self, threshold: float = DEFAULT_IDLE_THRESHOLD) -> None:
        self.threshold = threshold
        self._xlib = None
        self._xss = None
        self._display = None
        self._info = None
        if os.environ.get(_DISABLE_VAR) == "1":
            return
        self._setup()

    def _setup(self) -> None:
        try:
            xlib_name = ctypes.util.find_library("X11")
            xss_name = ctypes.util.find_library("Xss")
            if not xlib_name or not xss_name:
                return
            xlib = ctypes.cdll.LoadLibrary(xlib_name)
            xss = ctypes.cdll.LoadLibrary(xss_name)
            xlib.XOpenDisplay.restype = ctypes.c_void_p
            display = xlib.XOpenDisplay(None)
            if not display:
                return
            xss.XScreenSaverAllocInfo.restype = ctypes.POINTER(_XScreenSaverInfo)
            info = xss.XScreenSaverAllocInfo()
            xlib.XDefaultRootWindow.restype = ctypes.c_ulong
            xlib.XDefaultRootWindow.argtypes = [ctypes.c_void_p]
            self._xlib, self._xss, self._display, self._info = xlib, xss, display, info
        except Exception:
            # Any failure here means "cannot ask", which is not an error --
            # it is the headless case, and the gate simply disables itself.
            self._xlib = self._xss = self._display = self._info = None

    def available(self) -> bool:
        return self._info is not None

    def idle_seconds(self) -> float | None:
        """Seconds since the last keyboard or mouse event, or None if unknown."""
        if not self.available():
            return None
        try:
            root = self._xlib.XDefaultRootWindow(self._display)
            self._xss.XScreenSaverQueryInfo(
                ctypes.c_void_p(self._display), ctypes.c_ulong(root), self._info
            )
            return self._info.contents.idle / 1000.0
        except Exception:
            return None

    def wait_until_idle(self, log=None) -> None:
        """Block until the machine has been untouched for `threshold` seconds.

        A no-op where idle cannot be measured, so a cron run is not a run that
        waits for ever on a machine with no X display.
        """
        if not self.available():
            return
        announced = False
        while True:
            idle = self.idle_seconds()
            if idle is None or idle >= self.threshold:
                if announced and log:
                    log("machine idle again, resuming")
                return
            if not announced and log:
                log(
                    f"someone is using the machine (idle {idle:.0f}s < "
                    f"{self.threshold:.0f}s) -- pausing"
                )
                announced = True
            time.sleep(POLL_SECONDS)
