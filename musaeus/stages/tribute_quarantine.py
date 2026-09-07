#!/usr/bin/env python3
"""
MUSAEUS — Tribute Quarantine Stage (standalone, not wired into DEFAULT_PIPELINE)

Detects and quarantines tribute-band/karaoke/meditation/ASMR-type content.
Ported from ORPHEUS's orpheus_junk_quarantine.py, per tonight's ORPHEUS
salvage audit. Standalone, matching the Ghost/Permissions/BPM precedent.

Design differences from the ORPHEUS original:
  - DB-row-driven (archive table, status='CATALOGUED'), not a directory
    walk -- matches every other MUSAEUS stage's convention.
  - Detection reads archive.artist/title/album directly (already
    populated by Scholar), not re-parsed from folder names or re-read
    from file tags on every scan -- MUSAEUS already has this data.
  - Move target / status / manifest+restore-script conventions borrowed
    directly from DupeResolverStage: TRIBUTE_REMOVED_FOR_REVIEW/<date>/
    Artist/Album/ (the exact folder name the 2026-08-12 one-off
    tribute-removal script already used -- reused for consistency, not
    reinvented), archive.status = 'TRIBUTE_REVIEW' (same precedent),
    CSV manifest + auto-generated bash restore script, never deletes.
  - Resumability is automatic via the status transition -- once a row
    leaves 'CATALOGUED' it stops matching future scans, same as
    DupeResolverStage. No extra timestamp column needed.
  - Detection patterns (JUNK_*_PATTERNS / KNOWN_JUNK_ARTISTS /
    PROTECTED_ARTISTS) ported verbatim, including ORPHEUS's own inline
    commentary on why specific protected-artist entries exist -- these
    are hand-tuned against real false positives from actual ORPHEUS test
    runs, not something to second-guess or "clean up" while porting.
"""

from __future__ import annotations

import csv
import logging
import os
import re
import shutil
import stat
from datetime import datetime, timezone
from pathlib import Path

from ..artist_form import comparison_key
from ..context import RunContext, StageResult, elision
from ..wanted_list import wanted_lines
from .base import BaseStage
from .organize import build_track_filename, sanitize_path_component, unique_path

logger = logging.getLogger(__name__)

# ── Junk detection patterns (ported verbatim from orpheus_junk_quarantine.py) ──

JUNK_ARTIST_PATTERNS: list[re.Pattern[str]] = [
    re.compile(r"\bkaraoke\b", re.IGNORECASE),
    re.compile(r"\btribute\b", re.IGNORECASE),
    re.compile(r"\bsleep\b", re.IGNORECASE),
    re.compile(r"\bmeditation\b", re.IGNORECASE),
    re.compile(r"\bhypnos[ie]s\b", re.IGNORECASE),
    re.compile(r"\brelaxation\b", re.IGNORECASE),
    re.compile(r"\bhealing\b", re.IGNORECASE),
    re.compile(r"\bsound bath\b", re.IGNORECASE),
    re.compile(r"\bwhite noise\b", re.IGNORECASE),
    re.compile(r"\bbrown noise\b", re.IGNORECASE),
    re.compile(r"\bpink noise\b", re.IGNORECASE),
    re.compile(r"\basmr\b", re.IGNORECASE),
    re.compile(r"\bre-?record", re.IGNORECASE),
    re.compile(r"\bpiano covers?\b", re.IGNORECASE),
]

