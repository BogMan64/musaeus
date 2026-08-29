#!/usr/bin/env python3
"""
MUSAEUS — Tagger Stage

Writes normalised metadata from the archive table back into audio file tags.
Fields written: artist, album, title, genre, year, track number, albumartist.

albumartist is a special case. It has no archive column, so unlike every
other field here it is not driven by the DB -- it is only *repaired*, and
only when the file's existing value is demonstrably the same artist in a
non-canonical spelling (normalizing it lands on the DB artist). A
genuinely different albumartist is left untouched, because it legitimately
differs from the track artist on compilations ("Various Artists") and on
split/guest credits. Before this existed the field was never written at
all, so it kept whatever the source file arrived with: confirmed live
2026-08-21, 2,035 of 5,894 article-artist files (34.5%) had artist and
albumartist disagreeing.

Rules:
  - Only writes fields that differ from what's already in the file.
  - Never touches the audio stream.
  - Logs every change as a TAGGER_WRITE event.
  - Skips files with status != 'CATALOGUED'.
  - Periodic DB commits every _COMMIT_EVERY files.
"""

from __future__ import annotations

import logging
import re
import unicodedata
from pathlib import Path
from typing import Any

from ..context import RunContext, StageResult
from .base import BaseStage, StageError
from .normalize import _move_article_to_suffix

logger = logging.getLogger(__name__)

_COMMIT_EVERY = 50


# ── albumartist: when it must follow artist, and when it must not ─────────────
#
# albumartist has no archive column and was never written by this stage, so it
# kept whatever spelling the source file arrived with -- forever. Measured on
# the live library 2026-08-29: 1,735 of 10,588 files (16.4%) had artist and
# albumartist disagreeing. Sorting mp3tag by Album Artist makes it obvious --
# "Abba" beside "ABBA", "Ad Libs, The" beside "THE AD LIBS".
#
# The old rule repaired only one shape (leading article -> suffix), which left
# most of the 1,735 untouched. A blanket mirror is wrong too, and the
# measurement says exactly where the line falls:
#
#     292  article / punctuation convention   "THE AD LIBS" -> "Ad Libs, The"   MIRROR
#     544  collaboration credit, NO album     "50 Cent" / "50 Cent, Nate Dogg"
#                                             a loose single; the credit is
#                                             noise and the canon already
#                                             collapsed artist to the solo name MIRROR
#     359  collaboration credit, ON an album  "Art Blakey" /
#                                             "Art Blakey & The Jazz Messengers"
#                                             on "Moanin'" -- there it IS the
#                                             album's credited artist            KEEP
#     246  classical composer vs performer    "Antonio Vivaldi" /
#                                             "Anne-Sophie Mutter". Filing
#                                             classical under composer is
#                                             policy; the performer is real
#                                             information and lives nowhere else KEEP
#     181  differ ONLY by letter case         "TLC" / "Tlc" -- and here the
#                                             ARTIST is the damaged field         KEEP
#     110  unrelated                          no rule fits; a human decides       KEEP
#       3  compilation marker                 "Various Artists" / "Soundtrack"    KEEP
#
# Album context is the discriminator, and it is the one Grey ruled on
# (2026-08-29): mirror the album-less singles, keep the album credits.

_ARTICLE_SUFFIX = re.compile(r"^(.*?)[,(]\s*(?:the|a|an)\s*\)?$", re.I)
_ARTICLE_PREFIX = re.compile(r"^(?:the|a|an)\s+(.*)$", re.I)
_PUNCT = re.compile(r"[^\w\s]")
_WS = re.compile(r"\s+")

# Splitting a credit on "," is how "10,000 Maniacs" became artist "10" and how
# "Ray, Goodman & Brown" became "Ray" -- both are in the library today. A comma
# followed by digits is inside one name, not between two.
_CREDIT_SPLIT = re.compile(
    r"\s*(?:,(?!\s*\d)|&|/|;|\bfeat\.?\b|\bfeaturing\b|\bwith\b|\bvs\.?\b|\band\b)\s*",
    re.I,
)

# An albumartist naming an ensemble is a performer credit, not a spelling of
# the composer. Checked in addition to genre, because genre is not always set.
_ENSEMBLE = re.compile(
    r"orchestra|philharmon|symphon|ensemble|quartet|quintet|consort|academy"
    r"|camerata|chamber|baroque|choir|capella|chorale|sinfoni",
    re.I,
)

