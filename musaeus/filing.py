"""
MUSAEUS — filing names: which folder an artist's tracks live under.

Grey's rule: "when I look for a song it will be first by artist, so group
them into one folder." The tag and the folder are not always the same
question. `Louis Armstrong, Billie Holiday, Sy Oliver & His Orchestra` is
an accurate performance credit and a terrible folder name; the folder
should say `Louis Armstrong`.

WHY THIS IS A LIST AND NOT A RULE
Because every rule that works also breaks something. Measured against the
real catalogue on 2026-09-09, a "strip the backing group" pattern that
correctly turns `Bill Haley & His Comets` into `Bill Haley` also turns:

    Sly & the Family Stone  -> Sly
    Kool & The Gang         -> Kool
    Hootie & the Blowfish   -> Hootie
    Earth, Wind & Fire      -> Earth
    Peter, Paul & Mary      -> Peter

"Person plus backing group" and "band name that contains an ampersand" are
not distinguishable by pattern -- the same reason Grey's `&` versus `and`
convention had to be a ruling rather than a regex. So this is an explicit
per-artist list, and an artist absent from it is filed under their tag.
Only 119 of 2,773 artists have a shape where the question even arises;
2,654 need nothing.

NOT THE SAME FILE AS artist_canon.tsv, AND NOT THE SAME DIRECTION
    artist_canon.tsv   raw tag       -> canonical TAG    (KC -> KC & The Sunshine Band)
    artist_filing.tsv  canonical tag -> FOLDER name      (Gladys Knight & The Pips -> Gladys Knight)

artist_canon repairs a truncated tag; this chooses where the tag's tracks
are filed. Running them in the wrong order, or confusing one for the other,
would expand a tag and then file it under the fragment it was expanded
from. `check()` exists to catch exactly that.

Lives in MetaData/ with the other authorities rather than in a database
column, deliberately. Grey's rulings are all in files, so a database
rebuild loses none of them -- which is what makes a rebuild safe. A column
would put this one ruling inside the thing being rebuilt.
"""

from __future__ import annotations

import logging
from pathlib import Path

logger = logging.getLogger(__name__)

FILENAME = "artist_filing.tsv"
#: Grey, 2026-10-17: per-song folder names, from Grey's Windows-iTunes folders.
#: audio_hash TAB folder TAB note. Only songs whose folder is a different act;
#: the folder's spelling style is the rule's. Folder only: tag and title stay.
SONGS_FILENAME = "folder_names.tsv"
#: Grey, 2026-10-18: act TAB person [TAB genre]. A person with two or more act
#: folders gets a person folder holding them: Genre/Person/Act/Album/song, in
#: the library and the car. The genre is the person's (one place to look).
PERSONS_FILENAME = "person_folders.tsv"

HEADER = """\
# MUSAEUS Artist Filing — artist_tag TAB folder_name
#
# Which FOLDER an artist's tracks are filed under. An artist not listed
# here is filed under their own tag, which is the right answer for about
# 95% of the library.
#
# Add a line only where the tag is a performance credit rather than the
# name you would look under:
#
#     Gladys Knight & The Pips<TAB>Gladys Knight
#
# Do NOT add a line to shorten a real band name. `Kool & The Gang` and
# `Earth, Wind & Fire` are names, not credits, and belong in their own
# folders under their full names.
#
# Lines starting with # are ignored. Blank folder_name is an error, not
# "file under nothing".
"""


class FilingError(Exception):
    """The filing map is unusable and the caller must not guess."""


def load(meta_dir: Path) -> dict[str, str]:
    """artist tag -> folder name. Missing file is an empty map, not an error.

    An absent authority file means "no artist has been given a special
    folder", which is the correct starting state and exactly what every
    caller falls back to anyway.
    """
    path = Path(meta_dir) / FILENAME
    songs, song_genres = _load_songs(meta_dir)
    if not path.exists():
        empty = FilingMap({}, songs)
        empty.song_genres = song_genres
        empty.persons = _load_persons(meta_dir)
        return empty
    out: dict[str, str] = {}
    folded: dict[str, str] = {}
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise FilingError(f"{FILENAME} could not be read: {exc}") from exc

    for lineno, raw in enumerate(text.splitlines(), 1):
        line = raw.rstrip("\n")
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        if "\t" not in line:
            raise FilingError(
                f"{FILENAME} line {lineno}: expected 'artist<TAB>folder', found no tab. "
                f"A space-separated line here would silently file an artist under a "
                f"name nobody chose."
            )
        tag, folder = line.split("\t", 1)
        tag, folder = tag.strip(), folder.strip()
        if not tag or not folder:
            raise FilingError(
                f"{FILENAME} line {lineno}: both sides must be non-empty "
                f"(got {tag!r} -> {folder!r})"
            )
        if tag in out and out[tag] != folder:
            raise FilingError(
                f"{FILENAME} line {lineno}: {tag!r} is already filed under "
                f"{out[tag]!r}; a second answer of {folder!r} makes the file "
                f"ambiguous rather than overriding it."
            )
        # Rulings are found whatever their capitals (ruling_for), so two tags
        # that differ only in capitals ARE the same tag -- and two answers for
        # it are the same ambiguity as above (cloud review of #38).
        twin = folded.get(tag.casefold())
        if twin is not None and out[twin] != folder:
            raise FilingError(
                f"{FILENAME} line {lineno}: {tag!r} and {twin!r} differ only in capitals "
                f"but file under {folder!r} and {out[twin]!r}; that makes the file "
                f"ambiguous."
            )
        folded.setdefault(tag.casefold(), tag)
        out[tag] = folder
    loaded = FilingMap(out, songs)
    loaded.song_genres = song_genres
    loaded.persons = _load_persons(meta_dir)
    return loaded


