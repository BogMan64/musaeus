#!/usr/bin/env python3
"""The SONGS behind the quarantined tribute/karaoke tracks, and who actually charted them.

Grey's ask: take the song, not the artist, and where possible name the artist who
really charted it -- so the real version can be sourced.

READ-ONLY. Reads the tribute manifests and the vault database (mode=ro). Writes
one CSV. Moves nothing, deletes nothing, restores nothing.

WHERE THE CHARTING ARTIST COMES FROM
------------------------------------
Mostly from the filenames themselves. Karaoke publishers are legally obliged to
say who the original was, so they write it down:

    Hit Tunes Karaoke - Summer Girls (Originally Performed By LFO) (Karaoke Version)
    ProSource Karaoke - The Doctor (In the Style of Doobie Brothers)[Instrumental Only]
    Jimmy Crespo, Richard Kendrick - Panama (as made famous by Van Halen)
    Classic Blues Tones - Bad To The Bone - George Thorogood & The Destroyers Tribute

Four phrasings, 56 + 11 + 7 + a handful of the last across 173 rows. That is a
free, offline, high-confidence answer for most of the set, and it beats guessing
from a chart database that may not have the song.

Anything without an attribution marker falls back to the library: if a real
artist's copy of the same song is already catalogued, that artist is the answer
and no network is needed either.

WHAT IT ALSO REPORTS, UNASKED
-----------------------------
Suspected false positives. `Foghat - Slow Ride (2016 Remaster)` is in the
quarantine pile, matched on an ALBUM-level \\btribute\\b pattern -- but Foghat is
the real band and a 2016 remaster is a real release. Quarantining it is a
mistake, and a review list that silently passes over the mistakes is not a review
list. Rows where the quarantined artist looks genuine AND no karaoke/instrumental
/backing marker is present are flagged for a second look.
"""

from __future__ import annotations

import argparse
import csv
import os
import re
import sqlite3
import sys
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path

TRASH = Path("/mnt/FORGE2TB/.Trash-1000/files/TRIBUTE_REMOVED_FOR_REVIEW")
VAULT_DB = Path("/mnt/FORGE2TB/Projects/MUSAEUS_VAULT/musaeus.db")

# ── Borrowed title folding ────────────────────────────────────────────────────
# Same pattern as the other companion tools: use MUSAEUS's normaliser when it is
# importable so this agrees with the deduplicator, and say which is in force.
_IMPL = "builtin"
_IMPORT_ERR: str | None = None
_musaeus_normalise = None
try:  # pragma: no cover - machine dependent
    from musaeus.stages.neardupe import _normalise as _musaeus_normalise  # type: ignore

    _IMPL = "musaeus"
except Exception as exc:
    _IMPORT_ERR = f"{type(exc).__name__}: {exc}"

_PUNCT = re.compile(r"[^\w\s]")
_WS = re.compile(r"\s+")


def _builtin_fold(s: str) -> str:
    s = unicodedata.normalize("NFKD", (s or "").lower())
    s = "".join(c for c in s if not unicodedata.combining(c))
    s = _WS.sub(" ", _PUNCT.sub(" ", s)).strip()
    return s[4:] if s.startswith("the ") else s


def fold(s: str) -> str:
    if _musaeus_normalise is not None:
        try:
            return _musaeus_normalise(s or "", strip_qualifiers=True)
        except Exception:
            pass
    return _builtin_fold(s)


def provenance() -> str:
    if _IMPL == "musaeus":
        return "title folding: MUSAEUS neardupe._normalise"
    return f"title folding: built-in fallback ({_IMPORT_ERR})"


# ── Attribution: who really did it ────────────────────────────────────────────
# Ordered by how explicit the claim is. "Originally performed by" is a publisher
# stating the source; "tribute" in a trailing clause is weaker inference.
#: Brackets come in three flavours in this set, including curly:
#:   [In the Style of -Steely Dan-] {Karaoke Demonstration Version With Lead Vocal}
_OB, _CB = r"\(\[\{", r"\)\]\}"