_COMPILATION_MARKERS = frozenset(
    {"various artists", "various", "va", "soundtrack", "original soundtrack", "ost"}
)


def _fold_name(name: str) -> str:
    """Reduce a name to what survives spelling: case, accents, punctuation, article.

    "Abba" / "ABBA", "AC/DC" / "Ac/dc", "Ronettes, The" / "Ronettes (the)" /
    "The Ronettes" all fold to the same string. Order matters -- the article
    has to be handled while its comma or bracket is still there, which is the
    bug that made an earlier draft of this miss "Black Eyed Peas, The".
    """
    s = unicodedata.normalize("NFKD", name)
    s = "".join(c for c in s if not unicodedata.combining(c))
    s = s.lower().strip()
    for _ in range(2):
        m = _ARTICLE_SUFFIX.match(s)
        if m and m.group(1).strip():
            s = m.group(1).strip()
            continue
        m = _ARTICLE_PREFIX.match(s)
        if m and m.group(1).strip():
            s = m.group(1).strip()
            continue
        break
    s = s.replace("&", " and ").replace("+", " and ")
    s = _PUNCT.sub("", s)
    return _WS.sub(" ", s).strip()


def _first_credit(name: str) -> str:
    """The lead name in a credit list. "50 Cent, Nate Dogg" -> "50 Cent"."""
    parts = [p for p in _CREDIT_SPLIT.split(name) if p and p.strip()]
    return parts[0].strip() if parts else name.strip()


def albumartist_should_follow(
    db_artist: str, file_albumartist: str, *, album: str = "", genre: str = ""
) -> bool:
    """True when albumartist should be rewritten to the canonical artist.

    Returns False for anything it is not sure about. This writes to real
    files, and a wrong mirror destroys information no other field carries.
    """
    artist = (db_artist or "").strip()
    aa = (file_albumartist or "").strip()

    # Nothing to source a value from, or nothing to fix.
    if not artist or not aa or aa == artist:
        return False

    # A compilation's albumartist is genuinely not the track artist.
    if _fold_name(aa) in _COMPILATION_MARKERS:
        return False

    # Classical is filed under composer by policy; albumartist holds the
    # performer, which is the only place that information exists.
    if genre.strip().lower() == "classical" or _ENSEMBLE.search(aa):
        return False

    # Same name, different spelling -- but NOT when the only difference is
    # letter case. Measured 2026-08-29: 181 such files, and in most of them
    # the albumartist is CORRECT and the artist is the damaged one, because
    # `_smart_title()` title-cased acronyms and stylized names into nonsense:
    #
    #     albumartist 'TLC'   artist 'Tlc'      albumartist 'ABBA'  artist 'Abba'
    #     albumartist 'N.W.A' artist 'N.w.a'    albumartist 'SZA'   artist 'Sza'
    #     albumartist 'Paul McCartney'          artist 'Paul Mccartney'
    #     albumartist 'k.d. lang'               artist 'K.d. Lang'
    #
    # Mirroring would copy that damage over the last correct copy of the name.
    # The artist field is what needs repairing, not albumartist -- so refuse,
    # and leave the evidence on disk. Article and punctuation conventions
    # ("THE AD LIBS" -> "Ad Libs, The") still pass: they differ by more than case.
    if _fold_name(aa) == _fold_name(artist):
        # Mirror only when an ARTICLE actually moved, and the moved form is
        # the artist. That is the one difference this project treats as a
        # convention rather than a spelling. Everything else that folds equal
        # -- "A*Teens" vs "ATeens", "1910 Fruitgum Co." vs "1910 Fruitgum Co"
        # -- would lose a character the albumartist still has.
        moved = _move_article_to_suffix(aa)
        return moved != aa and moved.lower() == artist.lower()

    # A collaboration credit. On a real album it IS the album's artist and must
    # survive; on a loose single it is a leftover the canon already resolved.
    if album:
        return False

    # The same casing trap one level down. "24kGoldn, iann dior" leads with
    # the correctly-spelled artist while the artist field holds "24kgoldn";
    # mirroring would write the damaged spelling into the one field that
    # still had it right. If the lead credit and the artist differ ONLY by
    # case, the artist is the damaged one -- refuse, as above.
    lead = _first_credit(aa)
    if lead.lower() == artist.lower() and lead != artist:
        return False

    folded = _fold_name(artist)
    return (
        _fold_name(lead) == folded or _fold_name(_first_credit(artist)) == _fold_name(aa)
    )