def ruling_for(filing: dict[str, str] | None, artist: str | None) -> str | None:
    """Grey's filing ruling for *artist*, found whatever its capitals, or None.

    Names are written with every word capitalised since 2026-09-25 while the
    file keeps the spelling each ruling was made under ("Glenn Miller and His
    Orchestra"); an exact-case lookup quietly stopped applying them. The ONE
    lookup: organize (the ALAC tiers) and folder_for (the car tree) both use
    it, so the two trees cannot file a credit in different folders.
    """
    if not filing or not artist:
        return None
    if artist in filing:
        return filing[artist]
    index = getattr(filing, "folded", None)
    if index is None:  # a plain dict (tests, callers building their own)
        index = {tag.casefold(): tag for tag in filing}
    tag = index.get(artist.casefold())
    return filing[tag] if tag is not None else None


class FilingMap(dict[str, str]):
    """tag -> folder, with a casefolded index built once rather than per track."""

    def __init__(self, entries: dict[str, str], songs: dict[str, str] | None = None) -> None:
        super().__init__(entries)
        self.folded = {tag.casefold(): tag for tag in entries}
        #: audio_hash -> folder (folder_names.tsv); checked before any ruling.
        self.songs = dict(songs or {})
        #: audio_hash -> genre folder, where Grey's list gives one (the act's
        #: genre: one genre per act, Grey 2026-10-18).
        self.song_genres: dict[str, str] = {}
        #: act_key(act folder) -> (person, person's genre or None).
        self.persons: dict[str, tuple[str, str | None]] = {}


def song_folder(filing: dict[str, str] | None, audio_hash: str | None) -> str | None:
    """Grey's per-song folder for *audio_hash*, or None."""
    songs = getattr(filing, "songs", None)
    if not songs or not audio_hash:
        return None
    folder = songs.get(audio_hash)
    return str(folder) if folder else None


def song_genre(filing: dict[str, str] | None, audio_hash: str | None) -> str | None:
    """The genre folder Grey's list gives *audio_hash*, or None (its own)."""
    genres = getattr(filing, "song_genres", None)
    if not genres or not audio_hash:
        return None
    genre = genres.get(audio_hash)
    return str(genre) if genre else None


def person_for(
    filing: dict[str, str] | None, act_folder: str | None
) -> tuple[str, str | None] | None:
    """(person, genre or None) for an act folder in Grey's person list, or None.
    Matched by act_key, so "The X" / "X, The" and "&" / "and" agree."""
    persons = getattr(filing, "persons", None)
    if not persons or not act_folder:
        return None
    from .artist_form import act_key

    found = persons.get(act_key(act_folder))
    return (str(found[0]), found[1]) if found else None


def _allowed_genres(meta_dir: Path) -> dict[str, str] | None:
    """_genre_key -> the spelling Genre_Allowed.txt gives it, or None."""
    path = Path(meta_dir) / "Genre_Allowed.txt"
    if not path.exists():
        return None
    return {
        _genre_key(line): line.strip()
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    }


def _genre_key(genre: str) -> str:
    # "R&B/Funk/Soul" and the folder spelling "R&B-Funk-Soul" are one genre.
    return genre.strip().casefold().replace("-", "/").replace("_", " ")