JUNK_TITLE_PATTERNS: list[re.Pattern[str]] = [
    re.compile(r"karaoke version", re.IGNORECASE),
    re.compile(r"in the style of", re.IGNORECASE),
    re.compile(r"originally performed", re.IGNORECASE),
    re.compile(r"made famous by", re.IGNORECASE),
    re.compile(r"instrumental version", re.IGNORECASE),
    re.compile(r"backing track", re.IGNORECASE),
    # "tribute" was in the ARTIST and ALBUM lists but not this one, so a
    # knock-off that named itself only in the title walked straight in.
    # Measured 2026-09-06: "Bad To The Bone - George Thorogood and The
    # Destroyers Tribute" (artist "Classic Blues Tones"), "In the End
    # (Piano Tribute to Linkin Park)" (artist "Scott D. Davis") and
    # "Whole Lotta Love - a Tribute to Led Zeppelin" (artist "Led
    # Zepagain") were all CATALOGUED. None of their artist names contains
    # a trigger word, and Grey had to find them by eye.
    re.compile(r"\btribute\b", re.IGNORECASE),
    # "Piano Covers" style products. Deliberately NOT a bare "cover":
    # measured on the same pass, "cover version" alone would have
    # quarantined Bruce Springsteen and Morse/Portnoy/George performing
    # covers of their own choosing. A cover BY A REAL ARTIST is music;
    # a cover-product is not, and "piano cover" is the form the products
    # in this library actually use.
    re.compile(r"\bpiano cover\b", re.IGNORECASE),
]

JUNK_ALBUM_PATTERNS: list[re.Pattern[str]] = [
    re.compile(r"\bkaraoke\b", re.IGNORECASE),
    re.compile(r"\btribute\b", re.IGNORECASE),
    re.compile(r"\bmeditation\b", re.IGNORECASE),
    re.compile(r"\bsleep\b", re.IGNORECASE),
    re.compile(r"\bhypnos", re.IGNORECASE),
    re.compile(r"\brelax", re.IGNORECASE),
]

KNOWN_JUNK_ARTISTS: frozenset[str] = frozenset(
    {
        "karaoke channel",
        "hit tunes karaoke",
        "monster karaoke",
        "michael sealey",
        "lauren ostrowski fenton",
        "guided meditation guru",
        "healing vibrations",
        "michelle's sanctuary",
    }
)

# Protected REAL artists -- never quarantine even if title/album matches a
# pattern. Checked first, always wins. Ported verbatim, including
# ORPHEUS's own reasoning per entry (real false positives caught in
# actual ORPHEUS test runs, not hypothetical).
PROTECTED_ARTISTS: frozenset[str] = frozenset(
    {
        # Artists on tribute COMPILATION albums (they didn't make the tribute)
        "louis armstrong",
        "neil young",
        "garth brooks",
        "merle haggard",
        "spirit",
        "blondie",
        "led zeppelin",
        # Was a bare "jan", which protected every artist with those three
        # letters anywhere in the name. The canonical form is what the
        # artist canon settles on, so protect that instead (Grey,
        # 2026-09-01) -- "Dean" -> "Jan & Dean" is an open canon item.
        "jan & dean",
        "mary wells",
        "refreshments",
        "monks",
        "thomas d'arcy",
        "big blues corp city",
        # Real artists who performed AT a tribute, which the title records.
        # Both were CATALOGUED and correct on 2026-09-06 when `tribute` was
        # added to the title patterns above; this list is what keeps a
        # broader rule from costing real music. Springsteen's is
        # "(Your Love Keeps Lifting Me) Higher and Higher [With Darlene
        # Love, John Fogerty, Sam Moore, Billy Joel And Tom Morello]
        # [A Tribute To Jackie Wilson]" -- a genuine performance by six
        # named artists, not a knock-off of one.
        "bruce springsteen",
        "three tenors, los angeles music center opera chorus, the",
        # Real artists whose SONG TITLES contain trigger words
        "sleep",  # Sleep is a legitimate doom metal band
        "sleep token",  # Sleep Token is a legitimate rock band
        "the healing",  # Could be a real band
        "big sleep",  # Irish band
    }
)


#: PROTECTED_ARTISTS keyed the same way the incoming artist will be.
#:
#: The 2026-09-01 version article-stripped only the ARTIST and compared it
#: against the raw list, so a band called "Healing, The" reduced to
#: "healing", missed the protected entry "the healing", and was
#: quarantined as junk. Both sides must go through the same transform;
#: deriving the set here is what makes forgetting impossible.
_PROTECTED_KEYS = frozenset(comparison_key(p) for p in PROTECTED_ARTISTS)