# ── Tag read/write helpers ────────────────────────────────────────────────────


def _read_tags(path: Path) -> dict[str, str]:
    """Read existing tags from file. Returns {} on failure."""
    ext = path.suffix.lower()
    try:
        if ext in (".m4a", ".alac", ".mp4"):
            from mutagen.mp4 import MP4  # type: ignore[import-untyped]

            audio: Any = MP4(str(path))
            tags: dict = audio.tags or {}

            def _g(key: str) -> str:
                v = tags.get(key, [])
                return str(v[0]) if v else ""

            # trkn is stored as [(track_num, total)] — extract the number directly.
            _trkn = tags.get("trkn", [])
            track_str = str(_trkn[0][0]) if _trkn and _trkn[0] else ""
            return {
                "artist": _g("\xa9ART"),
                "albumartist": _g("aART"),
                "album": _g("\xa9alb"),
                "title": _g("\xa9nam"),
                "genre": _g("\xa9gen"),
                "year": _g("\xa9day"),
                "track": track_str,
            }

        if ext == ".flac":
            from mutagen.flac import FLAC  # type: ignore[import-untyped]

            audio = FLAC(str(path))

            def _gf(key: str) -> str:
                v = audio.get(key, [])
                return v[0] if v else ""

            return {
                "artist": _gf("artist"),
                "albumartist": _gf("albumartist"),
                "album": _gf("album"),
                "title": _gf("title"),
                "genre": _gf("genre"),
                "year": _gf("date"),
                "track": _gf("tracknumber"),
            }

        if ext == ".mp3":
            from mutagen.easyid3 import EasyID3  # type: ignore[import-untyped]

            try:
                audio = EasyID3(str(path))
            except Exception:
                return {}

            def _gm(key: str) -> str:
                v = audio.get(key, [])
                return v[0] if v else ""

            return {
                "artist": _gm("artist"),
                "albumartist": _gm("albumartist"),
                "album": _gm("album"),
                "title": _gm("title"),
                "genre": _gm("genre"),
                "year": _gm("date"),
                "track": _gm("tracknumber"),
            }

    except Exception as exc:
        logger.debug("read_tags failed %s: %s", path, exc)
    return {}


def _write_tags(path: Path, changes: dict[str, str]) -> bool:
    """Write *changes* dict to file tags. Returns True on success."""
    if not changes:
        return True
    ext = path.suffix.lower()
    try:
        if ext in (".m4a", ".alac", ".mp4"):
            from mutagen.mp4 import MP4  # type: ignore[import-untyped]

            audio: Any = MP4(str(path))
            if audio.tags is None:
                audio.add_tags()
            _map = {
                "artist": "\xa9ART",
                "albumartist": "aART",
                "album": "\xa9alb",
                "title": "\xa9nam",
                "genre": "\xa9gen",
                "year": "\xa9day",
            }
            for field, val in changes.items():
                key = _map.get(field)
                if key:
                    audio[key] = [val]
            audio.save()
            return True

        if ext == ".flac":
            from mutagen.flac import FLAC  # type: ignore[import-untyped]

            audio = FLAC(str(path))
            _map_f = {
                "artist": "artist",
                "albumartist": "albumartist",
                "album": "album",
                "title": "title",
                "genre": "genre",
                "year": "date",
                "track": "tracknumber",
            }
            for field, val in changes.items():
                key = _map_f.get(field)
                if key:
                    audio[key] = [val]
            audio.save()
            return True

        if ext == ".mp3":
            from mutagen.easyid3 import EasyID3  # type: ignore[import-untyped]

            try:
                audio = EasyID3(str(path))
            except Exception:
                from mutagen.id3 import ID3  # type: ignore[import-untyped]

                audio = ID3()
            _map_m = {
                "artist": "artist",
                "albumartist": "albumartist",
                "album": "album",
                "title": "title",
                "genre": "genre",
                "year": "date",
                "track": "tracknumber",
            }
            for field, val in changes.items():
                key = _map_m.get(field)
                if key:
                    audio[key] = [val]
            audio.save(str(path))
            return True

    except Exception as exc:
        logger.warning("write_tags failed %s: %s", path, exc)
        return False

    # Unsupported format: log but don't error
    logger.debug("tagger: no write support for ext %s: %s", ext, path)
    return True


# ── Tagger Stage ──────────────────────────────────────────────────────────────


