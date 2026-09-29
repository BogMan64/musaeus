"""
MUSAEUS — an AAC edition's browsing index (car and iPhone).

Moved from scripts/car_library/write_car_index.py (cloud review of #53):
`musaeus edition-build car|iphone` writes it after every build, as the
retired builder did.

Grey asked (2026-09-17) whether an index can be written for the Android head
unit, and made standard on CAR_Library.

**There is no index format that stops a head unit scanning.** Android units
build their own media database from a MediaStore scan on insert; FAT32 has no
directory index, so the scan is a linear walk of ~10,650 files however the
stick is arranged. Nothing MUSAEUS writes changes that.

What IS portable, and what this writes, is **M3U8 playlists**: per genre, per
decade, and one All. Every mainstream Android player (the stock unit apps,
Poweramp, VLC, Kodi) reads them, they give a browse-by-genre view that does
not depend on the unit's own database being correct, and they cost a few
hundred kilobytes.

It writes them INSIDE the edition -- `CAR_Library/Playlists/` -- for one
reason: the edition is what gets copied to the stick, so an index that lives
anywhere else is an index that does not travel.

It reuses `PlaylistStage`, which already groups by genre and decade and emits
`#EXTINF` lines. Pointing that stage at `CAR_Library/Playlists` makes its
relative paths come out as `../Artist/Album/Track.m4a`, which resolves both in
the vault and on the USB, because the playlist folder sits one level below the
edition root in both places.

Usage:
    python3 scripts/car_library/write_car_index.py            # dry run
    python3 scripts/car_library/write_car_index.py --apply
"""

from __future__ import annotations

import dataclasses
import sqlite3
from pathlib import Path

from .config import MusicConfig
from .context import RunContext
from .stages.playlist import PlaylistStage

#: Where the index lives inside the edition. One level below the edition root,
#: which is what makes the "../" in every entry resolve on the USB too.
INDEX_DIRNAME = "Playlists"


def index_dir_for(edition_root: Path) -> Path:
    return edition_root / INDEX_DIRNAME


def verify_index(index_dir: Path) -> list[str]:
    """Open every playlist and check each entry resolves to a real file.

    Existence of the .m3u8 is not evidence it works -- a playlist whose lines
    point nowhere looks identical to one that does, and only reports itself in
    the car. So the entries are resolved here, against the disk.
    """
    problems: list[str] = []
    playlists = sorted(index_dir.glob("*.m3u8"))
    if not playlists:
        return [f"no playlists were written to {index_dir}"]
    for pl in playlists:
        entries = [
            ln.strip()
            for ln in pl.read_text(encoding="utf-8").splitlines()
            if ln.strip() and not ln.startswith("#")
        ]
        if not entries:
            problems.append(f"{pl.name}: no entries")
            continue
        for entry in entries:
            if Path(entry).is_absolute():
                problems.append(f"{pl.name}: absolute path, will not survive the USB: {entry}")
                break
            if not (pl.parent / entry).resolve().is_file():
                problems.append(f"{pl.name}: entry does not resolve: {entry}")
                break
    return problems


def _entries(playlist: Path) -> list[str]:
    return [
        ln.strip()
        for ln in playlist.read_text(encoding="utf-8").splitlines()
        if ln.strip() and not ln.startswith("#")
    ]


def write_index(cfg: MusicConfig, edition_root: Path, apply: bool) -> tuple[list[str], list[str]]:
    """Write (or dry-run) the index for *edition_root*.

    Returns (notes, problems). `problems` is empty only when the index was
    written AND every entry in it was resolved against the disk -- existence
    of a .m3u8 proves nothing, which is the rule the rest of this codebase
    already runs on.
    """
    index_dir = index_dir_for(edition_root)
    # What was there before, by exact timestamp: a playlist this run leaves
    # untouched is stale. (It was 'older than the start less 1 s', and a
    # build faster than that second kept a stale one: the ffmpeg 6.1 container.)
    before = {f: f.stat().st_mtime_ns for f in index_dir.glob("*.m3u8")}
    # dataclasses.replace, not mutation: the stage reads cfg.playlists and
    # nothing else should see this override.
    cfg = dataclasses.replace(cfg, playlists=index_dir)

    conn = sqlite3.connect(cfg.db_path)
    conn.row_factory = sqlite3.Row
    ctx = RunContext.new(cfg, conn, dry_run=not apply)
    try:
        stage = PlaylistStage()
        stage.validate(ctx)
        result = stage.run(ctx) if apply else stage.dry_run(ctx)
        notes = list(result.notes)
    finally:
        ctx.finish()
        conn.close()

    if apply:
        # Sweep playlists this run did not write.
        #
        # PlaylistStage writes one file per genre that still HAS tracks. A
        # genre whose last track is deleted is simply not written -- and the
        # previous run's file is left sitting there, listing a track that no
        # longer exists. Found 2026-09-17: "Holiday.m3u8" held one track,
        # Bobby Goldsboro's "Honey"; he was deleted, the genre emptied, and
        # the stale playlist survived pointing at a missing file.
        #
        # An index that lists what is gone is worse than no index: in the car
        # it is a dead entry, and here it looked exactly like a build failure.
        fresh = {f for f in index_dir.glob("*.m3u8") if before.get(f) != f.stat().st_mtime_ns}
        for f in sorted(index_dir.glob("*.m3u8")):
            if f not in fresh:
                f.unlink()
                notes.append(f"removed stale playlist (genre is now empty): {f.name}")
        # A genre with no copy in THIS edition yet -- a partial car build,
        # every budgeted iPhone edition -- comes out as a playlist with no
        # entries: not written (the 200-song vault build, 2026-09-29).
        empty = [f for f in sorted(index_dir.glob("*.m3u8")) if not _entries(f)]
        for f in empty:
            f.unlink()
        if empty:
            notes.append(f"left out {len(empty)} playlist(s) with no song in this edition yet")

    problems = verify_index(index_dir) if apply else []
    return notes, problems
