#!/usr/bin/env python3
"""
MUSAEUS — Fuzzy string matching utilities.

Used by the Scholar stage and de-duplication logic to match artist / album /
title strings that differ only in punctuation, articles, casing, etc.

Design choices:
  - rapidfuzz (not fuzzywuzzy) — faster, no Levenshtein licence issues
  - threshold >= 85 matches ORPHEUS convention
  - normalize() is deterministic and side-effect-free
  - No global state; all functions are pure

Usage:
    from musaeus.fuzzy import normalize, similarity, is_match

    a = normalize("The Beatles")   # → "beatles"
    s = similarity("Abbey Road", "Abbey Rd") # → 87
    if is_match("abbey road", "abbey rd"):
        ...
"""

from __future__ import annotations

import re
import unicodedata

try:
    from rapidfuzz import fuzz as _fuzz

    _HAVE_RAPIDFUZZ = True
except ImportError:
    _HAVE_RAPIDFUZZ = False
    import difflib as _difflib  # type: ignore[assignment]


# ── Normalization ─────────────────────────────────────────────────────────────

_LEADING_ARTICLES = re.compile(
    r"^\s*(the|a|an)\s+",
    re.IGNORECASE,
)
_PUNCTUATION = re.compile(r"[^\w\s]")
_WHITESPACE = re.compile(r"\s+")


def normalize(text: str) -> str:
    """
    Canonical form for fuzzy comparison.

    Steps:
      1. Unicode NFKD decomposition (accents → base chars)
      2. Lowercase
      3. Strip leading articles: "the", "a", "an"
      4. Remove all punctuation
      5. Collapse whitespace
      6. Strip

    Designed to be idempotent: normalize(normalize(x)) == normalize(x)
    """
    if not text:
        return ""
    # Decompose and strip accents
    text = unicodedata.normalize("NFKD", text)
    text = "".join(c for c in text if unicodedata.category(c) != "Mn")
    text = text.lower()
    text = _LEADING_ARTICLES.sub("", text)
    text = _PUNCTUATION.sub(" ", text)
    text = _WHITESPACE.sub(" ", text).strip()
    return text


# ── Similarity scoring ────────────────────────────────────────────────────────

DEFAULT_THRESHOLD = 85  # matches ORPHEUS / NexusII convention


def similarity(a: str, b: str, pre_normalized: bool = False) -> float:
    """
    Return a similarity score in [0, 100] between two strings.
    Uses rapidfuzz.fuzz.ratio if available, else difflib SequenceMatcher.

    If pre_normalized=False (default), normalize() is applied first.
    """
    if not pre_normalized:
        a = normalize(a)
        b = normalize(b)

    if _HAVE_RAPIDFUZZ:
        return _fuzz.ratio(a, b)
    else:
        # difflib returns 0..1; scale to 0..100
        return _difflib.SequenceMatcher(None, a, b).ratio() * 100


def token_similarity(a: str, b: str, pre_normalized: bool = False) -> float:
    """
    Token-sort similarity — handles word-order differences.
    E.g. "Pink Floyd Animals" ↔ "Animals Pink Floyd" → 100

    Falls back to similarity() if rapidfuzz unavailable.
    """
    if not pre_normalized:
        a = normalize(a)
        b = normalize(b)

    if _HAVE_RAPIDFUZZ:
        return _fuzz.token_sort_ratio(a, b)
    return similarity(a, b, pre_normalized=True)


def partial_similarity(a: str, b: str, pre_normalized: bool = False) -> float:
    """
    Partial-string similarity — the shorter string is matched anywhere in the longer.
    Useful for "Radiohead" ↔ "Radiohead (feat. Thom Yorke solo)" comparisons.
    """
    if not pre_normalized:
        a = normalize(a)
        b = normalize(b)

    if _HAVE_RAPIDFUZZ:
        return _fuzz.partial_ratio(a, b)
    return similarity(a, b, pre_normalized=True)


def best_similarity(a: str, b: str) -> float:
    """
    Return the best of ratio, token_sort_ratio, and partial_ratio.
    Conservative: use this when you're unsure which strategy fits best.
    """
    na, nb = normalize(a), normalize(b)
    return max(
        similarity(na, nb, pre_normalized=True),
        token_similarity(na, nb, pre_normalized=True),
        partial_similarity(na, nb, pre_normalized=True),
    )


# ── Match predicate ───────────────────────────────────────────────────────────

def is_match(a: str, b: str, threshold: int = DEFAULT_THRESHOLD) -> bool:
    """
    Return True if a and b are considered the same string.
    Uses best_similarity() so token-order / partial matches are caught.
    """
    return best_similarity(a, b) >= threshold


# ── Batch matching ────────────────────────────────────────────────────────────

def best_match(
    query: str,
    candidates: list[str],
    threshold: int = DEFAULT_THRESHOLD,
) -> tuple[str | None, float]:
    """
    Find the best matching candidate for a query string.
    Returns (best_candidate, score) or (None, 0.0) if nothing exceeds threshold.
    """
    best: str | None = None
    best_score = 0.0
    nq = normalize(query)
    for candidate in candidates:
        nc = normalize(candidate)
        score = best_similarity(nq, nc)
        if score > best_score:
            best_score = score
            best = candidate
    if best_score >= threshold:
        return best, best_score
    return None, best_score


def group_near_duplicates(
    items: list[str],
    threshold: int = DEFAULT_THRESHOLD,
) -> list[list[str]]:
    """
    Cluster a list of strings into groups where any two members match.
    Naive O(n²) implementation — fine for typical music library sizes.
    Returns a list of groups (each group is a list of matching strings).
    """
    groups: list[list[str]] = []
    assigned: set[int] = set()

    for i, item in enumerate(items):
        if i in assigned:
            continue
        group = [item]
        assigned.add(i)
        for j, other in enumerate(items):
            if j <= i or j in assigned:
                continue
            if is_match(item, other, threshold):
                group.append(other)
                assigned.add(j)
        groups.append(group)

    return groups
