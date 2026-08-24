#!/usr/bin/env python3
"""
MUSAEUS — Classical Composer Stage

Files classical recordings under the composer rather than the performer.

Grey's ruling, 2026-08-24: for classical, the composer is the identity
that matters. A Bach cantata belongs with the other Bach, not scattered
across whichever ensemble happened to record it.

Runs after GenreValidate, because "is this Classical?" has to be settled
before this stage can ask "then who wrote it?".

Where the composer comes from, strongest signal first
-----------------------------------------------------
1. **A thematic catalogue number in the title.** RV 532 is Vivaldi and
   BWV 1043 is Bach whoever is playing; that is the entire purpose of
   those systems. This alone resolved 85 of 104 on the live library.
2. **An exact match against `Composer_Canon.tsv`** on a whole
   comma-separated part of the artist credit. The composer is often
   already in there ("Dubravka Tomsic, Johann Sebastian Bach"), and the
   position varies -- "Domenico Scarlatti, Dubravka Tomsic" has it first
   -- so it can be neither "take the first" nor "take the last".
3. **The same exact match against a title prefix** ("Handel - Water
   Music Suite No. 1").

Not from a composer tag: **no file in this library carries one.**

Why matching is exact, never substring
--------------------------------------
All four of these are real credits in this library:

    "Franz Liszt Chamber Orchestra"  — an ensemble named after a composer
    "Josef Suk Chamber Orchestra"    — likewise
    "The Four Seasons"               — a work, and also a band
    "Fiddler on the Roof"            — a musical

Parsing the title prefix loosely was tried first and rejected: 42%
coverage, and it confidently produced "The Four Seasons" (9 tracks) and
"Fiddler on the Roof" (2) as composers.

Anything unresolved is left exactly as it is. A wrong composer is worse
than no composer: it is indistinguishable from a right one once written,
and it moves the file.
"""

from __future__ import annotations

import logging
import re
import shutil
from pathlib import Path

from ..context import RunContext, StageResult
from .base import BaseStage
from .organize import build_track_filename, sanitize_path_component, unique_path

logger = logging.getLogger(__name__)

CANON_FILENAME = "Composer_Canon.tsv"

_TITLE_PREFIX = re.compile(r"^([^-–]{2,30}?)\s+[-–]\s+\S")

#: K. is deliberately absent. Köchel numbers Mozart, but Kirkpatrick numbers
#: Scarlatti, and "Keyboard Sonata in D Minor, K. 1, L. 366" is Scarlatti --
#: it resolves safely through L. (Longo), which is Scarlatti-only. An
#: ambiguous marker earns nothing here.
_CATALOGUE: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"\bBWV\s*\d", re.I), "Johann Sebastian Bach"),
    (re.compile(r"\bRV\s*\d", re.I), "Antonio Vivaldi"),
    (re.compile(r"\bHWV\s*\d", re.I), "George Frideric Handel"),
    (re.compile(r"\bTWV\s*\d", re.I), "Georg Philipp Telemann"),
    (re.compile(r"\bWWV\s*\d", re.I), "Richard Wagner"),
    (re.compile(r"\bL\.\s*\d", re.I), "Domenico Scarlatti"),
    (re.compile(r"\bZ\.?\s*\d{3}\b"), "Henry Purcell"),
    (re.compile(r"\bD\.\s*\d{3}\b"), "Franz Schubert"),
]


def load_composer_canon(path: Path) -> dict[str, str]:
    """variant (lowercased) → canonical composer. Empty if the file is absent."""
    if not path.exists():
        return {}
    canon: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "\t" not in line:
            continue
        variant, canonical = line.split("\t", 1)
        canon[variant.strip().lower()] = canonical.strip()
    return canon


def composer_for(artist: str, title: str, canon: dict[str, str]) -> tuple[str | None, str]:
    """Return (canonical composer, how it was found). (None, "") if unresolved."""
    for pattern, composer in _CATALOGUE:
        if pattern.search(title or ""):
            return composer, "catalogue number"
    for part in (artist or "").split(","):
        hit = canon.get(part.strip().lower())
        if hit:
            return hit, "artist credit"
    m = _TITLE_PREFIX.match((title or "").strip())
    if m:
        hit = canon.get(m.group(1).strip().lower())
        if hit:
            return hit, "title prefix"
    return None, ""