ATTRIBUTION = [
    ("originally performed", re.compile(
        rf"[{_OB}]\s*(?:originally\s+performed\s+by|orig(?:inally)?\.?\s+by)\s*:?\s*([^{_CB}]+?)\s*[{_CB}]", re.I)),
    # 18 files use this phrasing. Missing it left most of the unattributed rows
    # with no artist despite the answer sitting in the filename.
    ("made popular by", re.compile(
        rf"[{_OB}]?\s*(?:as\s+)?made\s+popular\s+by\s*:?\s*([^{_CB}]+?)\s*(?:[{_CB}]|$)", re.I)),
    ("in the style of", re.compile(
        rf"[{_OB}]\s*in\s+the\s+style\s+of\s*:?\s*([^{_CB}]+?)\s*[{_CB}]", re.I)),
    ("made famous by", re.compile(
        rf"[{_OB}]?\s*(?:as\s+)?made\s+famous\s+by\s*:?\s*([^{_CB}]+?)\s*(?:[{_CB}]|$)", re.I)),
    ("tribute clause", re.compile(
        r"[-–—]\s*([A-Z][^-–—()\[\]]{2,60}?)\s+Tribute\s*$", re.I)),
]

#: Markers that prove this is a substitute recording rather than the real thing.
#: Their PRESENCE is what makes quarantining safe; their ABSENCE on a real-looking
#: artist is what makes a row suspicious.
SUBSTITUTE_MARKERS = re.compile(
    r"\b(?:karaoke|instrumental|backing\s+track|backing\s+vocals?|vocal\s+version|"
    r"sing[\s-]?along|midi|in\s+the\s+style\s+of|originally\s+performed|made\s+famous\s+by|"
    r"tribute|cover\s+version|as\s+performed\s+by|piano\s+cover)\b", re.I)

#: Junk that is part of the packaging, not the song. Stripped from titles.
TITLE_NOISE = re.compile(
    rf"[{_OB}]\s*(?:[^{_CB}]*?(?:karaoke|instrumental|backing|vocal|originally\s+performed|"
    r"in\s+the\s+style|made\s+famous|made\s+popular|full\s+vocal|no\s+vocal|with\s+vocal|"
    r"lead\s+vocal|demonstration|sing[\s-]?along|midi|sound-?a-?like|version|mix|edit)\b"
    rf"[^{_CB}]*)\s*[{_CB}]", re.I)

TRAILING_TRIBUTE = re.compile(r"\s*[-–—]\s*[^-–—]{2,60}?\s+Tribute\s*$", re.I)

#: Un-bracketed packaging noise, stripped as whole clauses. Each was observed:
#:   "Omnibus Media - Karaoke Tracks - Sail Away (made famous by David Gray)"
#:   "Tribute Stars - Knockin' On Heaven's Door - Original"
#:   "Chances Are - Sound-A-Like As Made Famous By ..."
BARE_NOISE = [
    re.compile(r"^\s*karaoke\s+tracks\s*[-–—]\s*", re.I),      # leading clause
    re.compile(r"\s*[-–—]\s*original\s*$", re.I),               # trailing "- Original"
    re.compile(r"\s*[-–—]?\s*sound-?a-?like\b.*$", re.I),       # everything after it
    re.compile(r"\s*[-–—]\s*(?:as\s+)?made\s+(?:famous|popular)\s+by\b.*$", re.I),
    re.compile(r"\s*[-–—]\s*(?:originally\s+performed|in\s+the\s+style)\b.*$", re.I),
]


@dataclass
class Row:
    song: str = ""
    charted_by: str = ""
    attribution_source: str = ""
    quarantined_as: str = ""
    library_artists: list[str] = field(default_factory=list)
    already_owned: bool = False
    suspicious: str = ""
    reason: str = ""
    filename: str = ""


def parse_name(basename: str) -> tuple[str, str, str, str]:
    """(quarantined_artist, song, charting_artist, attribution_source).

    The filename shape is `Artist - Title (markers)`. Split on the FIRST " - "
    only: a medley like "Killer Queen - Bohemian Rhapsody - Somebody To Love" is
    one title with dashes in it, and splitting on the last would keep a fragment.
    """
    stem = re.sub(r"\.m4a$", "", basename, flags=re.I)

    artist, song = "", stem
    if " - " in stem:
        artist, song = stem.split(" - ", 1)

    charting, src = "", ""
    for label, rx in ATTRIBUTION:
        m = rx.search(stem)
        if m:
            # Strip surrounding dashes too: "[In the Style of -Steely Dan-]"
            # yields "-Steely Dan-" without it.
            charting, src = m.group(1).strip(" .:-–—"), label
            break

    # Remove the attribution clause and packaging noise from the title, in that
    # order -- the trailing "- X Tribute" form is part of the name, not a bracket.
    song = TRAILING_TRIBUTE.sub("", song)
    song = TITLE_NOISE.sub(" ", song)
    for rx in BARE_NOISE:
        song = rx.sub(" ", song)
    song = re.sub(rf"[{_OB}]\s*[{_CB}]", " ", song)       # emptied brackets
    song = _WS.sub(" ", song).strip(" -–—")
    # Drop an unbalanced bracket rather than stripping brackets wholesale. A blanket
    # .strip("()[]") turned "Slow Ride (2016 Remaster)" into "Slow Ride (2016
    # Remaster" -- it ate the closing paren and left the opening one. Filenames here
    # are also truncated at the 255-byte path limit, so genuinely unclosed brackets
    # do occur and have to be handled rather than mangled.
    for o, c in (("(", ")"), ("[", "]"), ("{", "}")):
        if song.count(o) > song.count(c):
            song = song[: song.rindex(o)].strip(" -–—")
    song = song.strip(" -–—")
    return artist.strip(), song, charting, src


