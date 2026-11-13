"""Windows refuses a full path of 260 characters or more unless "Win32 long
paths" is turned on, which needs admin. It was off on Grey's work PC
(2026-11-12), where a long song name in a deep folder failed to save.
"""

from __future__ import annotations

import sys

#: MAX_PATH is 260 counting the closing null, so 259 characters fit.
MAX_PATH_CHARS = 259

LONG_PATHS_OFF = (
    "Windows long paths: OFF -- a full path of 260 characters or more cannot be saved. "
    "MUSAEUS shortens song names to fit; a folder that is itself too long still fails. "
    "Fix (needs admin): turn on 'Enable Win32 long paths' in Group Policy, or set "
    r"LongPathsEnabled=1 in HKLM\SYSTEM\CurrentControlSet\Control\FileSystem"
)


def long_paths_on() -> bool | None:
    """Whether Windows long paths are on. None when this is not Windows."""
    if sys.platform != "win32":
        return None
    import winreg

    try:
        with winreg.OpenKey(
            winreg.HKEY_LOCAL_MACHINE, r"SYSTEM\CurrentControlSet\Control\FileSystem"
        ) as key:
            return bool(winreg.QueryValueEx(key, "LongPathsEnabled")[0] == 1)
    except OSError:
        return False
