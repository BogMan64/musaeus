#!/usr/bin/env python3
"""
MUSAEUS — Edition selection.

An **edition** is a rendering of the masters for a target: derived,
disposable, rebuildable. Building one is two separate jobs -- deciding WHAT
goes in, and encoding it -- and only the second existed. `build_car_library.py`
converts whatever is hand-dropped into a folder and never queries the
library at all, which is why no edition has ever been built from the
catalogue.

This is the first half: given an EditionSpec and some criteria, return the
exact list of masters that belong in it, in the order they should be
written. It touches no files and encodes nothing, so it is cheap to run,
cheap to preview, and testable without ffmpeg.

Why size is estimated rather than measured
------------------------------------------
A device budget has to be checked BEFORE spending hours encoding. For a
lossy edition the output size is a function of duration and bitrate and is
predictable within a few percent; for a lossless edition the bake re-encodes
at the same sample rate and depth, so the master's own size is the estimate.
Both are deliberately slight OVER-estimates -- overshooting a 32 GB iPhone
after a six-hour encode is a far worse failure than leaving 200 MB unused.

ORPHEUS prior art: SCRIPTS/build_aac_port_iphone.py, which ranked by genre
playlist and filled to a --limit-gb budget. Three things are different here:
selection reads the database rather than M3U8 files on a USB mount, so it
works when the mount is absent and cannot silently select a stale playlist;
genre matching is exact rather than substring (ORPHEUS's "Rock" also
matched "Classic_Rock", "Hard_Rock" and "Punk Rock", so a Rock-only edition
quietly pulled in four genres); and a track too large for the remaining
budget is skipped rather than ending the fill.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from pathlib import Path

# AAC in an MP4 container: sample data plus per-frame and index overhead.
# 2% is the measured ceiling on this library's encodes, and erring high is
# the safe direction for a device budget.
_CONTAINER_OVERHEAD = 1.02


@dataclass(frozen=True)
class EditionSpec:
    """What an edition IS -- the target, not the selection."""

    name: str
    codec: str                      # "alac" | "aac"
    lufs_target: float
    bitrate_kbps: int | None = None  # None = lossless
    max_sample_rate: int | None = None

    @property
    def is_lossless(self) -> bool:
        return self.bitrate_kbps is None


#: The three agreed editions. See MUSAEUS_EDITIONS_VOCABULARY.md -- the
#: masters are never baked, each edition bakes exactly once from them, and
#: no edition is ever built from another.
LOSSLESS = EditionSpec("lossless", codec="alac", lufs_target=-18.0)
CAR = EditionSpec("car", codec="aac", lufs_target=-14.0,
                  bitrate_kbps=256, max_sample_rate=48_000)
IPHONE = EditionSpec("iphone", codec="aac", lufs_target=-14.0,
                     bitrate_kbps=256, max_sample_rate=48_000)

EDITIONS = {e.name: e for e in (LOSSLESS, CAR, IPHONE)}

#: Fill order when a budget forces a choice. Genres absent from the library
#: are simply skipped, and any genre not named here follows in alphabetical
#: order, so adding a genre to the catalogue never silently drops it.
DEFAULT_GENRE_PRIORITY: tuple[str, ...] = (
    "Rock", "Classic Pop", "Rock & Roll", "R&B/Funk/Soul", "Blues",
    "Soft Rock", "Hard Rock", "Southern Rock", "Alternative", "Country",
    "Folk", "Singer-Songwriter", "Celtic", "Jazz", "Disco/Electronic",
    "Hip Hop", "Soundtrack", "Classical",
)


@dataclass(frozen=True)
class Track:
    file_path: str
    artist: str
    album: str
    title: str
    genre: str
    duration: float
    size_bytes: int


@dataclass
class Selection:
    spec: EditionSpec
    included: list[Track] = field(default_factory=list)
    skipped_for_budget: list[Track] = field(default_factory=list)
    budget_bytes: int | None = None

    @property
    def estimated_bytes(self) -> int:
        return sum(estimated_bytes(t, self.spec) for t in self.included)

    @property
    def total_duration_s(self) -> float:
        return sum(t.duration for t in self.included)

    def summary(self) -> str:
        gb = self.estimated_bytes / 1_000_000_000
        line = (f"{self.spec.name}: {len(self.included):,} track(s), "
                f"~{gb:.1f} GB, {self.total_duration_s / 3600:.1f} h")
        if self.budget_bytes is not None:
            line += f" of {self.budget_bytes / 1_000_000_000:.1f} GB budget"
        if self.skipped_for_budget:
            line += f" ({len(self.skipped_for_budget):,} skipped for space)"
        return line


def estimated_bytes(track: Track, spec: EditionSpec) -> int:
    """Predicted output size for *track* in *spec*.

    Lossless: the bake re-encodes ALAC at the same rate and depth, so the
    master's own size is the best available estimate.

    Lossy: duration x bitrate is exact for CBR and close enough for AAC's
    near-constant rate, plus container overhead. Deliberately an
    over-estimate -- see the module docstring.
    """
    if spec.is_lossless:
        return int(track.size_bytes or 0)
    if not track.duration or track.duration <= 0:
        # No duration means no way to predict. Fall back to the master's
        # size: wildly high for a lossy edition, but a budget that refuses
        # a track it cannot measure is safer than one that overruns.
        return int(track.size_bytes or 0)
    bits = track.duration * (spec.bitrate_kbps or 0) * 1000
    return int(bits / 8 * _CONTAINER_OVERHEAD)


def genre_rank(genre: str, priority: tuple[str, ...]) -> tuple[int, str]:
    """Sort key: named genres in listed order, everything else after,
    alphabetically. Exact match only -- see the module docstring on why."""
    try:
        return (priority.index(genre), "")
    except ValueError:
        return (len(priority), (genre or "￿").lower())


def load_tracks(
    conn: sqlite3.Connection,
    *,
    genres: set[str] | None = None,
    artists: set[str] | None = None,
) -> list[Track]:
    """Every CATALOGUED master, optionally narrowed by genre or artist.

    Only CATALOGUED rows: QUARANTINED, DUPE_REVIEW and GHOST all describe
    something the owner has not accepted into the library, and an edition
    built from them would ship a judgement back out as music.
    """
    rows = conn.execute(
        """
        SELECT file_path, artist, album, title, genre, duration, size_bytes
          FROM archive
         WHERE status = 'CATALOGUED'
         ORDER BY file_path
        """
    ).fetchall()

    out: list[Track] = []
    for r in rows:
        genre = (r["genre"] or "").strip()
        artist = (r["artist"] or "").strip()
        if genres is not None and genre not in genres:
            continue
        if artists is not None and artist not in artists:
            continue
        out.append(Track(
            file_path=r["file_path"], artist=artist,
            album=(r["album"] or "").strip(), title=(r["title"] or "").strip(),
            genre=genre, duration=float(r["duration"] or 0.0),
            size_bytes=int(r["size_bytes"] or 0),
        ))
    return out


def select_edition(
    conn: sqlite3.Connection,
    spec: EditionSpec,
    *,
    genres: set[str] | None = None,
    artists: set[str] | None = None,
    budget_bytes: int | None = None,
    genre_priority: tuple[str, ...] = DEFAULT_GENRE_PRIORITY,
) -> Selection:
    """Decide what goes into an edition. Reads only; writes nothing."""
    tracks = load_tracks(conn, genres=genres, artists=artists)

    # Deterministic: the same catalogue and criteria must always produce the
    # same edition, or a rebuild silently differs from what was delivered.
    tracks.sort(key=lambda t: (
        genre_rank(t.genre, genre_priority), t.artist.lower(),
        t.album.lower(), t.title.lower(), t.file_path,
    ))

    sel = Selection(spec=spec, budget_bytes=budget_bytes)
    if budget_bytes is None:
        sel.included = tracks
        return sel

    used = 0
    for t in tracks:
        cost = estimated_bytes(t, spec)
        if used + cost > budget_bytes:
            # Skip and keep filling. Stopping at the first oversized track
            # would strand the whole tail of the priority order behind one
            # long recording.
            sel.skipped_for_budget.append(t)
            continue
        sel.included.append(t)
        used += cost
    return sel


def output_path_for(track: Track, spec: EditionSpec, root: Path) -> Path:
    """Where *track* lands in the edition.

    Mirrors the master's own Artist/Album shape rather than inventing one,
    so an edition is diffable against the masters it came from.
    """
    src = Path(track.file_path)
    suffix = ".m4a"
    parent = src.parent.name or "Unknown Album"
    grand = src.parent.parent.name or "Unknown Artist"
    return root / grand / parent / (src.stem + suffix)