def load_manifests(root: Path) -> list[tuple[str, str]]:
    """[(basename, reason)] from every tribute_manifest_*.csv under root."""
    out: list[tuple[str, str]] = []
    files = sorted(root.rglob("tribute_manifest_*.csv"))
    if not files:
        raise SystemExit(f"No tribute_manifest_*.csv found under {root}")
    for f in files:
        with f.open(newline="", encoding="utf-8", errors="replace") as fh:
            for r in csv.DictReader(fh):
                src = (r.get("source") or "").strip()
                if src:
                    out.append((os.path.basename(src), (r.get("reason") or "").strip()))
    return out


def artist_names(db: Path) -> set[str]:
    """Folded artist names known to the library, for the swap check below."""
    if not db.is_file():
        return set()
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        return {
            fold(a) for (a,) in conn.execute(
                "SELECT DISTINCT COALESCE(artist,'') FROM archive WHERE COALESCE(artist,'') <> ''")
            if fold(a)
        }
    finally:
        conn.close()


def library_index(db: Path) -> dict[str, set[str]]:
    """{folded title: {artists}} for everything catalogued.

    Includes every status, not just CATALOGUED: a copy sitting in DUPE_REVIEW is
    still a copy you own, and reporting "you don't have this" about a song in the
    review queue would send Grey shopping for something already on the disk.
    """
    if not db.is_file():
        return {}
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        idx: dict[str, set[str]] = {}
        for title, artist in conn.execute(
            "SELECT COALESCE(title,''), COALESCE(artist,'') FROM archive "
            "WHERE COALESCE(title,'') <> ''"
        ):
            k = fold(title)
            if k:
                idx.setdefault(k, set()).add(artist)
        return idx
    finally:
        conn.close()


#: Names that are obviously the packaging outfit rather than a performer. Used
#: only to decide whether a row deserves a false-positive flag.
JUNK_ARTIST = re.compile(
    r"\b(?:karaoke|tribute|backing|playback|midi|studio\s?group|studioke|"
    r"all[\s-]?stars?|hit\s+tunes|party\s+tyme|prosource|stingray|starlite|"
    r"monster|piano\s+(?:covers?|tribute)|collective|ensemble|players|"
    r"greatest\s+hits|workout|meditation|lullaby|hypnosis|sleep|vibrations|"
    r"mastermixers|superstars|chart\s+collective|immense\s+media|paris\s+music|"
    r"omnibus|musical\s+creations|dream\s+toys|genie\s+girls|guided)\b", re.I)


