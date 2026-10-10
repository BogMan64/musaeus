#!/usr/bin/env python3
"""
MUSAEUS — Stage: NearDupe
Metadata-based near-duplicate detection (no audio fingerprinting required).

What it does:
  - Loads all CATALOGUED archive rows
  - Groups tracks by normalised artist name (ArtistCanon → fuzzy ≥88)
  - Within each artist group, strips version/edition qualifiers from
    titles (Remaster, Live, Remix, Acoustic, etc. — ORPHEUS-compatible,
    see STRIP_WORDS below) before comparing normalised title pairs with
    rapidfuzz fuzz.ratio — flags pairs scoring ≥ TITLE_THRESHOLD (88)
  - Guards against merging two different live recordings of the same
    song into one group (both sides carry a live marker in the raw,
    pre-strip title → skipped, since "live" is stripped before scoring
    and would otherwise make them look identical)
  - Stages flagged pairs in the duplicates table with type='NEAR'
  - Skips pairs already in one EXACT group together
  - dry_run() reports matches without writing to DB
  - Re-run safe: INSERT OR IGNORE on (group_id, file_path)

Design decisions (ported from ORPHEUS's SCRIPTS/lib/orpheus_fuzzy.py):
  - Artist normalisation: lowercase, strip punctuation, collapse whitespace
  - Title normalisation: strip version/edition brackets and bare
    STRIP_WORDS, then same as artist + strip leading "the "
  - group_id: "near_{sha8}" where sha8 is SHA-256[:8] of sorted file paths
  - Confidence: the fuzz.ratio score (0.0–1.0)
  - O(n²) within artist groups — fast because groups are small (< 200 tracks)
  - Threshold is conservative at 88 to avoid false positives; the
    stripping (not the threshold) is what catches "Yesterday" vs
    "Yesterday (Remaster)" and similar version-variant pairs — a bare
    fuzz.ratio on unstripped titles scores well below 88 for these
    (e.g. "Yesterday" vs "Yesterday (Remaster)" = 66.7)

The resulting near-duplicate groups appear in `musaeus dedupe` alongside
exact duplicates.
"""

from __future__ import annotations

import hashlib
import logging
import re
import unicodedata
from pathlib import Path

try:
    from rapidfuzz import fuzz

    _HAVE_RAPIDFUZZ = True
except ImportError:
    _HAVE_RAPIDFUZZ = False

from ..brackets import CLOSE, OPEN
from ..canon import ArtistCanon
from ..context import RunContext, StageResult
from .base import BaseStage, StageError
from .dupe_resolver import _is_live

logger = logging.getLogger(__name__)

# Minimum fuzz.ratio to flag as near-duplicate (0–100 scale)
TITLE_THRESHOLD = 88
ARTIST_THRESHOLD = 88

# Version/edition words stripped from titles before fuzzy comparison.
# ORPHEUS-compatible (SCRIPTS/lib/orpheus_fuzzy.py STRIP_WORDS).
STRIP_WORDS: tuple[str, ...] = (
    "remastered",
    "remaster",
    "remix",
    "live",
    "acoustic",
    "radio edit",
    "single version",
    "album version",
    "mono",
    "stereo",
    "demo",
    "karaoke",
)

# Brackets containing ONLY version words (+ optional year/digits/punctuation)
# are safe to strip. Deliberately NOT stripping all bracketed content —
# arbitrary parentheticals can be part of the real title (e.g. "Here I Am
# (Come and Take Me)" must not collapse to "Here I Am").
_VERSION_BRACKET_WORDS = re.compile(
    rf"[{OPEN}]\s*(?:(?:19|20)\d{{2}}\s+)?(?:"
    + r"|".join(re.escape(w) for w in sorted(STRIP_WORDS, key=len, reverse=True))
    + rf")[\s\d,./+-]*[{CLOSE}]",
    re.IGNORECASE,
)
# Bracket characters come from brackets.py so this file cannot drift
# from the others again -- all three earlier copies knew ( ) and [ ]
# and none knew { }. Only the ALPHABET is shared: WHICH annotations are
# safe to strip stays here, deliberately, per the comment above.
_YEAR_BRACKET_RE = re.compile(rf"[{OPEN}]\s*(?:19|20)\d{{2}}\s*[{CLOSE}]")  # "(2015)"
_NUM_BRACKET_RE = re.compile(rf"[{OPEN}]\s*\d{{1,2}}\s*[{CLOSE}]")  # "[2]"

