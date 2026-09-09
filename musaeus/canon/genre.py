#!/usr/bin/env python3
"""
MUSAEUS — Genre Canon

Two files back the genre system, named as the vault actually spells them:
  <vault>/MetaData/Genre_Allowed.txt        — one allowed genre per line
  <vault>/MetaData/Genre_Canonical_Map.txt  — "raw => canonical" per line

Both names and the map's separator were wrong here until 2026-09-08. The
docstring said `genre_map.tsv` with a TAB, and the loader below records what
that cost: the real file has used " => " since it was written, so not one of
its 51 rules ever loaded, and with no Genre_Allowed.txt either, resolve()
returned None for every genre ever passed to it. GenreCanon was wired into
EnrichStage and doing nothing at all. A tab is still accepted, so a
hand-written file in the old shape keeps working.

Design:
  - Allowed list: genres accepted as-is (case-insensitive).
  - Map: explicit raw → canonical overrides.
  - Fuzzy match against allowed list as final fallback.
  - Returns None when no suitable match found (caller decides what to do).
"""

from __future__ import annotations

import logging
from pathlib import Path

logger = logging.getLogger(__name__)

_FUZZY_THRESHOLD = 82


class GenreCanon:
    """
    Resolve a raw genre string to a canonical, allowed form.

    Usage:
        gc = GenreCanon(allowed_path, map_path)
        gc.resolve("Hip-Hop/Rap")     # → "Hip-Hop"  (via map or fuzzy)
        gc.resolve("Unknown Genre")   # → None
    """

    def __init__(self, allowed_path: Path, map_path: Path) -> None:
        self._allowed_path = allowed_path
        self._map_path = map_path
        self._allowed: set[str] = set()
        self._allowed_lower: list[str] = []  # for fuzzy matching
        self._map: dict[str, str] = {}  # lower_raw → canonical
        self._load()

    # ── loaders ───────────────────────────────────────────────────────────────

    def _load(self) -> None:
        self._allowed.clear()
        self._map.clear()

        if self._allowed_path.exists():
            with open(self._allowed_path, encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if line and not line.startswith("#"):
                        self._allowed.add(line)

        # Derived unconditionally, OUTSIDE the exists() check, so the two can
        # never disagree about how many genres there are.
        #
        # M-09, 2026-09-08: this assignment used to sit inside the branch
        # above. _allowed was cleared every load; _allowed_lower was only
        # rewritten when the file was present. Remove the file and reload,
        # and _allowed held 0 entries while _allowed_lower still held the
        # previous 3 -- after which resolve()'s `zip(..., strict=True)` did
        # exactly what it was written to do and raised
        # "zip() argument 2 is longer than argument 1", where the contract
        # says return None.
        #
        # The strict zip is not the bug and stays. It turned a silent
        # mis-pairing of genres with the wrong lower-cased names into a loud
        # failure; without it, fuzzy matching would have scored each genre
        # against some other genre's text and returned a plausible wrong
        # answer. Fix the invariant, keep the alarm.
        self._allowed_lower = [g.lower() for g in self._allowed]

        if self._map_path.exists():
            with open(self._map_path, encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if not line or line.startswith("#"):
                        continue
                    # Accept "raw => canonical" as well as a tab.
                    #
                    # The tab-only split was silent dead code: the real
                    # Genre_Canonical_Map.txt in the vault has used " => "
                    # since it was written, so not one of its 51 rules ever
                    # loaded. Combined with a Genre_Allowed.txt that did not
                    # exist, resolve() returned None for every genre ever
                    # passed to it -- meaning GenreCanon has been wired into
                    # EnrichStage and doing nothing at all. Found 2026-08-21
                    # while checking whether the canon could reduce a
                    # MusicBrainz genre list to one answer.
                    if "=>" in line:
                        raw, _, canon = line.partition("=>")
                    else:
                        parts = line.split("\t", 1)
                        if len(parts) != 2:
                            continue
                        raw, canon = parts
                    raw, canon = raw.strip(), canon.strip()
                    if raw and canon:
                        self._map[raw.lower()] = canon

    # ── public API ────────────────────────────────────────────────────────────

    def resolve(self, raw: str) -> str | None:
        """
        Return the canonical genre for *raw*, or None if unresolvable.

        Resolution order:
          1. Exact allowed-list match (case-insensitive)
          2. Explicit map entry
          3. Fuzzy match against allowed list
        """
        if not raw:
            return None

        # 1. Exact match against allowed list (case-insensitive)
        lower = raw.strip().lower()
        for allowed in self._allowed:
            if allowed.lower() == lower:
                return allowed

        # 2. Explicit map entry
        canon = self._map.get(lower)
        if canon:
            return canon

        # 3. Fuzzy match
        try:
            from rapidfuzz import fuzz  # type: ignore[import-untyped]

            best_score: float = 0
            best_genre = ""
            for allowed, allowed_lower in zip(self._allowed, self._allowed_lower, strict=True):
                score = fuzz.ratio(lower, allowed_lower)
                if score > best_score:
                    best_score = score
                    best_genre = allowed
            if best_score >= _FUZZY_THRESHOLD:
                logger.debug("genre fuzzy: %r → %r (score=%d)", raw, best_genre, best_score)
                return best_genre
        except ImportError:
            pass

        return None  # unresolvable

    def is_allowed(self, genre: str) -> bool:
        return genre.strip() in self._allowed

    def all_allowed(self) -> list[str]:
        return sorted(self._allowed)

    def reload(self) -> None:
        self._load()

    def __len__(self) -> int:
        return len(self._allowed)

    def __repr__(self) -> str:
        return f"GenreCanon(allowed={len(self._allowed)}, mapped={len(self._map)})"
