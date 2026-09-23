#!/usr/bin/env python3
"""
MUSAEUS — write recording identity into the FILES, not just the database.

Every MBID this project has ever fetched lives in musaeus.db and nowhere
else. Nothing in tagger.py, forge.py or tags.py writes MusicBrainz or
AcoustID identifiers to disk -- 806 artist MBIDs were resolved in a single
batch on 2026-08-26, all of them database-only. Audit's own docstring
calls snapshot-and-wipe of musaeus.db the design intent, and this project
asserts everywhere else that the FILE is the durable record. Identity was
the exception.

Tag names follow MusicBrainz Picard, which is what every other tool in
this space reads and writes. Deviating would make the tags unreadable by
the software most likely to consume them.

The trap this module exists to avoid
------------------------------------
forge.py:73 documents silent-no-op #2 in full: a dotted key
"com.apple.iTunes.R128_TRACK_GAIN" is accepted by mutagen's MP4 as a dict
key but cannot be serialised -- it is neither a 4-character atom nor a
freeform atom -- so save() succeeded, returned True, and wrote nothing.
Not one M4A carried the tag despite 12,279 recorded FORGE_TAG events.

So every write here is verified by READING THE VALUE BACK OFF DISK, in a
fresh handle, and comparing it. A writer that reports success without
re-reading is exactly the check that cannot fail.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# db column -> Picard tag name
IDENTITY_FIELDS: dict[str, str] = {
    "mb_artist_id": "MusicBrainz Artist Id",
    "mb_release_id": "MusicBrainz Album Id",
    "acousticid_recording": "Acoustid Id",
    "chromaprint": "Acoustid Fingerprint",
    # Not a Picard field. Ours, and load-bearing: a fingerprint describes
    # the PCM, so on rebuild we must be able to ask whether it still
    # describes THIS audio. Without the duration alongside it there is no
    # way to tell a valid fingerprint from one that outlived its file.
    "chromaprint_duration": "Acoustid Fingerprint Duration",
}

# Vorbis/FLAC uses its own upper-case convention, not the Picard label.
_VORBIS = {
    "mb_artist_id": "MUSICBRAINZ_ARTISTID",
    "mb_release_id": "MUSICBRAINZ_ALBUMID",
    "acousticid_recording": "ACOUSTID_ID",
    "chromaprint": "ACOUSTID_FINGERPRINT",
    "chromaprint_duration": "ACOUSTID_FINGERPRINT_DURATION",
}


# The two maps describe the same fields in two container conventions, so a
# field added to one and not the other is a silent trap: _write_flac would
# raise KeyError, write_identity's broad except would turn it into
# (False, "'col'"), the marker would stay NULL, and that row would retry
# for ever at warning level -- work removed from the queue by a typo.
# Structural, so it cannot drift.
assert IDENTITY_FIELDS.keys() == _VORBIS.keys(), (
    "IDENTITY_FIELDS and _VORBIS must describe the same fields: "
    f"{IDENTITY_FIELDS.keys() ^ _VORBIS.keys()}"
)


def _m4a_key(tag_name: str) -> str:
    """The freeform atom form. NEVER the dotted form -- see module docstring."""
    return f"----:com.apple.iTunes:{tag_name}"


def write_identity(path: Path, values: dict[str, str]) -> tuple[bool, str]:
    """Write identity tags and prove they landed.

    `values` maps db column name -> value. Returns (ok, detail); ok is True
    only when every written tag was read back off disk and matched.
    """
    if not values:
        return True, "nothing to write"
    suffix = path.suffix.lower()
    try:
        if suffix in (".m4a", ".mp4", ".m4b"):
            return _write_m4a(path, values)
        if suffix == ".flac":
            return _write_flac(path, values)
    except Exception as exc:  # a tagging failure must not kill the run
        logger.debug("identity tag write failed %s: %s", path, exc)
        return False, str(exc)
    return False, f"unsupported container {suffix}"


def _write_m4a(path: Path, values: dict[str, str]) -> tuple[bool, str]:
    from mutagen.mp4 import MP4, MP4FreeForm  # type: ignore[import-untyped]

    audio: Any = MP4(str(path))
    if audio.tags is None:
        audio.add_tags()
    for col, val in values.items():
        audio.tags[_m4a_key(IDENTITY_FIELDS[col])] = [MP4FreeForm(str(val).encode("utf-8"))]
    audio.save()

    # Read back in a FRESH handle. Re-reading the object we just wrote
    # would pass on the in-memory dict and prove nothing about the file.
    check: Any = MP4(str(path))
    for col, val in values.items():
        got = (check.tags or {}).get(_m4a_key(IDENTITY_FIELDS[col]))
        if not got or bytes(got[0]).decode("utf-8") != str(val):
            return False, f"{IDENTITY_FIELDS[col]} did not survive the write"
    return True, f"{len(values)} tag(s) verified on disk"


def _write_flac(path: Path, values: dict[str, str]) -> tuple[bool, str]:
    from mutagen.flac import FLAC  # type: ignore[import-untyped]

    audio = FLAC(str(path))
    for col, val in values.items():
        audio[_VORBIS[col]] = [str(val)]
    audio.save()

    check = FLAC(str(path))
    for col, val in values.items():
        got = check.get(_VORBIS[col])
        if not got or got[0] != str(val):
            return False, f"{_VORBIS[col]} did not survive the write"
    return True, f"{len(values)} tag(s) verified on disk"


def read_identity(path: Path) -> dict[str, str]:
    """What identity tags does this file actually carry? For verification."""
    suffix = path.suffix.lower()
    out: dict[str, str] = {}
    try:
        if suffix in (".m4a", ".mp4", ".m4b"):
            from mutagen.mp4 import MP4  # type: ignore[import-untyped]

            tags: Any = MP4(str(path)).tags or {}
            for col, name in IDENTITY_FIELDS.items():
                got = tags.get(_m4a_key(name))
                if got:
                    out[col] = bytes(got[0]).decode("utf-8", "replace")
        elif suffix == ".flac":
            from mutagen.flac import FLAC  # type: ignore[import-untyped]

            audio = FLAC(str(path))
            for col, name in _VORBIS.items():
                got = audio.get(name)
                if got:
                    out[col] = got[0]
    except Exception as exc:
        logger.debug("identity tag read failed %s: %s", path, exc)
    return out