class TaggerStage(BaseStage):
    """
    Write normalised metadata from the DB archive back to file tags.

    Only changes fields that genuinely differ — no-op writes are skipped.
    The stage never modifies the audio stream.
    """

    @classmethod
    def plan_candidates(cls, conn, cfg) -> tuple[int, str]:
        """Rows this stage would act on. Read-only; see planner.py."""
        n = conn.execute("SELECT COUNT(*) FROM archive WHERE status='CATALOGUED'").fetchone()[0]
        return int(n), "catalogued files whose tags would be checked"

    NAME = "tagger"

    def validate(self, ctx: RunContext) -> None:
        try:
            import mutagen  # noqa: F401
        except ImportError:
            raise StageError("mutagen not installed — run: pip install mutagen") from None

    # ── helpers ───────────────────────────────────────────────────────────────

    def _get_pending(self, ctx: RunContext) -> list[dict]:
        rows = ctx.conn.execute(
            """
            SELECT file_path, artist, album, title, genre, year, track
              FROM archive
             WHERE status = 'CATALOGUED'
             ORDER BY artist, album, track
            """
        ).fetchall()
        return [dict(r) for r in rows]

    def _compute_changes(self, db_row: dict, file_tags: dict[str, str]) -> dict[str, str]:
        """Return only the fields that need updating."""
        changes: dict[str, str] = {}
        field_map = {
            "artist": "artist",
            "album": "album",
            "title": "title",
            "genre": "genre",
            "year": "year",
            "track": "track",
        }
        for db_field, tag_field in field_map.items():
            db_val = str(db_row.get(db_field) or "").strip()
            file_val = str(file_tags.get(tag_field) or "").strip()
            if db_val and db_val != file_val:
                changes[db_field] = db_val

        db_artist = str(db_row.get("artist") or "").strip()
        file_aa = str(file_tags.get("albumartist") or "").strip()
        album = str(db_row.get("album") or file_tags.get("album") or "").strip()
        genre = str(db_row.get("genre") or file_tags.get("genre") or "").strip()
        if albumartist_should_follow(db_artist, file_aa, album=album, genre=genre):
            changes["albumartist"] = db_artist

        return changes

    # ── run ───────────────────────────────────────────────────────────────────

    def run(self, ctx: RunContext) -> StageResult:
        result = self._make_result(dry_run=False)
        pending = self._get_pending(ctx)

        total = len(pending)
        result.notes.append(f"catalogued files: {total}")

        changed_count = 0
        skip_count = 0
        error_count = 0

        for i, row in enumerate(pending, 1):
            fp = row["file_path"]
            path = Path(fp)

            if not path.exists():
                result.files_skipped += 1
                skip_count += 1
                continue

            file_tags = _read_tags(path)
            changes = self._compute_changes(row, file_tags)

            result.files_processed += 1

            if not changes:
                result.files_skipped += 1
                skip_count += 1
                continue

            ok = _write_tags(path, changes)
            if ok:
                result.files_changed += 1
                changed_count += 1
                ctx.log_event(
                    "TAGGER_WRITE",
                    file_path=fp,
                    new_value=str(changes),
                    stage="tagger",
                )
            else:
                result.files_errored += 1
                error_count += 1

            if i % _COMMIT_EVERY == 0:
                ctx.conn.commit()
                logger.info("tagger: checkpoint %d/%d", i, total)

        ctx.conn.commit()

        result.notes.append(f"  tags written  : {changed_count}")
        result.notes.append(f"  already clean : {skip_count}")
        if error_count:
            result.notes.append(f"  errors        : {error_count}")
            result.success = False

        ctx.record_stage(result)
        return result

    # ── dry_run ───────────────────────────────────────────────────────────────

    def dry_run(self, ctx: RunContext) -> StageResult:
        result = self._make_result(dry_run=True)
        pending = self._get_pending(ctx)

        total = len(pending)
        would_change = 0
        would_skip = 0

        for row in pending:
            path = Path(row["file_path"])
            if not path.exists():
                would_skip += 1
                continue
            file_tags = _read_tags(path)
            changes = self._compute_changes(row, file_tags)
            if changes:
                would_change += 1
            else:
                would_skip += 1

        result.files_processed = total
        result.files_changed = would_change
        result.files_skipped = would_skip
        result.notes.append(f"[DRY RUN] would update {would_change} file(s)")
        result.notes.append(f"  already clean : {would_skip}")

        ctx.record_stage(result)
        return result