class ClassicalComposerStage(BaseStage):
    """Refile CATALOGUED Classical tracks under their composer."""

    NAME = "classical-composer"

    def validate(self, ctx: RunContext) -> None:
        canon = load_composer_canon(ctx.config.meta_dir / CANON_FILENAME)
        n = ctx.conn.execute(
            "SELECT COUNT(*) FROM archive WHERE status='CATALOGUED' AND genre='Classical'"
        ).fetchone()[0]
        logger.info("[classical-composer] %d classical track(s), %d canon entries", n, len(canon))

    def _plan(self, ctx: RunContext) -> tuple[list[tuple[dict, str, str]], int]:
        canon = load_composer_canon(ctx.config.meta_dir / CANON_FILENAME)
        rows = ctx.conn.execute(
            "SELECT id, artist, title, file_path FROM archive "
            "WHERE status='CATALOGUED' AND genre='Classical' ORDER BY artist, title"
        ).fetchall()
        plan, unresolved = [], 0
        for r in rows:
            row = dict(r)
            comp, how = composer_for(row["artist"] or "", row["title"] or "", canon)
            if not comp:
                unresolved += 1
            elif comp != (row["artist"] or "").strip():
                plan.append((row, comp, how))
        return plan, unresolved

    def _process(self, ctx: RunContext, dry_run: bool) -> StageResult:
        result = self._make_result(dry_run=dry_run)
        plan, unresolved = self._plan(ctx)
        result.notes.append(f"classical tracks to refile: {len(plan)}")
        result.notes.append(f"composer unresolved, left as-is: {unresolved}")

        for row, composer, how in plan:
            result.files_processed += 1
            src = Path(row["file_path"])
            if not src.exists():
                result.files_errored += 1
                result.errors.append(f"{src.name}: file missing on disk")
                continue
            if dry_run:
                result.notes.append(f"  [DRY] {row['artist']} → {composer} ({how})")
                result.files_changed += 1
                continue

            album_dir, artist_dir = src.parent, src.parent.parent
            dst_dir = artist_dir.with_name(sanitize_path_component(composer)) / album_dir.name
            dst = unique_path(
                dst_dir / build_track_filename(composer, row["title"] or src.stem, src.suffix)
            )
            dst_dir.mkdir(parents=True, exist_ok=True)

            # Row first, then the move, then commit. A move cannot be rolled
            # back and a DB write can, so this ordering means a failure leaves
            # neither half applied. Committed per row because rollback()
            # discards everything uncommitted.
            ctx.conn.execute(
                "UPDATE archive SET artist=?, file_path=? WHERE id=?",
                (composer, str(dst), row["id"]),
            )
            try:
                shutil.move(str(src), str(dst))
            except OSError as exc:
                ctx.conn.rollback()
                result.files_errored += 1
                result.errors.append(f"{src.name}: {exc}")
                continue

            ctx.log_event(
                "ARTIST_SET_TO_COMPOSER",
                file_path=str(dst),
                old_value=row["artist"],
                new_value=composer,
                stage=self.NAME,
                note=f"resolved by {how}",
            )
            ctx.conn.commit()
            result.files_changed += 1
            for d in (album_dir, artist_dir):
                try:
                    d.rmdir()
                except OSError:
                    pass

        ctx.record_stage(result)
        return result

    def verify_effect(self, ctx: RunContext, result: StageResult) -> list[str]:
        """Nothing resolvable may still be filed under a performer.

        Asserted by re-resolving the library rather than by re-reading what
        this stage wrote, so a stage that moved nothing cannot satisfy it.
        """
        problems: list[str] = []
        plan, _ = self._plan(ctx)
        if plan:
            names = ", ".join(f"{r['artist']} → {c}" for r, c, _ in plan[:3])
            problems.append(
                f"{len(plan)} classical track(s) still filed under a performer "
                f"whose composer is resolvable: {names}"
            )
        return problems

    def dry_run(self, ctx: RunContext) -> StageResult:
        return self._process(ctx, dry_run=True)

    def run(self, ctx: RunContext) -> StageResult:
        return self._process(ctx, dry_run=False)
