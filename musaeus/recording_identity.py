"""Is the file at a path the recording a catalogue row names?

Shared by the scripts that change or remove a master: a file that cannot be
confirmed as the row's recording is never touched (scripts/delete_reviewed_tracks.py,
scripts/musaeus_purge_multichannel.py).
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path


def not_this_recording(p: Path, row: Mapping, h0: str | None) -> str:
    """Why the file at *p* cannot be confirmed as the recording *row* names,
    or "" when it is. Anything that cannot be read counts as not confirmed:
    a deletion that cannot confirm its target does not happen (--force-id)."""
    from musaeus.hasher import audio_hash

    if not h0:
        return "the row has no audio hash to confirm it by"
    cols = set(row.keys())
    try:
        if audio_hash(p, strict=True) == h0:
            return ""
    except Exception as exc:  # noqa: BLE001 - unreadable is "not confirmed"
        return f"its audio could not be read ({type(exc).__name__}: {exc})"
    if (row["canon_action"] if "canon_action" in cols else None) != "TRANSCODED":
        return "its audio is not this track's (audio hash differs)"
    stored = row["chromaprint"] if "chromaprint" in cols else None
    if not stored:
        return "a re-encoded master, and no stored fingerprint to confirm it by"
    try:
        from musaeus.stages.acousticid import _fpcalc

        if _fpcalc(str(p))[1] == stored:
            return ""
    except Exception as exc:  # noqa: BLE001
        return f"a re-encoded master whose fingerprint could not be taken ({type(exc).__name__}: {exc})"
    return "a re-encoded master whose fingerprint is not this track's"
