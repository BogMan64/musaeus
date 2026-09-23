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
from pathlib import Path
from typing import Any

from ..context import RunContext, StageResult
from .base import BaseStage, StageError
from .normalize import _move_article_to_suffix

logger = logging.getLogger(__name__)

_COMMIT_EVERY = 50


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

        # albumartist has no archive column and was never written by this
        # stage, so it kept whatever spelling the source file arrived with --
        # forever. Confirmed live 2026-08-21: 2,035 of 5,894 article-artist
        # files (34.5%) had artist and albumartist disagreeing, e.g. artist
        # "Cranberries, The" beside albumartist "The Cranberries".
        #
        # Deliberately NOT a blanket mirror of artist. albumartist is
        # legitimately different from track artist on compilations ("Various
        # Artists") and split/guest credits, and clobbering those would
        # destroy real information. Only rewritten when the file's existing
        # albumartist is the SAME artist in a non-canonical spelling -- i.e.
        # normalizing it lands on the DB artist.
        db_artist = str(db_row.get("artist") or "").strip()
        file_aa = str(file_tags.get("albumartist") or "").strip()
        if (
            db_artist
            and file_aa
            and file_aa != db_artist
            and _move_article_to_suffix(file_aa) == db_artist
        ):
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
