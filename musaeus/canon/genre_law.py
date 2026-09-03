#!/usr/bin/env python3
"""
MUSAEUS — Genre Law (artist -> genre authority)

Backed by <vault>/MetaData/MasterLaw.csv:
    artist,genre

Distinct from GenreCanon (canon/genre.py), and the difference matters:

  GenreCanon answers "is this genre STRING allowed, and what is its
  canonical spelling?" -- it maps "Hip-Hop/Rap" to "Hip-Hop". It knows
  nothing about who the artist is.

  GenreLaw answers "what genre does THIS ARTIST belong to?" It is the
  hand-curated artist->genre table salvaged from ORPHEUS/NEXUS (2,398
  artists), and it is the only thing in MUSAEUS that can say a file's
  genre is *wrong* rather than merely oddly spelled.

Exact-match only, deliberately, for the same reason ArtistCanon.
resolve_exact() exists: an answer that will be written into
archive.genre without a human in the loop must come from someone having
written that mapping down, never from a similarity score. Fuzzy artist
matching is especially unsafe here because near-identical artist names
are routinely different acts in different genres.

Separator handling: MUSAEUS sanitises "/" out of genre strings for
filesystem safety, so the library stores "Disco-Electronic" where
MasterLaw says "Disco/Electronic". Those are the SAME genre. Comparing
them naively reports ~1,000 conflicts that do not exist, which is why
comparison goes through _norm() rather than ==.
"""

from __future__ import annotations

import csv
import logging
import re
from pathlib import Path

logger = logging.getLogger(__name__)

_WS_RE = re.compile(r"\s+")


class GenreLaw:
    """Artist -> canonical genre, from MasterLaw.csv."""

    def __init__(self, csv_path: Path) -> None:
        self._path = csv_path
        self._map: dict[str, str] = {}
        self._load()

    def _load(self) -> None:
        self._map.clear()
        if not self._path.exists():
            logger.info("[genre-law] no MasterLaw.csv at %s -- law unavailable", self._path)
            return
        with open(self._path, encoding="utf-8", newline="") as fh:
            for row in csv.DictReader(fh):
                artist = (row.get("artist") or "").strip()
                genre = (row.get("genre") or "").strip()
                if artist and genre:
                    self._map[self._key(artist)] = genre

    @staticmethod
    def _key(artist: str) -> str:
        """Lookup key for an artist, folding the article convention.

        This used to lowercase and collapse whitespace only, which meant the
        law and the library disagreed about what an artist is called. The
        library stores "5th Dimension, The" (normalize._move_article_to_suffix),
        while MasterLaw still held pre-normalisation spellings like
        "5th Dimension (the)" and "The Byrds" -- so those rules simply never
        fired. 246 rules were dormant for that reason alone, and because a
        dormant rule looks exactly like an absent one, nothing reported it.

        Folding here rather than rewriting the file means a hand-edited row
        keeps working whichever way its author spells the article.
        """
        s = _WS_RE.sub(" ", artist.strip().lower())
        # "the beatles" / "beatles, the" / "beatles (the)" -> "beatles"
        if s.startswith("the "):
            s = s[4:]
        elif s.endswith(", the"):
            s = s[:-5]
        elif s.endswith(" (the)"):
            s = s[:-6]
        return s.strip()

    @staticmethod
    def _norm(genre: str) -> str:
        """Compare-form for a genre string.

        Folds the "/" vs "-" separator difference that Sanitize introduces,
        and collapses whitespace. Used ONLY for comparison -- never for
        deciding what to write, which is always MasterLaw's own spelling.
        """
        return _WS_RE.sub(" ", genre.replace("/", "-").strip().lower())

    def genre_for(self, artist: str) -> str | None:
        """MasterLaw's genre for *artist*, or None if it has no opinion."""
        if not artist:
            return None
        return self._map.get(self._key(artist))

    def agrees(self, artist: str, genre: str) -> bool | None:
        """True/False if the law has an opinion on this pairing, else None."""
        law = self.genre_for(artist)
        if law is None:
            return None
        return self._norm(law) == self._norm(genre)

    @property
    def genres(self) -> set[str]:
        """Every genre the law actually assigns -- the closed vocabulary.

        Derived from the file rather than hardcoded, so it cannot drift from
        what the law will really hand out. Callers checking membership should
        compare through _norm(), since "R&B/Funk/Soul" and "R&B-Funk-Soul"
        are the same genre spelled two ways.
        """
        return set(self._map.values())

    def permits(self, genre: str) -> bool:
        """True if *genre* is in the closed vocabulary, "/" vs "-" aside."""
        if not genre:
            return False
        return self._norm(genre) in {self._norm(g) for g in self._map.values()}

    def __len__(self) -> int:
        return len(self._map)

    def __repr__(self) -> str:
        return f"GenreLaw(path={self._path}, artists={len(self)})"