# Same words also stripped as bare whole-words (catches "Song Title Remix"
# with no surrounding parens at all).
_STRIP_WORDS_RE = re.compile(
    r"\b(?:" + "|".join(re.escape(w) for w in sorted(STRIP_WORDS, key=len, reverse=True)) + r")\b",
    re.IGNORECASE,
)

# Raw (pre-strip) live-recording marker, used to avoid merging two
# different live recordings of the same song into one near-dupe group.
_LIVE_MARKER_RE = re.compile(r"\b(live|in concert)\b", re.IGNORECASE)


def _has_live_marker(raw_title: str) -> bool:
    """True if the raw (unstripped) title looks like a live recording."""
    return bool(_LIVE_MARKER_RE.search(raw_title))


# Two copies this close in length, under the same title, are one performance.
# The baked copies and their originals measured 0.0-0.1 s apart; two nights of
# one song are seconds apart. dupeGuru found 10 live pairs (Gary Moore, The
# Police, Cream ...) this guard kept apart (Grey, 2026-10-05): the titles
# differed only by "-" against "/" (the file name could not hold "/"), and one
# pair was 1.3 s apart. The length tolerance is the keep rule's own, 2 s.
_SAME_TAKE_SECONDS = 2.0


def _same_take(a: dict, b: dict) -> bool:
    """True if two live-marked copies are the same performance, not two.

    The live guard kept every pair of live titles apart. A baked copy and its
    own original are one concert, so 32 baked live copies stayed filed beside
    their originals (2026-09-26). Same title (punctuation and case ignored)
    AND the same length.
    """
    same_title = _normalise(a["title"]) == _normalise(b["title"])
    da, db = a.get("duration"), b.get("duration")
    return same_title and da is not None and db is not None and abs(da - db) <= _SAME_TAKE_SECONDS


# Classical works carry TWO independent identifiers, and conflating them
# was my first mistake here: a work number ("No. 1", "Op. 12", "RV 317",
# "BWV 974") says WHICH PIECE, and a movement marker ("I.", "II.",
# "Movement 3", "Act 2") says WHICH PART OF IT. Two tracks differ if
# either differs.
#
# The first version searched for whichever came first and returned "1" for
# "Violin Concerto No. 1 ... - I. Allegro" -- the work number -- so
# comparing it to "... - II. Adagio" also yielded "1" and the pair stayed
# grouped. Exactly the case the guard existed for.
_WORK_RE = re.compile(r"\b(?:No\.|Op\.|BWV|HWV|RV|K\.|D\.|Hob\.)\s*(\d+[a-z]?)", re.IGNORECASE)
_MOVEMENT_RE = re.compile(
    r"(?:^|[\s\-–—:,(\[])(?:([IVXLC]{1,5})\.(?=\s)|"
    r"(?:Movement|Mvt\.?|Part|Act|Scene)\s*(\d+|[IVXLC]{1,5})\b)",
    re.IGNORECASE,
)


def _classical_id(raw_title: str) -> tuple[str | None, str | None]:
    """(work, movement) identifiers found in a title, either may be None."""
    t = raw_title or ""
    w = _WORK_RE.search(t)
    m = _MOVEMENT_RE.search(t)
    work = w.group(1).lower() if w else None
    mov = next((g for g in (m.groups() if m else ()) if g), None)
    return work, (mov.lower() if mov else None)


def _is_different_piece(title_a: str, title_b: str) -> bool:
    """True when two titles name different works, or different movements.

    Conservative on purpose: a difference is only asserted when BOTH sides
    carry the identifier being compared. One side having a movement marker
    and the other not is the ordinary "Yesterday" vs "Yesterday (Live)"
    case, which the existing stripping already handles.
    """
    wa, ma = _classical_id(title_a)
    wb, mb = _classical_id(title_b)
    if wa and wb and wa != wb:
        return True
    return bool(ma and mb and ma != mb)


def _strip_title_qualifiers(s: str) -> str:
    """Remove version/edition brackets and bare STRIP_WORDS from a title."""
    s = _VERSION_BRACKET_WORDS.sub(" ", s)
    s = _YEAR_BRACKET_RE.sub(" ", s)
    s = _NUM_BRACKET_RE.sub(" ", s)
    s = _STRIP_WORDS_RE.sub(" ", s)
    return s


