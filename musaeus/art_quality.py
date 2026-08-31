#!/usr/bin/env python3
"""
MUSAEUS — is the embedded album art actually usable?

`AlbumArtStage` asked one question: is there art? It never asked whether the
art is any good. Measured on the live library 2026-08-31: coverage was
99.9%, and 16 of those files carried images under 300px on the longest edge
-- one at 150x150. Reporting "99.9% have art" was true and hid that.

ORPHEUS asked the second question (SCRIPTS/album_art_audit.py flags anything
under 500x500); this is that idea with the dimension probe done inline, so
no external image library is needed.

Dimensions are read from the JPEG/PNG header rather than by decoding, which
is why this is cheap enough to run over the whole library.
"""

from __future__ import annotations

import struct

# Below this on the longest edge, art looks soft on a phone or car head unit
# and is worth replacing. ORPHEUS used 500x500; the same number is kept so
# the two projects agree on what "too small" means.
MIN_EDGE_PX = 500


def image_dimensions(blob: bytes) -> tuple[int, int] | None:
    """(width, height) from a JPEG or PNG header. None if unrecognised.

    Deliberately header-only: decoding tens of thousands of covers to learn
    their size would cost more than the check is worth.
    """
    if not blob:
        return None

    if blob[:8] == b"\x89PNG\r\n\x1a\n" and len(blob) >= 24:
        w, h = struct.unpack(">II", blob[16:24])
        return int(w), int(h)

    if blob[:2] == b"\xff\xd8":
        i = 2
        n = len(blob)
        while i < n - 9:
            if blob[i] != 0xFF:
                i += 1
                continue
            marker = blob[i + 1]
            # SOF0..SOF15 carry the frame size; skip the ones that do not.
            if marker in (0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7,
                          0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF):
                h, w = struct.unpack(">HH", blob[i + 5:i + 9])
                return int(w), int(h)
            if marker in (0xD8, 0xD9) or 0xD0 <= marker <= 0xD7:
                i += 2
                continue
            if i + 4 > n:
                break
            seg = struct.unpack(">H", blob[i + 2:i + 4])[0]
            if seg < 2:
                break
            i += 2 + seg
    return None


def is_too_small(blob: bytes, min_edge: int = MIN_EDGE_PX) -> bool:
    """True when the art is present but below the usable threshold.

    Unreadable dimensions return False: "cannot tell" is not "too small",
    and flagging on ignorance would send every unusual image for replacement.
    """
    dims = image_dimensions(blob)
    if dims is None:
        return False
    return max(dims) < min_edge


def describe(blob: bytes) -> str:
    """A short human line for reports and logs."""
    if not blob:
        return "no art"
    dims = image_dimensions(blob)
    size_kb = len(blob) // 1024
    if dims is None:
        return f"unreadable header, {size_kb}KB"
    flag = "  TOO SMALL" if max(dims) < MIN_EDGE_PX else ""
    return f"{dims[0]}x{dims[1]}, {size_kb}KB{flag}"
