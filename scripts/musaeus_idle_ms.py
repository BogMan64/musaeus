#!/usr/bin/env python3
"""
Print milliseconds since the last keyboard/mouse input on the X11 session
named by $DISPLAY, using the XScreenSaver extension (the same mechanism
xprintidle uses) via python3-xlib -- no extra system package required.

Exits non-zero with nothing on stdout if the X session isn't reachable
(wrong DISPLAY, no X server, extension unavailable), so callers can treat
that the same way they'd treat any other "can't determine, assume busy"
case rather than parsing a bogus value.
"""

from __future__ import annotations

import sys


def main() -> int:
    try:
        from Xlib import display
    except ImportError:
        print("error: python3-xlib not installed", file=sys.stderr)
        return 1

    try:
        d = display.Display()
        root = d.screen().root
        info = root.screensaver_query_info()
        print(info.idle)
        return 0
    except Exception as exc:  # noqa: BLE001 -- any X11 failure means "can't determine"
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
