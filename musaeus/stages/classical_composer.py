#!/usr/bin/env python3
"""
MUSAEUS — Classical Composer Stage

Files classical recordings under the composer rather than the performer.

Grey's ruling, 2026-08-24: for classical, the composer is the identity
that matters. A Bach cantata belongs with the other Bach, not scattered
across whichever ensemble happened to record it.

Grey's ruling, 2026-10-17, extending it: "for classical albums only
composer instead of artists". A composer-filed album is matched by its
composer ONLY when an outside source is asked about it -- album art first
(art_sources.Asked): a source's answer counts only if one of its credited
names is the composer; a performer-only credit never does. Recognised the
way this stage filed it: the row's artist is a Composer_Canon.tsv composer.

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

import contextlib
import logging
import re
import shutil
import unicodedata
from pathlib import Path

from ..context import RunContext, StageResult
from ..filing import load as filing_load
from .base import BaseStage
from .normalize import stored_artist
from .organize import library_relpath, unique_path

logger = logging.getLogger(__name__)

CANON_FILENAME = "Composer_Canon.tsv"

# "Handel - Water Music" or "Wolfgang Amadeus Mozart: Symphony No. 41" -- the
# colon form was missed and the symphony filed under its orchestra (batch 4,
# 2026-10-25). Safe: the prefix must still be a Composer_Canon name exactly.
_TITLE_PREFIX = re.compile(r"^([^-–:]{2,60}?)(?:\s+[-–]|\s*:)\s+\S")

#: K. is deliberately absent. Köchel numbers Mozart, but Kirkpatrick numbers
#: Scarlatti, and "Keyboard Sonata in D Minor, K. 1, L. 366" is Scarlatti --
#: it resolves safely through L. (Longo), which is Scarlatti-only. An
#: ambiguous marker earns nothing here.
_CATALOGUE: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"\bBWV\.?\s*\d", re.I), "Johann Sebastian Bach"),
    (re.compile(r"\bRV\.?\s*\d", re.I), "Antonio Vivaldi"),
    (re.compile(r"\bHWV\.?\s*\d", re.I), "George Frideric Handel"),
    (re.compile(r"\bTWV\.?\s*\d", re.I), "Georg Philipp Telemann"),
    (re.compile(r"\bWWV\.?\s*\d", re.I), "Richard Wagner"),
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
    # A composer named at the head of the title before the credit: the canon
    # also maps performers to a composer ("András Schiff" -> Bach), and Schiff's
    # "Mozart: Piano Sonata No. 11" went to Bach (Codex review of #244, round 2).
    m = _TITLE_PREFIX.match((title or "").strip())
    if m:
        # The prefix must name the composer, not a performer the canon maps:
        # "András Schiff: Mozart Piano Sonata" went to Bach (#244, round 1).
        def named(name: str) -> str | None:
            hit = canon.get(name.strip().lower())
            return hit if hit and _fold(hit).split()[-1] in _fold(name).split() else None

        # Whole, then each name of a list: "Albinoni, Giazotto - Adagio" is the
        # composer and an arranger (batch 5, 2026-10-25). Two composers in the
        # list ("Bach, Handel - ...") leave it to the credit (Codex review of #245).
        whole = named(m.group(1))
        listed = {c for c in map(named, m.group(1).split(",")) if c}
        if whole or len(listed) == 1:
            return whole or listed.pop(), "title prefix"
    for part in (artist or "").split(","):
        hit = canon.get(part.strip().lower())
        if hit:
            return hit, "artist credit"
    return None, ""


def _fold(name: str) -> str:
    """Lower case, accents and punctuation gone: "J.S.Bach" -> "j s bach"."""
    plain = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]+", " ", plain.lower()).strip()


def _is_within(path: Path, root: Path) -> bool:
    """True if `path` is `root` or sits underneath it.

    Compared on resolved paths so a symlink or a `..` segment cannot make
    an outside path look inside one.
    """
    try:
        path.resolve().relative_to(root.resolve())
    except ValueError:
        return False
    return True


def _PROTECTED_DIRS(ctx: RunContext) -> set[Path]:
    """Directories this stage must never delete, however empty they get."""
    c = ctx.config
    return {
        d.resolve()
        for d in (
            c.vault_root,
            c.inbox,
            c.staging,
            c.quarantine,
            c.runs_root,
            c.meta_dir,
            c.alac_library,
            c.alac_archive,
        )
    }


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
            "SELECT id, artist, title, album, genre, file_path FROM archive "
            "WHERE status='CATALOGUED' AND genre='Classical' ORDER BY artist, title"
        ).fetchall()
        plan, unresolved = [], 0
        for r in rows:
            row = dict(r)
            comp, how = composer_for(row["artist"] or "", row["title"] or "", canon)
            if not comp:
                unresolved += 1
                continue
            # In the every-word form Normalize writes (Grey, 2026-09-25). The
            # canon says "Ludwig van Beethoven"; comparing that with the
            # stored "Ludwig Van Beethoven" refiled every classical track on
            # every run, and Normalize changed each one straight back.
            comp = stored_artist(comp)
            if comp != (row["artist"] or "").strip():
                plan.append((row, comp, how))
        return plan, unresolved

    def _process(self, ctx: RunContext, dry_run: bool) -> StageResult:
        result = self._make_result(dry_run=dry_run)
        plan, unresolved = self._plan(ctx)
        result.notes.append(f"classical tracks to refile: {len(plan)}")
        result.notes.append(f"composer unresolved, left as-is: {unresolved}")

        filing = filing_load(ctx.config.meta_dir)
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

            # Only a file already filed in the library has an artist
            # directory to rename. Before finalize, a file has no such
            # layout -- the campaign stages them flat, straight into
            # INBOX -- and deriving the destination from where the source
            # happens to sit is then catastrophic:
            #
            #     src        = <vault>/INBOX/track.m4a
            #     src.parent = <vault>/INBOX
            #     src.parent.parent = <vault>          <-- the vault ITSELF
            #     .with_name(composer) -> <vault>/../<Composer>
            #
            # which moved the track to a SIBLING of the vault
            # (Projects/Johann Sebastian Bach/INBOX/...), outside every
            # root the run knows about. Finalize's recovery boundary then
            # correctly refused to move a file its checkpoint could not
            # restore, and the row failed its batch. Measured on the live
            # vault 2026-08-25: 3 tracks escaped this way (Bach, Handel,
            # Vivaldi), growing by ~2 per 500-file batch.
            #
            # There is nothing to move in that case anyway. Finalize
            # builds its own target as
            # `alac_library / <batch date> / <artist> / <album>` from the
            # archive row, so updating `artist` is the whole job: the file
            # gets filed under the composer by the stage whose task that
            # is. Move only what is already filed among the masters.
            #
            # It asked about alac_library, where finalize filed before
            # 2026-09-25; that is the Lossless edition now, so no master was
            # ever moved here (review of slice A). Organize refiled them later.
            in_library = _is_within(src, ctx.config.alac_archive)
            album_dir: Path | None = None
            artist_dir: Path | None = None
            if in_library:
                # Where Organize would file it (organize.library_relpath, the
                # one path rule), not a sibling of src's grandparent: a master
                # not at Genre/Artist/Album/file depth was carried out of
                # ALAC-Archival that way (pre-merge review of #161).
                archive = ctx.config.alac_archive
                rel = library_relpath(
                    composer, None, row["genre"], row["album"],
                    row["title"] or src.stem, src.suffix, filing,
                )  # fmt: skip
                dst = unique_path(archive / rel)
                dst.parent.mkdir(parents=True, exist_ok=True)
                # Emptied folders go, but only below a genre folder: never
                # ALAC-Archival itself or a genre folder.
                album_dir, artist_dir = src.parent, src.parent.parent
                if len(album_dir.relative_to(archive).parts) < 3:
                    album_dir = None
                if len(artist_dir.relative_to(archive).parts) < 2:
                    artist_dir = None
            else:
                dst = src

            # Row first, then the move, then commit. A move cannot be rolled
            # back and a DB write can, so this ordering means a failure leaves
            # neither half applied. Committed per row because rollback()
            # discards everything uncommitted.
            ctx.conn.execute(
                "UPDATE archive SET artist=?, file_path=? WHERE id=?",
                (composer, str(dst), row["id"]),
            )
            if dst != src:
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
            if dst != src:
                # The move itself, path to path: the event above carries the
                # artist names, so doctor could not follow the file and read
                # a master MUSAEUS had tagged as changed outside (Grey's code
                # review, 2026-10-11, finding 5).
                ctx.log_event(
                    "COMPOSER_REFILED",
                    file_path=str(dst),
                    old_value=str(src),
                    stage=self.NAME,
                    note=f"filed under the composer {composer}",
                )
            ctx.conn.commit()
            result.files_changed += 1
            # Guarded because artist_dir used to resolve to the vault
            # root itself (see above); this only ever failed to delete it
            # because rmdir refuses a non-empty directory. Never prune a
            # directory the configuration names.
            for d in (album_dir, artist_dir):
                if d is None or d in _PROTECTED_DIRS(ctx):
                    continue
                with contextlib.suppress(OSError):
                    d.rmdir()

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
