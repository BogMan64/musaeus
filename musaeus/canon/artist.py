#!/usr/bin/env python3
"""
MUSAEUS — Artist Canon

Maps raw artist name strings to their canonical forms.
Backed by a plain TSV file: <vault>/MetaData/artist_canon.tsv
  raw_name\tcanonical_name

Design:
  - Loaded once at startup, then in-memory.
  - Fuzzy matching (rapidfuzz ratio >= 88) as a fallback when no exact entry exists.
  - Canon is append-only: new mappings are added, existing ones never removed.
  - Thread-safe reads (dict lookup); writes serialised through CanonManager.
"""

from __future__ import annotations

import logging
import threading
from pathlib import Path

logger = logging.getLogger(__name__)

_FUZZY_THRESHOLD = 88   # rapidfuzz ratio (0–100)


class ArtistCanon:
    """
    In-memory artist name → canonical name map, backed by a TSV file.

    Usage:
        canon = ArtistCanon(Path("~/.config/musaeus/artist_canon.tsv"))
        canon_name = canon.resolve("THE Beatles")   # → "The Beatles"
        canon.add("Portishead ", "Portishead")       # trim + persist
    """

    def __init__(self, tsv_path: Path) -> None:
        self._path = tsv_path
        self._lock = threading.Lock()
        self._map: dict[str, str] = {}   # normalised_key → canonical
        self._load()

    # ── persistence ───────────────────────────────────────────────────────────

    def _load(self) -> None:
        self._map.clear()
        if not self._path.exists():
            return
        with open(self._path, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                parts = line.split("\t", 1)
                if len(parts) == 2:
                    raw, canon = parts
                    self._map[self._norm(raw)] = canon.strip()

    def _save(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with open(self._path, "w", encoding="utf-8") as fh:
            fh.write("# Musaeus Artist Canon — raw_name<TAB>canonical_name\n")
            for raw_norm, canon in sorted(self._map.items()):
                fh.write(f"{raw_norm}\t{canon}\n")

    @staticmethod
    def _norm(name: str) -> str:
        """Normalise a name for keying: lowercase, collapse spaces."""
        return " ".join(name.lower().split())

    # ── public API ────────────────────────────────────────────────────────────

    def resolve(self, raw: str) -> str:
        """
        Return the canonical form of *raw*.
        Falls back to fuzzy match, then returns *raw* unchanged if no match.
        """
        if not raw:
            return raw
        key = self._norm(raw)
        exact = self._map.get(key)
        if exact:
            return exact

        # Fuzzy fallback
        try:
            from rapidfuzz import fuzz  # type: ignore[import-untyped]
            best_score = 0
            best_canon = raw
            for stored_key, canon in self._map.items():
                score = fuzz.ratio(key, stored_key)
                if score > best_score:
                    best_score = score
                    best_canon = canon
            if best_score >= _FUZZY_THRESHOLD:
                logger.debug(
                    "artist canon fuzzy match: %r → %r (score=%d)",
                    raw, best_canon, best_score,
                )
                return best_canon
        except ImportError:
            pass   # rapidfuzz optional

        return raw   # no match — return as-is

    def add(self, raw: str, canonical: str) -> None:
        """Append a new canon mapping and persist to disk."""
        key = self._norm(raw)
        with self._lock:
            self._map[key] = canonical.strip()
            self._save()

    def has(self, raw: str) -> bool:
        return self._norm(raw) in self._map

    def all_entries(self) -> list[tuple[str, str]]:
        """Return all (raw_normalised, canonical) pairs, sorted."""
        return sorted(self._map.items())

    def reload(self) -> None:
        with self._lock:
            self._load()

    def __len__(self) -> int:
        return len(self._map)

    def __repr__(self) -> str:
        return f"ArtistCanon(path={self._path}, entries={len(self)})"
