"""`musaeus export itunes`: the file the iTunes Companion reads.

The Companion (Grey's Windows front panel for iTunes, built by ChatGPT,
reviewed by Claude 2026-11-08/09) adds the lossless edition to iTunes and
mirrors its playlists. MUSAEUS is the authority; this writes what the
Companion is allowed to touch, in its `musaeus-itunes-bridge/v1` format:

    {"schema": "musaeus-itunes-bridge/v1",
     "library_root": "<the lossless edition>",
     "tracks": [{"path": ..., "previous_paths": [...]}],
     "playlists": [{"name": ..., "track_paths": [...]}]}

- tracks: every copy the edition ledger records for the lossless edition
  whose file is on disk and carries that record's marker. Never a master:
  the root is the edition, and the Companion refuses any path outside it.
- previous_paths: where the copy was before MUSAEUS refiled its master
  (ORGANIZE_MOVE / ORGANIZE_RENAME / ARTIST_CONSOLIDATED), mapped into the
  edition, so the Companion can re-link a moved song instead of adding a
  second one. A path another track holds now is left out.
- playlists: the edition's own `Playlists/*.m3u8` (genres, eras, All),
  entries limited to the tracks above.

Read-only: the catalogue and the ledger are opened read-only; the only
write is the export file.
"""

from __future__ import annotations

import json
import os
import sqlite3
from collections import Counter
from pathlib import Path

from . import edition_bake
from .edition_build import EDITION, _mirror, marker_for
from .edition_index import INDEX_DIRNAME
from .edition_ledger import ledger_path

SCHEMA = "musaeus-itunes-bridge/v1"
MOVE_EVENTS = ("ORGANIZE_MOVE", "ORGANIZE_RENAME", "ARTIST_CONSOLIDATED")


def _read_only(path: Path) -> sqlite3.Connection:
    # as_uri() escapes "#" and "?", which SQLite would read as URI syntax.
    return sqlite3.connect(Path(path).resolve().as_uri() + "?mode=ro", uri=True)


class _History:
    """The catalogue's record of where files went, read once (a query per
    song took five minutes on 7,991 songs). Paths only: a drive letter or a
    leading slash."""

    def __init__(self, conn: sqlite3.Connection | None) -> None:
        #: path -> (event id, event type, where it came from), newest first:
        #: EVERY event that names the path as a destination, not only moves.
        self.arrivals: dict[str, list[tuple[int, str, str]]] = {}
        #: path -> ids of events that took a file away from it, newest first.
        self.departures: dict[str, list[int]] = {}
        #: path -> (event id, audio fingerprint prefix), newest first.
        self.hashes: dict[str, list[tuple[int, str]]] = {}
        rows = (
            conn.execute(
                "SELECT id, event_type, file_path, old_value, new_value FROM events "
                "WHERE new_value LIKE '/%' OR new_value LIKE '_:%' "
                "OR old_value LIKE '/%' OR old_value LIKE '_:%' "
                "OR event_type = 'HASH_COMPUTED' ORDER BY id DESC"
            )
            if conn is not None
            else ()
        )
        for eid, kind, file_path, old, new in rows:
            if kind == "HASH_COMPUTED":
                if file_path and new:
                    self.hashes.setdefault(file_path, []).append((eid, new.rstrip("…")))
                continue
            if new and old:
                self.arrivals.setdefault(new, []).append((eid, kind, old))
            # Not departures: Sentinel's "duplicate of" names the file already
            # filed, and a tag fix in place names the file it leaves where it is.
            if (
                old
                and kind != "DUPLICATE_FOUND"
                and not (kind == "CANONICALIZE" and old == file_path)
            ):
                self.departures.setdefault(old, []).append(eid)

    def earlier_masters(self, master: str, audio_hash: str) -> list[str]:
        """Every path *master* had before, newest first.

        One step back at a time, each strictly older than the last. A step
        is taken only when it is surely this recording's (CodeRabbit on #321;
        local CodeRabbit): the latest arrival at the path is one of the moves,
        not a finalize, a restore or anything else; nothing left the path
        between that arrival and this song leaving it; and where a
        fingerprint was recorded at the old path, it is this recording's.
        """
        out: list[str] = []
        path, before = master, None

        def newest(items, key=lambda x: x):
            return next((x for x in items if before is None or key(x) < before), None)

        while True:
            arrival = newest(self.arrivals.get(path, ()), key=lambda a: a[0])
            if arrival is None or arrival[1] not in MOVE_EVENTS:
                return out
            eid, _, old = arrival
            left = newest(self.departures.get(path, ()))
            if left is not None and left > eid:
                return out  # the file that arrived then left again
            seen = next((h for i, h in self.hashes.get(old, ()) if i < eid), None)
            if seen and not audio_hash.startswith(seen):
                return out  # another recording was at the old path
            if old != master and old not in out:
                out.append(old)
            path, before = old, eid