def _normalise(s: str, strip_qualifiers: bool = False) -> str:
    """
    Lowercase, NFD-normalise, strip punctuation, collapse whitespace.

    strip_qualifiers=True additionally removes version/edition words
    (Remaster, Live, Remix, etc.) — used for titles, not artist names.
    """
    if strip_qualifiers:
        s = _strip_title_qualifiers(s)
    s = unicodedata.normalize("NFD", s.lower())
    s = re.sub(r"[^\w\s]", " ", s)  # punctuation → space
    s = re.sub(r"\s+", " ", s).strip()  # collapse whitespace
    # Strip leading "the "
    if s.startswith("the "):
        s = s[4:]
    return s


# A credit that names a second artist after the first: "Stevie Ray Vaughan &
# Double Trouble", "Janis Joplin, Big Brother ...", "Duke Ellington & His
# Orchestra". dupeGuru found 57 pairs of one recording filed under the plain
# name and under the credit with a collaborator, which the artist buckets
# never compared (Grey, 2026-10-05).
_COLLAB_SPLIT_RE = re.compile(
    r"\s*(?:,|&|/|\band\b|\bwith\b|\bfeat\.?|\bfeaturing\b)\s*", re.IGNORECASE
)

# A credit with a collaborator is only paired with the plain name when the two
# copies are the same length too: "X" and "X & Y" are often two recordings.
_COLLAB_SECONDS = 3.0


def _primary_artist_key(raw_artist: str) -> str:
    """The normalised first artist of a credit ("" if it names only one)."""
    parts = _COLLAB_SPLIT_RE.split(raw_artist.strip(), maxsplit=1)
    if len(parts) < 2 or not parts[0].strip():
        return ""
    return _normalise(parts[0])


def _without_artist_prefix(norm_title: str, norm_artist: str) -> str:
    """The title with the artist's own name taken off the front, for COMPARISON only.

    The wanted list and the library both held titles like "Toto Africa" under the
    artist Toto, "Kinks Lola", "Belinda Carlisle I Feel Free" (Grey, 2026-10-05: 64
    in the library, 14 with the clean-titled song beside them). A fuzzy ratio of
    "toto africa" against "africa" is about 70, far under the threshold, so such a
    pair was never staged. Both arguments are already normalised. Nothing is left
    when the title IS the artist's name, and then the title stays whole. This is
    only ever one MORE way to compare two titles (the best score wins); nothing is
    rewritten, so a title that merely starts with a band's word ("Heart Of Glass")
    can at worst be offered for a person to judge.
    """
    if len(norm_artist) >= 3 and norm_title.startswith(norm_artist + " "):
        rest = norm_title[len(norm_artist) + 1 :].strip()
        if rest:
            return rest
    return norm_title


def _group_id(path_a: str, path_b: str) -> str:
    """Stable group ID from the two sorted paths."""
    combined = "\n".join(sorted([path_a, path_b]))
    return "near_" + hashlib.sha256(combined.encode()).hexdigest()[:8]


