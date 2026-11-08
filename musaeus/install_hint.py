"""How to install a missing outside program, in this system's own words.

Debian gets the apt command it always got. Windows has no apt: preflight
used to print 'sudo apt install ...' there and offer to run it (Windows
port, 2026-11-08).
"""

from __future__ import annotations

import sys

#: The program each Debian package provides, by the name a Windows user searches for.
_WINDOWS_NAME = {
    "ffmpeg": "ffmpeg (it includes ffprobe)",
    "libchromaprint-tools": "Chromaprint's fpcalc",
}


def install_hint(apt_pkg: str) -> str:
    """The fix to print for a missing program, named by its Debian package."""
    if sys.platform == "win32":
        name = _WINDOWS_NAME.get(apt_pkg, apt_pkg)
        return f"download {name} for Windows, unzip it, and add its folder to your user PATH"
    return f"sudo apt install {apt_pkg}"
