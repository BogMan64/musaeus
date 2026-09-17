"""What the library actually holds, and which artists it is worth asking about.

READ-ONLY. The vault database is opened mode=ro.

THE MEASUREMENT THAT SHAPES THIS TOOL
-------------------------------------
Grey's library is singles-oriented, not album-oriented. Measured on the vault,
2,140 catalogued tracks:

    782 tracks (37%)          have no album tag at all
    the largest "album"       is "My playlist B", 183 tracks
    277 of 464 artists        have exactly ONE track
    only 42 artists           have 2+ real albums and 5+ tracks

So a discography gap report that runs over every artist produces mostly
nonsense: "you own 1 track by Artist X and are missing their 14 albums" is
technically true and completely useless. Worse, it is the kind of output that
buries the 42 rows that ARE worth reading.

Eligibility is therefore a first-class part of the tool, not a filter bolted on.
The thresholds are arguments, and the report states them, because they are a
judgement about what Grey collects rather than a fact about music.

ARTIST MBIDs
------------
Only 20 of the 42 eligible artists carry an mb_artist_id in the archive, and the
biggest collections are among the missing -- The Beatles (31 albums), Bob Dylan
(20), Bruce Springsteen (18), Billy Joel (18) all have none. Resolving those over
the network would be 22 searches before any real work started.

They are already resolved in MUSAEUS's own cache:
/mnt/FORGE2TB/Projects/MUSAEUS_VAULT/_db_backups/mb_cache.db, table mb_artist,
2,646 found of 3,186 looked up, keyed on the stored artist name lowercased. All
four of the above resolve from it. So this reads that cache and needs no network
for artist identity at all.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from pathlib import Path

VAULT_DB = Path("/mnt/FORGE2TB/Projects/MUSAEUS_VAULT/musaeus.db")
MB_CACHE = Path("/mnt/FORGE2TB/Projects/MUSAEUS_VAULT/_db_backups/mb_cache.db")

#: Album tags that name a container rather than a release. These are Grey's
#: playlist exports and MUSAEUS's placeholders; treating "My playlist B" as an
#: album would make its 183 tracks look like one enormous record.
NOT_AN_ALBUM = ("my playlist", "unknown", "various artists", "untitled")

#: Owned albums that are collections rather than studio records. Detected only to
#: WARN: see OwnedArtist.has_compilation.
COMPILATION_HINTS = (
    "greatest hits", "best of", "the best", "anthology", "collection",
    "essential", "very best", "hits", "compilation", "gold", "singles",
    "definitive", "retrospective",
)

DEFAULT_MIN_ALBUMS = 2
DEFAULT_MIN_TRACKS = 5


def _is_real_album(album: str) -> bool:
    a = (album or "").strip().lower()
    return bool(a) and not any(h in a for h in NOT_AN_ALBUM)


@dataclass
class OwnedArtist:
    """One artist as the library holds them."""

    artist: str
    mbid: str = ""
    mb_name: str = ""
    albums: set[str] = field(default_factory=set)
    track_count: int = 0
    genre: str = ""

    @property
    def has_compilation(self) -> bool:
        """True if any owned album looks like a hits collection.

        WHY THIS IS REPORTED RATHER THAN CORRECTED. Own "Bob Dylan's Greatest
        Hits" and you own Blowin' in the Wind -- so "you own nothing from The
        Freewheelin' Bob Dylan" is misleading even though, at album level, it is
        true. Resolving it properly means fetching every release group's
        tracklist and comparing recordings, which is one extra request per album
        against a source limited to one request per second.

        So the gap is reported at album level, as Grey framed it, and the row
        carries a flag saying a compilation may already cover it. A caveat the
        reader can see beats a silent inaccuracy.
        """
        return any(
            any(h in (a or "").lower() for h in COMPILATION_HINTS) for a in self.albums
        )


def _resolve_mbids(artists: list[str], cache_db: Path) -> dict[str, tuple[str, str]]:
    """{artist: (mbid, mb_name)} from MUSAEUS's artist cache.

    Missing cache is not fatal -- it degrades to "no mbid", which the caller
    reports as unresolvable rather than treating as an empty discography. Those
    are opposite conclusions and must not be confused: one is "we could not ask",
    the other is "we asked and you have everything".
    """
    if not cache_db.is_file():
        return {}
    uri = f"file:{cache_db}?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    try:
        out: dict[str, tuple[str, str]] = {}
        for a in artists:
            row = conn.execute(
                "SELECT mbid, COALESCE(mb_name,'') FROM mb_artist "
                "WHERE artist_key = ? AND found = 1 AND COALESCE(mbid,'') <> ''",
                (a.strip().lower(),),
            ).fetchone()
            if row:
                out[a] = (row[0], row[1])
        return out
    finally:
        conn.close()


def load_artists(
    db: Path = VAULT_DB,
    cache_db: Path = MB_CACHE,
    status: str = "CATALOGUED",
    min_albums: int = DEFAULT_MIN_ALBUMS,
    min_tracks: int = DEFAULT_MIN_TRACKS,
) -> tuple[list[OwnedArtist], dict[str, int]]:
    """(eligible artists, counts explaining who was excluded and why).

    The counts are returned rather than logged because the excluded population is
    most of the library -- 422 of 464 artists -- and a report that silently drops
    91% of its input is one nobody can sanity-check.
    """
    if not db.is_file():
        raise SystemExit(f"Vault database not found: {db}")
    uri = f"file:{db}?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            """
            SELECT COALESCE(artist,'') AS artist,
                   COALESCE(album,'')  AS album,
                   COALESCE(genre,'')  AS genre,
                   COALESCE(mb_artist_id,'')   AS mb_artist_id,
                   COALESCE(mb_artist_name,'') AS mb_artist_name
              FROM archive
             WHERE status = ?
            """,
            (status,),
        ).fetchall()
    finally:
        conn.close()

    by_artist: dict[str, OwnedArtist] = {}
    for r in rows:
        name = r["artist"].strip()
        if not name:
            continue
        oa = by_artist.setdefault(name, OwnedArtist(artist=name))
        oa.track_count += 1
        if _is_real_album(r["album"]):
            oa.albums.add(r["album"].strip())
        if r["mb_artist_id"] and not oa.mbid:
            oa.mbid = r["mb_artist_id"]
            oa.mb_name = r["mb_artist_name"]
        # Genre is majority-voted: keep the most-seen value. A single track
        # tagged differently should not reclassify an artist with 40 Rock tracks
        # as Classical. Stored as the raw genre string; comparison is
        # case-insensitive at exclusion time.
        if r["genre"]:
            oa.genre = r["genre"].strip()

    stats = {
        "artists_total": len(by_artist),
        "excluded_too_few_tracks": 0,
        "excluded_too_few_albums": 0,
        "excluded_classical": 0,
        "eligible_before_mbid": 0,
        "mbid_from_archive": 0,
        "mbid_from_cache": 0,
        "unresolvable_no_mbid": 0,
    }

    eligible: list[OwnedArtist] = []
    for oa in by_artist.values():
        if oa.track_count < min_tracks:
            stats["excluded_too_few_tracks"] += 1
            continue
        if len(oa.albums) < min_albums:
            stats["excluded_too_few_albums"] += 1
            continue
        # A composer's "discography" in MusicBrainz is thousands of release
        # groups by hundreds of different performers -- Vivaldi alone would
        # return hundreds of orchestras' recordings of the Four Seasons, most
        # tagged Album with no secondary types and therefore passing the ruling.
        # Studio-album gaps are meaningless for classical composers; exclude them
        # rather than flooding the report with noise that destroys its credibility.
        if oa.genre.lower() == "classical":
            stats["excluded_classical"] += 1
            continue
        eligible.append(oa)
    stats["eligible_before_mbid"] = len(eligible)

    need = [oa.artist for oa in eligible if not oa.mbid]
    resolved = _resolve_mbids(need, cache_db)
    for oa in eligible:
        if oa.mbid:
            stats["mbid_from_archive"] += 1
        elif oa.artist in resolved:
            oa.mbid, oa.mb_name = resolved[oa.artist]
            stats["mbid_from_cache"] += 1
        else:
            stats["unresolvable_no_mbid"] += 1

    eligible.sort(key=lambda o: (-len(o.albums), -o.track_count, o.artist.lower()))
    return eligible, stats
