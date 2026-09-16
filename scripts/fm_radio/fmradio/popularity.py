"""How many listens is "a lot" -- answered per artist, not once for everyone.

THE PROBLEM WITH AN ABSOLUTE FLOOR
----------------------------------
The first version dropped any ListenBrainz recording under 100 listens. That
number was chosen from a real measurement: The Beatles return thousands of
recordings, most of them bootlegs and session chatter like "A/B Road: Complete
Get Back Sessions", and only 1,753 cleared 100. Feeding the tail to the scorer
buried the real pressings.

It is still the wrong shape, because it conflates two jobs:

  1. separating real pressings from bootleg noise   <- what it was for
  2. deciding an artist is worth considering at all <- what it also did

For Barney Bentall & The Legendary Hearts, whose best-known recording may sit at
40 listens, an absolute 100 returns nothing. The tool then reports "no
ListenBrainz signal" -- indistinguishable from an outage -- for an artist whose
data was there all along. The floor silenced the artist rather than the noise.

THE FIX
-------
Derive the floor from the artist's OWN distribution. The top decile of their
counts scales down for obscure artists and up for The Beatles, which is the
behaviour wanted from a single number.

WHAT THIS MUST NOT DO
---------------------
A relative floor can be more destructive than an absolute one. Ask about a deep
album cut and the 90th percentile of that artist's counts will sit far above it,
so the very song being asked about gets filtered out and the run reports "no
candidates" for a song plainly in the library. The floor exists to stop real
pressings being buried, never to exclude the subject of the question. So
apply_floor() is guaranteed non-empty: if the floor would remove everything, it
returns everything and says so. A filter that can silently empty its own input
is the same failure as the absolute 100, one level down.
"""

from __future__ import annotations

from dataclasses import dataclass

#: Below this, a count is noise on any artist's scale -- a single stray scrobble.
#: Retained as a hard floor because a relative floor computed from a handful of
#: 1s and 2s is meaningless.
ABSOLUTE_NOISE_FLOOR = 5

#: The top decile. Grey's call, and the right shape: it is a statement about
#: rank within the artist rather than an absolute popularity.
DEFAULT_QUANTILE = 0.90

#: A second floor, as a fraction of the artist's single biggest count.
#:
#: WHY BOTH. A rank-based quantile fails on the exact distribution it was
#: introduced to handle. The Beatles return thousands of records of which a
#: large share have exactly one listen; when the junk tail is most of the
#: catalogue, the 90th percentile BY RANK is itself junk. A test with 2,249
#: one-listen records against two millionsellers produced a floor of 5 -- lower
#: than the floor computed for an obscure artist whose best track had 40 listens.
#: The statistic inverted.
#:
#: Peak-relative does not have that failure mode: it is immune to how many junk
#: records exist, because it only looks at the top. So the floor is the HIGHER of
#: the two, which keeps the quantile's sensitivity for flat catalogues and the
#: peak fraction's robustness for skewed ones.
#:
#: 1% is deliberately loose. Being too aggressive is self-correcting here --
#: apply_floor() waives a floor that would empty its input -- whereas being too
#: loose silently lets bootlegs bury real pressings, which is unrecoverable.
PEAK_FRACTION = 0.01


def quantile(values: list[int], q: float) -> float:
    """Linear-interpolated quantile. No numpy dependency for eight lines.

    Empty input returns 0.0 rather than raising: an artist with no counts should
    produce a floor of "keep everything", not an exception in a reporting tool.
    """
    xs = sorted(v for v in values if v is not None)
    if not xs:
        return 0.0
    if len(xs) == 1:
        return float(xs[0])
    pos = (len(xs) - 1) * max(0.0, min(1.0, q))
    lo = int(pos)
    hi = min(lo + 1, len(xs) - 1)
    frac = pos - lo
    return xs[lo] * (1.0 - frac) + xs[hi] * frac


@dataclass(frozen=True)
class Floor:
    """A computed floor, and the numbers behind it.

    Carries its inputs because a threshold that cannot be explained cannot be
    argued with, and this one changes which pressings a reviewer ever sees.
    """

    value: int
    quantile_used: float
    n_counts: int
    max_count: int

    def describe(self) -> str:
        if self.n_counts == 0:
            return "no listen counts for this artist -- no popularity floor applied"
        return (
            f"popularity floor {self.value:,} "
            f"(top {int(round((1 - self.quantile_used) * 100))}% of {self.n_counts:,} "
            f"recordings, best {self.max_count:,})"
        )


def artist_floor(
    counts: list[int],
    q: float = DEFAULT_QUANTILE,
    absolute_min: int = ABSOLUTE_NOISE_FLOOR,
) -> Floor:
    """The listen floor for one artist, from that artist's own counts."""
    real = [c for c in counts if c is not None and c > 0]
    if not real:
        return Floor(value=0, quantile_used=q, n_counts=0, max_count=0)
    peak = max(real)
    raw = max(
        quantile(real, q),          # sensitive on flat catalogues
        peak * PEAK_FRACTION,       # robust on catalogues with a huge junk tail
        min(absolute_min, peak),    # never below the noise gate, unless the
                                    # artist's whole catalogue is below it
    )
    # The artist's best recording always survives its own floor. Without this
    # cap an artist whose entire catalogue sits under ABSOLUTE_NOISE_FLOOR would
    # be filtered to nothing -- the original bug, one level down.
    floor = min(int(raw), peak)
    return Floor(value=floor, quantile_used=q, n_counts=len(real), max_count=peak)


def apply_floor(items: list, floor: Floor, count_of) -> tuple[list, bool]:
    """(kept, floor_was_waived).

    count_of(item) returns that item's listen count or None. None NEVER fails
    the floor: a MusicBrainz pressing has no ListenBrainz count, and dropping it
    for having no popularity data would silently make pass 2 useless.

    floor_was_waived is True when the floor removed everything and was therefore
    ignored. The caller is expected to surface that, not swallow it -- it is the
    signal that this song sits below its artist's top decile, which is ordinary
    for an album cut and worth a reviewer knowing.
    """
    if floor.value <= 0:
        return list(items), False
    kept = [it for it in items if count_of(it) is None or count_of(it) >= floor.value]
    if not kept:
        return list(items), True
    return kept, False