def is_junk(artist: str, title: str, album: str) -> tuple[bool, str]:
    """Check whether a row's metadata matches any junk pattern. Returns
    (is_junk, reason). Protected artists are checked first and always win,
    even against a matching title/album pattern."""
    artist_lower = (artist or "").lower().strip()
    # Match the WHOLE artist name, not a fragment of it.
    #
    # This was `if protected in artist_lower` -- a substring test -- which
    # inverted the list's purpose in both directions. It protected junk:
    # "neil young" shielded "The Neil Young Tribute Band" (4 tracks in the
    # library on 2026-09-01), and "sleep" shielded "Deep Sleep Music
    # Collective". And it made two junk patterns unreachable, since
    # \bsleep\b and \bhealing\b can never fire for an artist the words
    # "sleep" or "the healing" already protect.
    #
    # The entries mean the ARTIST NAMED "Sleep", not everyone with the
    # word in their name. Article forms are normalised because the library
    # stores "Pretenders, The" and MusicBrainz says "The Pretenders".
    #
    # Measured before changing: catches 5 more knock-offs across the 10,446
    # catalogued rows and loses nothing. Real records with a trigger word
    # in the name -- Asleep at the Wheel, Sleeping at Last, ZZ Top's
    # "Sleeping Bag", the Beatles' "I'm Only Sleeping" -- are untouched,
    # because the junk patterns are word-bounded and none of them match.
    if comparison_key(artist) in _PROTECTED_KEYS:
        return False, ""

    for known in KNOWN_JUNK_ARTISTS:
        if known in artist_lower:
            return True, f"known_junk_artist: {artist_lower}"

    for pat in JUNK_ARTIST_PATTERNS:
        if pat.search(artist or ""):
            return True, f"artist_pattern: {pat.pattern}"

    for pat in JUNK_TITLE_PATTERNS:
        if pat.search(title or ""):
            return True, f"title_pattern: {pat.pattern}"

    for pat in JUNK_ALBUM_PATTERNS:
        if pat.search(album or ""):
            return True, f"album_pattern: {pat.pattern}"

    return False, ""


# ── Tribute Quarantine Stage ─────────────────────────────────────────────────