class NearDupeStage(BaseStage):
    """
    Near-duplicate detection — find tracks with the same artist and very
    similar titles, staging them for review in the dedupe console.
    """

    NAME = "neardupe"

    # ── Validate ──────────────────────────────────────────────────────────────

    def validate(self, ctx: RunContext) -> None:
        if not _HAVE_RAPIDFUZZ:
            raise StageError(
                "rapidfuzz not installed — required for near-duplicate detection.\n"
                "Install with: pip install rapidfuzz"
            )
        count = ctx.conn.execute(
            "SELECT COUNT(*) FROM archive WHERE status='CATALOGUED'"
        ).fetchone()[0]
        logger.info("[neardupe] %d catalogued tracks to compare", count)

    # ── Shared logic ──────────────────────────────────────────────────────────

    def _detect(self, ctx: RunContext, dry_run: bool) -> StageResult:
        result = self._make_result(dry_run=dry_run)

        cfg = ctx.config
        artist_canon = ArtistCanon(cfg.meta_dir / "artist_canon.tsv")

        # Load all catalogued rows
        rows = ctx.conn.execute(
            """
            SELECT file_path, artist, title, album, bitrate, size_bytes, duration, audio_hash
            FROM archive
            WHERE status = 'CATALOGUED'
              AND artist IS NOT NULL AND trim(artist) != ''
              AND title  IS NOT NULL AND trim(title)  != ''
            ORDER BY artist, title
            """
        ).fetchall()

        result.files_processed = len(rows)

        # Exact groups each path belongs to. A pair already in one EXACT
        # group is the same recording and needs no NEAR row as well. This
        # skipped any file that had EVER been in an EXACT group, whoever
        # the other file was: most originals arrived twice (NUC and USB1),
        # so none was compared with the baked copy it replaces, and 510 of
        # those copies were filed beside their originals (2026-09-26).
        exact_groups: dict[str, set[str]] = {}
        for row in ctx.conn.execute(
            "SELECT group_id, file_path FROM duplicates WHERE duplicate_type='EXACT'"
        ).fetchall():
            exact_groups.setdefault(row["file_path"], set()).add(row["group_id"])

        # Pre-load already-staged near dupe pairs to avoid re-flagging
        existing_near: set[tuple[str, str]] = set()
        near_rows: dict[str, list] = {}
        for row in ctx.conn.execute(
            "SELECT group_id, file_path, status, audio_hash FROM duplicates "
            "WHERE duplicate_type='NEAR'"
        ).fetchall():
            existing_near.add((row["group_id"], row["file_path"]))
            near_rows.setdefault(row["group_id"], []).append(row)

        # A pair closed as 'stale' -- a file changed under it, or it was
        # joined to a group that had -- was never flagged again, because any
        # row counted as "already flagged". 7 baked copies sat stuck beside
        # their originals (2026-09-26). Found again, it is judged again, with
        # the recordings there now.
        stale_near = {
            gid for gid, rs in near_rows.items() if all(r["status"] == "stale" for r in rs)
        }

        # Keep both: every member of a NEAR or ACOUSTIC group marked 'keep'
        # (the resolver keeps ONE; both kept is a person's decision -- Grey
        # decides AcoustID's pairs). Held by the two recordings, not the two
        # paths, so a rename does not undo it.
        decided: dict[str, list] = {}
        for row in ctx.conn.execute(
            "SELECT group_id, status, audio_hash FROM duplicates "
            "WHERE duplicate_type IN ('NEAR', 'ACOUSTIC')"
        ).fetchall():
            decided.setdefault(row["group_id"], []).append(row)
        keep_both = {
            frozenset(r["audio_hash"] for r in rs)
            for rs in decided.values()
            if len(rs) > 1
            and all(r["status"] == "keep" and r["audio_hash"] for r in rs)
            and len({r["audio_hash"] for r in rs}) == 2
        }

        # Bucket tracks by canonical artist
        artist_buckets: dict[str, list[dict]] = {}
        canonical_of_key: dict[str, str] = {}
        for row in rows:
            raw_artist = row["artist"].strip()
            canonical = artist_canon.resolve(raw_artist) or raw_artist
            key = _normalise(canonical)
            canonical_of_key.setdefault(key, canonical)
            bucket = artist_buckets.setdefault(key, [])
            bucket.append(dict(row))

        new_groups = 0
        new_pairs = 0

        # Collab credits to the plain name's bucket: [(plain key, credit tracks)].
        collab_of: dict[str, list[dict]] = {}
        for key, tracks in artist_buckets.items():
            raw = canonical_of_key.get(key, "")
            primary = _primary_artist_key(raw)
            if primary and primary != key and primary in artist_buckets:
                collab_of.setdefault(primary, []).extend(tracks)

        def _candidate_pairs():
            for key, tracks in artist_buckets.items():
                for i in range(len(tracks)):
                    for j in range(i + 1, len(tracks)):
                        yield key, tracks[i], tracks[j], False
                for a_ in tracks:
                    for b_ in collab_of.get(key, ()):
                        yield key, a_, b_, True

        for artist_key, a, b, cross in _candidate_pairs():
            if cross and abs((a["duration"] or 0) - (b["duration"] or 0)) > _COLLAB_SECONDS:
                continue

            # Skip if the two are already an exact pair
            if exact_groups.get(a["file_path"], set()) & exact_groups.get(b["file_path"], set()):
                continue

            # Don't merge two different live recordings of the same
            # song — "live" is stripped before scoring below, so
            # without this guard they'd look identical and collapse
            # into one group. Studio-vs-live still matches fine
            # (only one side carries the marker).
            # The resolver's own rule, title AND album: read from the title
            # alone, "Unplugged" and "24 Nights (Live)" were one song, and one
            # live recording moved to review (review of #86, finding 5).
            if _is_live(dict(a)) and _is_live(dict(b)) and not _same_take(a, b):
                continue

            if frozenset((a["audio_hash"], b["audio_hash"])) in keep_both:
                continue

            # Different movements of one work are different pieces.
            # The shared work title makes them score in the 90s, so
            # the threshold cannot catch this -- only the marker can.
            if _is_different_piece(a["title"], b["title"]):
                continue

            title_a = _normalise(a["title"], strip_qualifiers=True)
            title_b = _normalise(b["title"], strip_qualifiers=True)

            # Also compare with the artist's name taken off the front of either
            # title; the best of the four wins. Never lowers a score.
            bare_a = _without_artist_prefix(title_a, artist_key)
            bare_b = _without_artist_prefix(title_b, artist_key)
            score = max(
                fuzz.ratio(title_a, title_b),
                fuzz.ratio(bare_a, title_b),
                fuzz.ratio(title_a, bare_b),
                fuzz.ratio(bare_a, bare_b),
            )
            if score < TITLE_THRESHOLD:
                continue

            # Near duplicate found
            gid = _group_id(a["file_path"], b["file_path"])
            confidence = round(score / 100.0, 4)

            if gid in stale_near:
                stale_near.discard(gid)
                new_groups += 1
                result.files_changed += 1
                if not dry_run:
                    ctx.conn.execute(
                        """
                        UPDATE duplicates
                           SET status = 'pending', run_id = ?, confidence = ?,
                               audio_hash = (SELECT audio_hash FROM archive
                                              WHERE archive.file_path = duplicates.file_path)
                         WHERE group_id = ?
                        """,
                        (ctx.run_id, confidence, gid),
                    )
                    ctx.log_event(
                        "NEAR_DUPLICATE_FOUND",
                        file_path=a["file_path"],
                        stage=self.NAME,
                        note=f"group={gid} score={score} judged again (was stale)",
                    )
                continue

            is_new_group = False
            for fp in (a["file_path"], b["file_path"]):
                pair_key = (gid, fp)
                if pair_key not in existing_near:
                    is_new_group = True
                    new_pairs += 1
                    existing_near.add(pair_key)

                    if not dry_run:
                        ctx.conn.execute(
                            """
                            INSERT OR IGNORE INTO duplicates
                                (group_id, file_path, duplicate_type,
                                 confidence, run_id, audio_hash)
                            VALUES (?, ?, 'NEAR', ?, ?,
                                    (SELECT audio_hash FROM archive WHERE file_path = ?))
                            """,
                            (gid, fp, confidence, ctx.run_id, fp),
                        )
                        ctx.log_event(
                            "NEAR_DUPLICATE_FOUND",
                            file_path=fp,
                            stage=self.NAME,
                            note=(
                                f"group={gid} score={score} "
                                f"title_a={a['title']!r} "
                                f"title_b={b['title']!r}"
                            ),
                        )
            if is_new_group:
                new_groups += 1
                result.files_changed += 1
                logger.info(
                    "near-dupe: %r ~~ %r (score=%d, artist=%s)",
                    a["title"],
                    b["title"],
                    score,
                    artist_key,
                )

        if not dry_run and new_pairs > 0:
            ctx.conn.commit()

        prefix = "Would stage" if dry_run else "Staged"
        if new_groups == 0:
            result.notes.append("No new near-duplicates found.")
        else:
            result.notes.append(
                f"{prefix} {new_groups} near-duplicate group(s) "
                f"({new_pairs} file entries). "
                f"Review with: musaeus dedupe"
            )

        ctx.record_stage(result)
        return result

    # ── Dry run ───────────────────────────────────────────────────────────────

    def dry_run(self, ctx: RunContext) -> StageResult:
        return self._detect(ctx, dry_run=True)

    # ── Run ───────────────────────────────────────────────────────────────────

    def verify_effect(self, ctx: RunContext, result: StageResult) -> list[str]:
        """A near-duplicate this stage found must actually be staged.

        Same contract as cross-dupe: the event is the report, the
        `duplicates` row is what the resolver acts on. One without the
        other is a finding that reaches a log and nothing else.
        """
        rows = ctx.conn.execute(
            "SELECT file_path FROM events WHERE run_id = ? "
            " AND event_type = 'NEAR_DUPLICATE_FOUND' ORDER BY id DESC LIMIT 10",
            (ctx.run_id,),
        ).fetchall()
        if not rows:
            return []
        unstaged = [
            Path(r["file_path"]).name
            for r in rows
            if not ctx.conn.execute(
                "SELECT 1 FROM duplicates WHERE file_path = ?  AND duplicate_type = 'NEAR' LIMIT 1",
                (r["file_path"],),
            ).fetchone()
        ]
        if not unstaged:
            return []
        return [
            f"{len(unstaged)} of {len(rows)} near-duplicate(s) were reported but "
            f"never staged: {', '.join(unstaged[:3])}"
        ]

    def run(self, ctx: RunContext) -> StageResult:
        return self._detect(ctx, dry_run=False)
