"""Which of two copies of one song to keep: Grey's rule of 2026-10-03.

Grey, on the duplicate-review list: "keep the copy that is ALAC over FLAC,
then studio over live, then quality, then length". Asked the same day and
answered yes to each reading:

  1. format          ALAC, then FLAC, then any other lossless, then lossy
                     (a copy whose codec is unknown is never taken for lossless)
  1b. baked          an original beats a copy the retired bake made at
                     -18 LUFS (Grey, 2026-09-26; the resolver's own step,
                     joined to this rule 2026-10-05 so there is ONE rule)
  2. studio / live   a studio recording beats a live one
  3. original        the original beats a remaster, remix or re-recording --
                     Grey's standing ruling ("the original trumps the
                     remaster"), placed after studio/live: "keep this"
  4. quality         SAMPLE RATE only. Bitrate between two ALAC copies of one
                     recording is noise; counting it would swap masters for
                     no audible gain (AC/DC "Big Gun", 2026-10-03)
  5. length          the longer copy, when it is at least 2 s longer
  6. a full tie      the library copy stays

Grey, 2026-10-05: the duplicate resolver ranked by bitrate and size and never
used this rule, so 32 of its 256 planned moves kept the SHORTER copy ("Sweet
Child O' Mine" 4:23 over 5:56). Grey: "you may change the keep rule". The
resolver now ranks by this rule too.

Live and remaster are read the way the duplicate resolver reads them
(dupe_resolver._is_live / _is_reissue: title AND album), so the review list
and the resolver can never disagree about what counts as live.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

LOSSY = frozenset({"aac", "mp3", "vorbis", "opus", "wma", "wmav2", "mp2"})
#: Codecs that rank as lossless (step 1 "any other lossless"), with any
#: "pcm_*". Named rather than inferred: a lossy codec missing from LOSSY --
#: ac3, eac3, wmav1, musepack, amr -- ranked as lossless and beat a real AAC
#: master, which canonicalize then re-encoded (review of #86, finding 8). An
#: unknown codec is never taken for lossless, as the rule already said.
LOSSLESS = frozenset({"alac", "flac", "wavpack", "ape", "tta", "mlp", "truehd", "shorten",
                      "mp4als", "als", "wmalossless", "tak", "ralf"})  # fmt: skip
STEPS = ("format", "original/baked", "studio/live", "original/remaster", "quality", "length")
#: Lengths closer than this are the same length (a fade, a gap of silence).
LENGTH_SLACK_S = 2.0


def _format_rank(codec: str | None) -> int:
    c = (codec or "").lower()
    if not c:
        return 3
    if c == "alac":
        return 0
    if c == "flac":
        return 1
    return 2 if c in LOSSLESS or c.startswith("pcm_") else 3


def keep_key(m: Mapping[str, Any]) -> tuple[int, int, int, int, int]:
    """Steps 1-4; smaller is better. *m* needs codec, title, album, sample_rate, lufs.

    Length (step 5) is not in the key: "at least 2 s longer" is a tolerance,
    and a sort key cannot hold one without bucketing 201.9 s and 202.1 s apart.
    """
    # Imported here: the resolver imports this module, and these read titles
    # and albums exactly the way it does.
    from .stages.dupe_resolver import _is_live, _is_reissue, _looks_baked

    return (
        _format_rank(m.get("codec")),
        1 if _looks_baked(dict(m)) else 0,
        1 if _is_live(dict(m)) else 0,
        1 if _is_reissue(dict(m)) else 0,
        -int(m.get("sample_rate") or 0),
    )


def decide(review: Mapping[str, Any], library: Mapping[str, Any]) -> tuple[str, str]:
    """("review" | "library" | "tie", the step that decided it, or "").

    "tie" means keep the library copy: a swap moves a master and remakes
    every edition copy of it, which is not worth doing for nothing.

    The two first share their loudness the way the resolver's members do: an
    unmeasured copy with the same audio as a master measured at -18 LUFS is
    that baked copy, not an original. Without it the swap tool judged an
    identical copy "original" (review of #86, finding 7; the 2026-09-26 shape).
    """
    from .stages.dupe_resolver import _share_loudness

    review, library = dict(review), dict(library)
    _share_loudness([review, library])
    for step, r, lib in zip(STEPS, keep_key(review), keep_key(library), strict=False):
        if r != lib:
            return ("review" if r < lib else "library"), step
    gap = float(review.get("duration") or 0) - float(library.get("duration") or 0)
    if abs(gap) >= LENGTH_SLACK_S:
        return ("review" if gap > 0 else "library"), "length"
    return "tie", ""