class TributeQuarantineStage(BaseStage):
    """
    Detect and quarantine tribute-band/karaoke/meditation-type content.
    Wired into Act 1 on 2026-09-01, after VariousArtistsFix and before
    GenreValidate -- see stages/__init__.py for the ordering reasoning.
    Files are moved, never deleted, into config.tribute_review_dir,
    mirroring DupeResolverStage's DUPES_MOVED_FOR_REVIEW convention
    exactly.
    """

    NAME = "tribute-quarantine"

    def validate(self, ctx: RunContext) -> None:
        """No external dependency to check -- pure Python regex matching
        against already-populated archive columns."""

    def _get_candidates(self, ctx: RunContext) -> list[dict]:
        rows = ctx.conn.execute(
            "SELECT id, file_path, artist, title, album FROM archive WHERE status='CATALOGUED'"
        ).fetchall()
        return [dict(r) for r in rows]

    def _batch_date(self, ctx: RunContext) -> str:
        """Same convention as FinalizeStage/DupeResolverStage's own
        _batch_date -- overridable for tests, defaults to today's real
        UTC date, computed once per run."""
        override = ctx.get("finalize_batch_date")
        if override:
            return str(override)
        return datetime.now(tz=timezone.utc).strftime("%Y-%m-%d")

    def _target_path(self, ctx: RunContext, row: dict, source: Path) -> Path:
        artist = row.get("artist") or "Unknown Artist"
        album = row.get("album") or "Unsorted"
        title = row.get("title") or "Unknown Title"

        new_filename = build_track_filename(artist, title, source.suffix)
        artist_safe = sanitize_path_component(artist)
        album_safe = sanitize_path_component(album)

        target_dir = (
            ctx.config.tribute_review_dir / self._batch_date(ctx) / artist_safe / album_safe
        )
        return unique_path(target_dir / new_filename)

    def _write_manifest_and_restore_script(
        self, ctx: RunContext, batch_date: str, moved: list[dict]
    ) -> tuple[Path, Path]:
        review_dir = ctx.config.tribute_review_dir / batch_date
        review_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(tz=timezone.utc).strftime("%Y%m%dT%H%M%SZ")

        manifest_path = review_dir / f"tribute_manifest_{stamp}.csv"
        with open(manifest_path, "w", newline="", encoding="utf-8") as fh:
            # extrasaction="ignore": moved dicts now also carry artist/title
            # for the wanted-list export below, and DictWriter otherwise
            # raises on any key not in fieldnames -- the manifest's own
            # three columns are the contract, unaffected by what else the
            # dict carries.
            writer = csv.DictWriter(
                fh, fieldnames=["source", "destination", "reason"], extrasaction="ignore"
            )
            writer.writeheader()
            writer.writerows(moved)

        restore_path = review_dir / f"restore_{stamp}.sh"
        lines = ["#!/usr/bin/env bash", "set -euo pipefail", ""]
        for m in moved:
            src, dst = m["source"], m["destination"]
            src_dir = os.path.dirname(src)
            lines.append(f'mkdir -p "{src_dir}"')
            lines.append(f'mv -n "{dst}" "{src}"')
        restore_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        restore_path.chmod(restore_path.stat().st_mode | stat.S_IEXEC)

        return manifest_path, restore_path

    def _write_wanted_list(
        self, ctx: RunContext, batch_date: str, moved: list[dict]
    ) -> Path | None:
        """TuneMyMusic-ready "Artist - Title" lines for this batch's
        quarantined tracks, wherever their own title credits the
        original -- see musaeus/wanted_list.py for the extraction and
        already-owned logic. Grey's own instruction, 2026-09-04: this
        used to be a one-off manual export; now every quarantine run
        produces its own.

        Returns None (and writes nothing) when no line in this batch had
        an extractable, not-already-owned credit -- an empty file that
        looks like "nothing found" is worse than no file, since the
        stage's notes already say how many were quarantined.
        """
        lines = wanted_lines(ctx.conn, moved)
        if not lines:
            return None
        review_dir = ctx.config.tribute_review_dir / batch_date
        review_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(tz=timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        wanted_path = review_dir / f"tunemymusic_{stamp}.csv"
        wanted_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return wanted_path

    def verify_effect(self, ctx: RunContext, result: StageResult) -> list[str]:
        """Confirm the quarantine moved files, and left a way back.

        This stage moves real music out of the library on a regex match,
        and as of 2026-09-01 it does so automatically inside Act 1 rather
        than only when a human runs it. Three of the four checks are the
        ones OrganizeStage needs for the same reason -- it is the same
        operation -- and the fourth is specific to what makes this stage
        safe to automate at all.

        The file is not at the new path. The move reported success and did
        not happen, and the row now points at nothing.

        The file is at BOTH paths. A "move" that copied looks perfect
        per-row and silently doubles the library; nothing that counts
        successes can see it.

        The row is still CATALOGUED. Then the DB says a quarantined file
        is still in the library, and the next run quarantines it again.

        The restore script is missing. "Moved, never deleted" is this
        stage's entire safety argument, and the restore script is the only
        thing that makes it true. A quarantine with no way back is a
        deletion with extra steps, and it would be invisible: the files
        moved, the rows updated, the count was right.
        """
        rows = ctx.conn.execute(
            """
            SELECT old_value AS source, new_value AS target
              FROM events
             WHERE stage = ? AND event_type = 'TRIBUTE_QUARANTINED'
               AND run_id = ?
             ORDER BY id DESC LIMIT 12
            """,
            (self.NAME, ctx.run_id),
        ).fetchall()
        if not rows:
            return [
                f"stage reported {result.files_changed} quarantined file(s) but the "
                f"event log has no TRIBUTE_QUARANTINED for this run"
            ]

        problems: list[str] = []
        for r in rows:
            source, target = Path(r["source"]), Path(r["target"])
            if not target.exists():
                problems.append(f"quarantined file is not at the new path: {target.name}")
            elif source.exists():
                problems.append(
                    f"{source.name} is at BOTH paths — the move copied, and the "
                    f"library still holds it"
                )

        still_catalogued = ctx.conn.execute(
            """
            SELECT COUNT(*) FROM archive
             WHERE status = 'CATALOGUED'
               AND file_path IN (
                   SELECT new_value FROM events
                    WHERE stage = ? AND event_type = 'TRIBUTE_QUARANTINED'
                      AND run_id = ?
               )
            """,
            (self.NAME, ctx.run_id),
        ).fetchone()[0]
        if still_catalogued:
            problems.append(
                f"{still_catalogued} quarantined row(s) are still CATALOGUED — the "
                f"library still counts them, and the next run moves them again"
            )

        review_dir = ctx.config.tribute_review_dir / self._batch_date(ctx)
        if not any(review_dir.glob("restore_*.sh")):
            problems.append(
                f"no restore script in {review_dir} — files were moved with no "
                f"way back, which is the one thing this stage promises"
            )

        return problems

    def run(self, ctx: RunContext) -> StageResult:
        result = self._make_result(dry_run=False)
        candidates = self._get_candidates(ctx)

        matches: list[tuple[dict, str]] = []
        for row in candidates:
            matched, reason = is_junk(
                row.get("artist") or "", row.get("title") or "", row.get("album") or ""
            )
            if matched:
                matches.append((row, reason))

        result.notes.append(f"scanned: {len(candidates)}")
        result.notes.append(f"matched: {len(matches)}")
        if not matches:
            result.notes.append("nothing to do — no junk patterns matched")
            ctx.record_stage(result)
            return result

        batch_date = self._batch_date(ctx)
        moved: list[dict] = []
        for row, reason in matches:
            result.files_processed += 1
            source = Path(row["file_path"])
            if not source.exists():
                result.files_errored += 1
                result.errors.append(f"{source}: file missing on disk")
                continue

            target = self._target_path(ctx, row, source)
            try:
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(source), str(target))
            except OSError as exc:
                result.files_errored += 1
                result.errors.append(f"{source.name}: {exc}")
                continue

            ctx.conn.execute(
                "UPDATE archive SET status = 'TRIBUTE_REVIEW', file_path = ? WHERE id = ?",
                (str(target), row["id"]),
            )
            ctx.log_event(
                "TRIBUTE_QUARANTINED",
                file_path=str(target),
                old_value=str(source),
                new_value=str(target),
                stage=self.NAME,
                note=reason,
            )
            moved.append({
                "source": str(source), "destination": str(target), "reason": reason,
                "artist": row.get("artist") or "", "title": row.get("title") or "",
            })
            result.files_changed += 1
            logger.info("[tribute-quarantine] %s -> %s (%s)", source, target, reason)

        ctx.conn.commit()

        if moved:
            manifest_path, restore_path = self._write_manifest_and_restore_script(
                ctx, batch_date, moved
            )
            result.notes.append(f"quarantined {len(moved)} file(s)")
            result.notes.append(f"manifest: {manifest_path}")
            result.notes.append(f"restore script: {restore_path}")

            wanted_path = self._write_wanted_list(ctx, batch_date, moved)
            if wanted_path is not None:
                result.notes.append(f"wanted list: {wanted_path}")

        if result.files_errored:
            result.success = False

        ctx.record_stage(result)
        return result

    def dry_run(self, ctx: RunContext) -> StageResult:
        result = self._make_result(dry_run=True)
        candidates = self._get_candidates(ctx)

        matches = []
        for row in candidates:
            matched, reason = is_junk(
                row.get("artist") or "", row.get("title") or "", row.get("album") or ""
            )
            if matched:
                matches.append((row, reason))

        result.files_processed = len(matches)
        result.notes.append(f"[DRY RUN] scanned {len(candidates)}, would quarantine {len(matches)}")
        result.notes.append("  no files moved, no DB changes")
        for row, reason in matches[:20]:
            result.notes.append(f"  {row.get('artist')} — {row.get('title')}  ({reason})")
        if len(matches) > 20:
            result.notes.append(f"  {elision(len(matches) - 20)}")

        ctx.record_stage(result)
        return result