def _playlist_paths(playlist: Path, tracks: set[str]) -> list[str]:
    out: list[str] = []
    for line in playlist.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        path = os.path.normpath(playlist.parent / line)
        if path in tracks and path not in out:
            out.append(path)
    return out


def build(cfg: object) -> dict:
    """The bridge manifest for *cfg*'s lossless edition."""
    library = Path(cfg.alac_library)  # type: ignore[attr-defined]
    masters = Path(cfg.alac_archive)  # type: ignore[attr-defined]
    ledger = _read_only(ledger_path(cfg))
    try:
        rows = ledger.execute(
            "SELECT output_path, master_path, master_hash FROM edition_copies WHERE edition = ? "
            "ORDER BY output_path",
            (EDITION,),
        ).fetchall()
    finally:
        ledger.close()
    # Containment on the real paths: ".." and a symlink out both pass a plain
    # is_relative_to (local CodeRabbit and CodeRabbit on #321).
    real_library = Path(os.path.realpath(library))
    copies = []
    for out, master, audio_hash in rows:
        out = os.path.normpath(out)
        if not Path(os.path.realpath(out)).is_relative_to(real_library):
            continue
        # Its marker, not only a file: a stale record's path may hold another
        # master's copy (CodeRabbit on #321). No file reads as no marker.
        if edition_bake.read_marker(Path(out)) == marker_for(audio_hash):
            copies.append((out, master, audio_hash))
    current = {out for out, _, _ in copies}

    # After `musaeus reset` there is no catalogue: no history, the copies still go.
    db = Path(cfg.db_path)  # type: ignore[attr-defined]
    catalogue = _read_only(db) if db.is_file() else None
    try:
        history = _History(catalogue)
    finally:
        if catalogue is not None:
            catalogue.close()
    tracks = []
    for out, master, audio_hash in copies:
        previous: list[str] = []
        for old in history.earlier_masters(master, audio_hash or ""):
            old = os.path.normpath(old)
            if not Path(old).is_relative_to(masters):
                continue  # it was not in the library then: no copy of it
            was = os.path.normpath(_mirror(masters, library, Path(old)))
            if not Path(os.path.realpath(was)).is_relative_to(real_library):
                continue
            if was != out and was not in current and was not in previous:
                previous.append(was)
        tracks.append({"path": out, "previous_paths": previous})
    # An old place two songs both had is no proof of either: left out, so the
    # Companion never re-links one song's entry to the other.
    claims = Counter(p for t in tracks for p in t["previous_paths"])
    for t in tracks:
        t["previous_paths"] = [p for p in t["previous_paths"] if claims[p] == 1]

    playlists = []
    index = library / INDEX_DIRNAME
    for pl in sorted(index.glob("*.m3u8")) if index.is_dir() else []:
        paths = _playlist_paths(pl, current)
        if paths:
            # The file name with spaces back: "Blues_Rock" -> "Blues Rock".
            playlists.append({"name": pl.stem.replace("_", " "), "track_paths": paths})

    return {
        "schema": SCHEMA,
        "library_root": str(library),
        "tracks": tracks,
        "playlists": playlists,
    }


def write(manifest: dict, out: Path) -> None:
    """Write *manifest* to *out*, whole or not at all."""
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_name(f"{out.name}.{os.getpid()}.tmp")  # one per writer
    try:
        tmp.write_text(json.dumps(manifest, ensure_ascii=False, indent=1), encoding="utf-8")
        os.replace(tmp, out)
    finally:
        tmp.unlink(missing_ok=True)  # gone after a replace; a failed write's leftover
