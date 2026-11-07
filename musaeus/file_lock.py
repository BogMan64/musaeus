"""One advisory lock on a whole file, for Linux and Windows (Windows port, 2026-11-07).

Every lock in MUSAEUS asks without waiting and relies on the operating system
to free it when the process dies, however it dies. On Linux that is
`fcntl.flock`, exactly as before. Windows has no `fcntl`; there it is
`LockFileEx`, whose lock dies with its handle.

Two Windows differences, handled here so no caller has to know:

- A Windows lock is mandatory for the bytes it covers: another handle could
  not even read them. MUSAEUS writes a pid into some lock files and reads
  them back, so the lock covers one byte far past the end of any real file
  instead of the content.
- `try_lock` raises `BlockingIOError` when the lock is taken, as
  `flock(LOCK_NB)` does, so the callers' `except BlockingIOError` /
  `except OSError` stay as they are.
"""

from __future__ import annotations

import sys
from typing import IO, Any

FileLike = int | IO[Any]

if sys.platform == "win32":
    import ctypes
    import msvcrt
    from ctypes import wintypes

    class _Overlapped(ctypes.Structure):
        _fields_ = [
            ("Internal", ctypes.c_void_p),
            ("InternalHigh", ctypes.c_void_p),
            ("Offset", wintypes.DWORD),
            ("OffsetHigh", wintypes.DWORD),
            ("hEvent", wintypes.HANDLE),
        ]

    _KERNEL32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _KERNEL32.LockFileEx.argtypes = [
        wintypes.HANDLE,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.DWORD,
        ctypes.POINTER(_Overlapped),
    ]
    _KERNEL32.LockFileEx.restype = wintypes.BOOL
    _KERNEL32.UnlockFileEx.argtypes = [
        wintypes.HANDLE,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.DWORD,
        ctypes.POINTER(_Overlapped),
    ]
    _KERNEL32.UnlockFileEx.restype = wintypes.BOOL

    _FAIL_IMMEDIATELY = 0x1
    _EXCLUSIVE = 0x2
    _BUSY = {32, 33}  # ERROR_SHARING_VIOLATION, ERROR_LOCK_VIOLATION
    # One byte at offset 2**63 - 1: past the end of any real file.
    _AT_LOW, _AT_HIGH = 0xFFFFFFFF, 0x7FFFFFFF

    def _region() -> _Overlapped:
        o = _Overlapped()
        o.Offset, o.OffsetHigh = _AT_LOW, _AT_HIGH
        return o

    def _handle(f: FileLike) -> int:
        return msvcrt.get_osfhandle(f if isinstance(f, int) else f.fileno())

    def try_lock(f: FileLike, *, exclusive: bool) -> None:
        """Take the lock now, or raise BlockingIOError if another holder has it."""
        flags = _FAIL_IMMEDIATELY | (_EXCLUSIVE if exclusive else 0)
        if not _KERNEL32.LockFileEx(_handle(f), flags, 0, 1, 0, ctypes.byref(_region())):
            err = ctypes.get_last_error()
            if err in _BUSY:
                raise BlockingIOError(err, "the lock is held by another process")
            raise ctypes.WinError(err)

    def unlock(f: FileLike) -> None:
        """Give the lock back."""
        if not _KERNEL32.UnlockFileEx(_handle(f), 0, 1, 0, ctypes.byref(_region())):
            raise ctypes.WinError(ctypes.get_last_error())

else:
    import fcntl

    def try_lock(f: FileLike, *, exclusive: bool) -> None:
        """Take the lock now, or raise BlockingIOError if another holder has it."""
        fcntl.flock(f, (fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH) | fcntl.LOCK_NB)

    def unlock(f: FileLike) -> None:
        """Give the lock back."""
        fcntl.flock(f, fcntl.LOCK_UN)
