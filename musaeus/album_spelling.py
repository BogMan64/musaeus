"""One spelling per album, per artist (Grey, 2026-10-06).

51 album folders were split in two by capital letters alone -- "Kind of Blue" beside
"Kind Of Blue", "Back in Black" beside "Back In Black" -- because each new batch kept
its source's spelling. A FAT32 stick cannot hold both, and the car and iPhone
libraries (no genre level) joined songs from two genres into one clash. Grey asked
that MUSAEUS keep one spelling by itself.

The spelling already filed for that artist wins; a new song takes it. When none is
filed yet, or more than one is, the official style wins (small words lower case,
numerals and initials upper case), then the spelling most songs carry. Only a
difference of capital letters is changed: apostrophe style and a final full stop
make no folder of their own, so each song keeps its own.
"""

from __future__ import annotations

import re
from collections import Counter, defaultdict
from collections.abc import Callable
from typing import Any

from .artist_form import folder_artist

_SMALL = frozenset(
    [
        "of",
        "the",
        "in",
        "at",
        "for",
        "to",
        "a",
        "an",
        "and",
        "on",
        "as",
        "is",
        "by",
        "from",
        "with",
        "or",
        "but",
        "nor",
    ]
)
_ROMAN = re.compile(r"^(?=[MDCLXVI])M*(C[MD]|D?C{0,3})(X[CL]|L?X{0,3})(I[XV]|V?I{0,3})$")
_INITIALS = re.compile(r"(?:[A-Z]\.)+[A-Z]?\.?")


def _shape(album: str) -> str:
    """The album as its folder spells it: apostrophe style and a final full stop
    make no folder of their own, so they are left as each source has them."""
    return album.replace("’", "'").rstrip(". ").strip()


def album_key(album: str) -> str:
    """Two spellings of one album share this key."""
    return _shape(album).casefold()


def style_score(album: str) -> int:
    """Higher is closer to how album titles are officially written."""
    score = 0
    for i, tok in enumerate(re.findall(r"[\w'’.!]+", album)):
        if i > 0 and tok in _SMALL:
            score += 1
        elif i > 0 and tok[:1].islower() and not tok.lower().startswith(("d'", "d’")):
            score -= 1
        if len(tok) > 1 and tok.isupper() and _ROMAN.match(tok):
            score += 2
        elif 1 < len(tok) <= 5 and tok.isupper() and tok.isalnum():
            score += 1
        if _INITIALS.fullmatch(tok):
            score += 2
    return score


def plan_album_spelling(rows: list[Any]) -> list[tuple[Any, str]]:
    """[(row, spelling it should take)] for rows whose album is spelled otherwise.

    *rows* need file_path, artist, album, finalized_at and mb_artist_name.
    """
    groups: dict[tuple[str, str], list[Any]] = defaultdict(list)
    for r in rows:
        album = (r["album"] or "").strip()
        if album:
            lead = folder_artist(r["artist"] or "", r["mb_artist_name"])
            groups[(lead.casefold(), album_key(album))].append(r)
    changes: list[tuple[Any, str]] = []
    for members in groups.values():
        spellings = Counter(r["album"].strip() for r in members)
        if len(spellings) < 2:
            continue
        filed = {r["album"].strip() for r in members if r["finalized_at"]}
        best = max(spellings, key=lambda s: (s in filed, style_score(s), spellings[s], s))
        # Only capital letters split a folder; anything else is left alone.
        changes += [(r, best) for r in members if _shape(r["album"]) != _shape(best)]
    return changes


def unify_album_spelling(
    conn: Any, log_event: Callable[..., Any], stage: str, dry_run: bool
) -> list[tuple[str, str, str]]:
    """Give every album one spelling per artist. Returns [(file_path, old, new)]."""
    # mb_artist_name arrives with MusicBrainz enrichment; a new vault lacks it.
    has_mb = any(r[1] == "mb_artist_name" for r in conn.execute("PRAGMA table_info(archive)"))
    mb_col = "mb_artist_name" if has_mb else "NULL AS mb_artist_name"
    rows = conn.execute(
        f"SELECT rowid AS rid, file_path, artist, album, finalized_at, {mb_col} "
        "FROM archive WHERE status = 'CATALOGUED' AND album IS NOT NULL AND trim(album) <> ''"
    ).fetchall()
    done = []
    for r, best in plan_album_spelling(rows):
        done.append((r["file_path"], r["album"], best))
        if not dry_run:
            conn.execute("UPDATE archive SET album = ? WHERE rowid = ?", (best, r["rid"]))
            log_event(
                "ALBUM_SPELLING_UNIFIED",
                file_path=r["file_path"],
                old_value=r["album"],
                new_value=best,
                stage=stage,
                note="one spelling per album per artist: the one already filed, else the official style",
            )
    if done and not dry_run:
        conn.commit()
    return done