def build(rows: list[tuple[str, str]], idx: dict[str, set[str]],
          known_artists: set[str] | None = None) -> list[Row]:
    known_artists = known_artists or set()
    out: list[Row] = []
    for basename, reason in rows:
        q_artist, song, charting, src = parse_name(basename)
        r = Row(song=song, charted_by=charting, attribution_source=src,
                quarantined_as=q_artist, reason=reason, filename=basename)

        # Some names carry THREE parts -- "Piano Covers - Nirvana - Come as you
        # are" -- so splitting off the packager still leaves the real artist stuck
        # to the front of the song. Only split again when the leading fragment is
        # a name the library actually knows as an artist; splitting on any " - "
        # would decapitate titles like "One Tin Soldier - The Legend of Billy Jack".
        if not r.charted_by and " - " in song and known_artists:
            head, tail = song.split(" - ", 1)
            if fold(head) in known_artists and tail.strip():
                r.charted_by, r.attribution_source = head.strip(), "artist in the filename"
                song = tail.strip()
                r.song = song

        # Fall back to the library only when the filename said nothing.
        arts = sorted(a for a in idx.get(fold(song), set()) if a and not JUNK_ARTIST.search(a))
        r.library_artists = arts[:4]
        r.already_owned = bool(arts)
        if not r.charted_by and arts:
            r.charted_by, r.attribution_source = arts[0], "already in your library"

        # A real-looking artist with no substitute marker anywhere is the shape of
        # a wrongly-quarantined real recording -- Foghat's 2016 remaster.
        #
        # But NOT when the reason is `known_junk_artist:`. That list is curated by
        # hand, so a match on it is a decision someone already made deliberately.
        # Flagging those buried the two rows that actually matter under nine
        # Michael Sealey sleep-hypnosis tracks, which are correctly removed and
        # are not karaoke, so they tripped the "no marker" test for the wrong
        # reason. A review flag that fires on nine false alarms and two real ones
        # gets ignored, which is the failure it exists to prevent.
        from_curated_list = r.reason.lower().startswith("known_junk_artist")
        if (q_artist and not from_curated_list
                and not JUNK_ARTIST.search(q_artist)
                and not SUBSTITUTE_MARKERS.search(basename)):
            r.suspicious = "CHECK -- looks like a real artist, no karaoke/instrumental marker"

        # The SOURCE DATA is sometimes reversed, not just messy:
        #   "Stardust All Stars - Manfred Mann (Originally Performed by Sha La La)"
        # Manfred Mann is the artist and Sha La La the song, written the wrong way
        # round by whoever tagged it. Parsing that faithfully produces a row saying
        # the song "Manfred Mann" was charted by "Sha La La", which is nonsense a
        # reader would have to catch by eye. Detected by asking the library which
        # of the two names it knows as an ARTIST.
        if r.charted_by and known_artists:
            song_is_artist = fold(r.song) in known_artists
            attr_is_artist = fold(r.charted_by) in known_artists
            if song_is_artist and not attr_is_artist:
                r.suspicious = (f"SWAPPED in the source filename -- '{r.song}' is the artist, "
                                f"'{r.charted_by}' is the song")
                r.song, r.charted_by = r.charted_by, r.song
        out.append(r)
    return out


COLUMNS = ["song", "charted_by", "how_we_know", "already_in_library",
           "library_has_it_as", "quarantined_as", "needs_a_look",
           "quarantine_reason", "original_filename"]


def write_csv(rows: list[Row], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    # Sort so the work is at the top: songs you do NOT own, with a known artist.
    rows = sorted(rows, key=lambda r: (r.already_owned, not r.charted_by, r.song.lower()))
    with path.open("w", encoding="utf-8", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=COLUMNS)
        w.writeheader()
        for r in rows:
            w.writerow({
                "song": r.song,
                "charted_by": r.charted_by,
                "how_we_know": r.attribution_source,
                "already_in_library": "yes" if r.already_owned else "no",
                "library_has_it_as": " | ".join(r.library_artists),
                "quarantined_as": r.quarantined_as,
                "needs_a_look": r.suspicious,
                "quarantine_reason": r.reason,
                "original_filename": r.filename,
            })


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--root", type=Path, default=TRASH)
    p.add_argument("--db", type=Path, default=VAULT_DB)
    p.add_argument("--out", type=Path,
                   default=Path.home() / "Desktop" / "TRIBUTE_songs_and_real_artists.csv")
    args = p.parse_args(argv)

    manifest_rows = load_manifests(args.root)
    idx = library_index(args.db)
    rows = build(manifest_rows, idx, artist_names(args.db))
    write_csv(rows, args.out)

    known = sum(1 for r in rows if r.charted_by)
    owned = sum(1 for r in rows if r.already_owned)
    to_get = [r for r in rows if not r.already_owned]
    sus = [r for r in rows if r.suspicious]

    print(f"manifest rows read        {len(manifest_rows)}")
    print(f"distinct songs            {len({fold(r.song) for r in rows})}")
    print(provenance())
    print(f"library titles indexed    {len(idx):,}")
    print()
    print(f"charting artist identified {known}/{len(rows)}")
    for label, _ in ATTRIBUTION + [("already in your library", None)]:
        n = sum(1 for r in rows if r.attribution_source == label)
        if n:
            print(f"    {label:<26} {n}")
    print(f"  no attribution found      {len(rows) - known}")
    print()
    print(f"ALREADY in your library    {owned}   (nothing to do)")
    print(f"NOT in your library        {len(to_get)}   <- the shopping list")
    if sus:
        print()
        print(f"NEEDS A LOOK               {len(sus)}   (real artist, no karaoke marker)")
        for r in sus[:10]:
            print(f"    {r.quarantined_as} — {r.song}   [{r.reason}]")
    print()
    print(f"-> {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