def _load_persons(meta_dir: Path) -> dict[str, tuple[str, str | None]]:
    """act_key(act) -> (person, the person's ONE genre).

    Refused rather than guessed (review of #208): a person with no genre
    or two, a genre outside Genre_Allowed.txt (a typo would make a new
    top-level folder), a person with fewer than two acts (a typo in the
    person column makes exactly that), and one person spelled two ways."""
    path = Path(meta_dir) / PERSONS_FILENAME
    if not path.exists():
        return {}
    from .artist_form import act_key

    acts: dict[str, tuple[str, int]] = {}  # act_key -> (person, line)
    spelled: dict[str, str] = {}  # person casefold -> spelling
    genres: dict[str, set[str]] = {}
    for lineno, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not raw.strip() or raw.lstrip().startswith("#"):
            continue
        parts = [p.strip() for p in raw.split("\t")]
        if len(parts) < 2 or not parts[0] or not parts[1]:
            raise FilingError(
                f"{PERSONS_FILENAME} line {lineno}: expected 'act<TAB>person', got {raw!r}"
            )
        act, person = parts[0], parts[1]
        twin = spelled.setdefault(person.casefold(), person)
        if twin != person:
            raise FilingError(
                f"{PERSONS_FILENAME} line {lineno}: {person!r} and {twin!r} are one person "
                f"spelled two ways; use one spelling."
            )
        key = act_key(act)
        if key in acts and acts[key][0] != person:
            raise FilingError(
                f"{PERSONS_FILENAME} line {lineno}: {act!r} is already under "
                f"{acts[key][0]!r}; a second person makes the file ambiguous."
            )
        acts[key] = (person, lineno)
        if len(parts) >= 3 and parts[2]:
            genres.setdefault(person, set()).add(parts[2])
    allowed = _allowed_genres(meta_dir)
    per_person: dict[str, str | None] = {}
    for person in set(spelled.values()):
        named = genres.get(person, set())
        if len({_genre_key(g) for g in named}) > 1:
            raise FilingError(
                f"{PERSONS_FILENAME}: {person!r} is given {len(named)} genres "
                f"({', '.join(sorted(named))}); a person sits under one."
            )
        if not named:
            # Without one, each act would follow its own songs' genre and the
            # person would get a folder in each (Codex review of #208).
            raise FilingError(
                f"{PERSONS_FILENAME}: {person!r} has no genre; give one on any of "
                f"their lines so all their acts sit in one place."
            )
        # min(): one answer whatever the set's order. Then the allowed list's
        # own spelling, so "rock" cannot make a "rock" folder beside "Rock"
        # (review of #208).
        genre = min(named)
        if allowed is not None:
            if _genre_key(genre) not in allowed:
                raise FilingError(
                    f"{PERSONS_FILENAME}: {person!r}'s genre {genre!r} is not in "
                    f"Genre_Allowed.txt -- check the spelling"
                )
            genre = allowed[_genre_key(genre)]
        if sum(1 for p, _ in acts.values() if p == person) < 2:
            raise FilingError(
                f"{PERSONS_FILENAME}: {person!r} has only one act; a person folder is "
                f"for two or more (a typo in the person column?)"
            )
        per_person[person] = genre
    return {key: (person, per_person[person]) for key, (person, _) in acts.items()}


def _load_songs(meta_dir: Path) -> tuple[dict[str, str], dict[str, str]]:
    path = Path(meta_dir) / SONGS_FILENAME
    if not path.exists():
        return {}, {}
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise FilingError(f"{SONGS_FILENAME} could not be read: {exc}") from exc
    out: dict[str, str] = {}
    genres: dict[str, str] = {}
    for lineno, raw in enumerate(text.splitlines(), 1):
        if not raw.strip() or raw.lstrip().startswith("#"):
            continue
        # audio_hash TAB folder [TAB genre [TAB note]]: a blank genre keeps the
        # song's own.
        parts = raw.split("\t")
        h, folder = (parts[0].strip(), parts[1].strip()) if len(parts) >= 2 else ("", "")
        if not h or not folder:
            raise FilingError(
                f"{SONGS_FILENAME} line {lineno}: expected 'audio_hash<TAB>folder', got {raw!r}"
            )
        if h in out and out[h] != folder:
            raise FilingError(
                f"{SONGS_FILENAME} line {lineno}: that song is already filed under "
                f"{out[h]!r}; a second answer {folder!r} makes the file ambiguous."
            )
        out[h] = folder
        if len(parts) >= 4 and parts[2].strip():
            genres[h] = parts[2].strip()
    return out, genres


def folder_for(artist: str | None, filing: dict[str, str]) -> str:
    """The folder name for a tag. No ruling means: use the tag."""
    if not artist:
        return "Unknown Artist"
    ruled = ruling_for(filing, artist)
    if ruled is None:
        return artist
    # In the stored form, as the library files it, so a ruled credit and the
    # artist's other tracks share one folder (organize.library_relpath).
    from .stages.normalize import stored_artist

    return stored_artist(ruled)


def check(filing: dict[str, str], canon: dict[str, str] | None = None) -> list[str]:
    """Problems worth reporting. Empty list means checked and clean.

    Distinct from returning nothing at all: a caller that cannot tell
    "I looked and found nothing" from "I did not look" will report a
    healthy library either way.
    """
    problems: list[str] = []
    for tag, folder in sorted(filing.items()):
        if folder == tag:
            problems.append(f"{tag!r} maps to itself — the line has no effect and can be removed")
        if folder in filing and filing[folder] != folder:
            problems.append(
                f"{tag!r} is filed under {folder!r}, which is itself filed under "
                f"{filing[folder]!r}; filing does not follow chains, so this "
                f"artist lands in {folder!r} and not where the second line says"
            )
        # The artist_canon collision. canon expands a truncated tag; if a
        # filing key is something canon would rewrite, the tag never
        # reaches this map under that spelling and the line is dead.
        if canon and tag in canon and canon[tag] != tag:
            problems.append(
                f"{tag!r} is rewritten by artist_canon.tsv to {canon[tag]!r} before "
                f"filing sees it, so this line never applies. File {canon[tag]!r} instead."
            )
    return problems
