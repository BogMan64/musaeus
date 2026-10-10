"""
MUSAEUS -- Enrichment: complete titles that were cut short at the source.

Grey's USB1 source tags were cut at exactly 36 characters ("Red Hot Chili Peppers
Under the Brid", "Lenny Kravitz Are You Gonna Go My Wa"), and the library kept
the cut titles: 15 were found and completed by hand on 2026-10-06, and Grey asked
that MUSAEUS catch them as songs come in.

The rule is the one that held every time that day: a title that is the START of
the song's MusicBrainz recording title (the recording AcoustID matched), and at
least two characters shorter, was cut -- it takes the recording's title. A title
that merely differs from MusicBrainz (a version word, a different spelling) is
left alone: only an unfinished title is finished.

Each song is asked about once (title_checked_at), at MusicBrainz's 1 request per
second. A song with no recording, or a lookup that could not be made, is not
stamped, so a later run asks again. The tagger writes the new title into the file
and organize renames it on the next Act 3.
"""

from __future__ import annotations

import logging
import re
import time
import unicodedata
from datetime import datetime, timezone

from ..context import RunContext, StageResult
from ..db import ensure_columns
from .album_fill import Unavailable, _get_json
from .base import BaseStage

logger = logging.getLogger(__name__)

_MB_RECORDING = "https://musicbrainz.org/ws/2/recording/{}?fmt=json"
_MB_RATE_S = 1.05
#: Shorter than this, "starts with" says nothing ("Lola" is the start of many titles).
_MIN_TITLE_CHARS = 8
_COMMIT_EVERY = 50


def _norm(s: str) -> str:
    s = unicodedata.normalize("NFKD", (s or "").casefold()).replace("’", "'")
    s = re.sub(r",\s*the\s*$", "", s)  # "Nights In White Sa, The": the article moved to the end
    s = re.sub(r"[^\w\s]", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def completed_title(title: str, mb_title: str) -> str | None:
    """MusicBrainz's title when *title* is a cut-off start of it, else None."""
    cur, full = _norm(title), _norm(mb_title)
    if len(cur) < _MIN_TITLE_CHARS or not full.startswith(cur) or len(full) - len(cur) < 2:
        return None
    return mb_title.replace("’", "'").strip()


def recording_title(mbid: str) -> str | None:
    """The recording's title. Raises Unavailable when MusicBrainz could not be asked."""
    try:
        data = _get_json(_MB_RECORDING.format(mbid))
    except Unavailable:
        raise
    except Exception as exc:  # the network policy's refusal, in a preview
        raise Unavailable(str(exc)) from exc
    title = data.get("title")
    return str(title) if title else None


class TitleCompleteStage(BaseStage):
    """Finish titles the source cut short, from the song's MusicBrainz recording."""

    NAME = "title-complete"

    def validate(self, ctx: RunContext) -> None:
        """Nothing to refuse: a song with no recording is simply not asked about."""

    def _rows(self, ctx: RunContext) -> list:
        from .acousticid import _ensure_columns as _fingerprint_columns

        _fingerprint_columns(ctx.conn)  # acousticid_recording, on a vault that has never had it
        ensure_columns(ctx.conn, (("title_checked_at", "TEXT"),))
        return ctx.conn.execute(
            "SELECT id, file_path, title, acousticid_recording FROM archive "
            "WHERE status = 'CATALOGUED' AND COALESCE(acousticid_recording, '') <> '' "
            "AND COALESCE(title, '') <> '' AND COALESCE(title_checked_at, '') = '' ORDER BY id"
        ).fetchall()

    def dry_run(self, ctx: RunContext) -> StageResult:
        result = self._make_result(dry_run=True)
        rows = self._rows(ctx)
        result.files_processed = len(rows)
        result.notes.append(
            f"{len(rows)} title(s) to check against MusicBrainz (no lookup in a preview)"
        )
        ctx.record_stage(result)
        return result

    def verify_effect(self, ctx: RunContext, result: StageResult) -> list[str]:
        """Every title this run says it completed must be the title in the catalogue."""
        rows = ctx.conn.execute(
            "SELECT e.file_path, e.new_value, a.title FROM events e "
            "LEFT JOIN archive a ON a.file_path = e.file_path "
            "WHERE e.run_id = ? AND e.event_type = 'TITLE_COMPLETED' ORDER BY e.id DESC LIMIT 20",
            (ctx.run_id,),
        ).fetchall()
        wrong = [r["file_path"] for r in rows if r["title"] != r["new_value"]]
        if not wrong:
            return []
        return [f"{len(wrong)} completed title(s) are not in the catalogue: {wrong[0]}"]

    def run(self, ctx: RunContext) -> StageResult:
        result = self._make_result(dry_run=False)
        rows = self._rows(ctx)
        completed = unavailable = 0
        for n, row in enumerate(rows, 1):
            result.files_processed += 1
            try:
                mb_title = recording_title(row["acousticid_recording"])
            except Unavailable as exc:
                unavailable += 1
                logger.debug("[title-complete] could not ask for %s: %s", row["file_path"], exc)
                if unavailable >= 20 and unavailable == n:
                    result.notes.append(
                        "MusicBrainz could not be reached -- stopped; nothing stamped"
                    )
                    break
                continue
            finally:
                time.sleep(_MB_RATE_S)
            now = datetime.now(timezone.utc).isoformat(timespec="seconds")
            new = completed_title(row["title"], mb_title or "")
            if new:
                ctx.conn.execute(
                    "UPDATE archive SET title = ?, title_checked_at = ? WHERE id = ?",
                    (new, now, row["id"]),
                )
                ctx.log_event(
                    "TITLE_COMPLETED",
                    file_path=row["file_path"],
                    old_value=row["title"],
                    new_value=new,
                    stage=self.NAME,
                    note="cut short at the source; the MusicBrainz recording's title",
                )
                completed += 1
                result.files_changed += 1
                logger.info("[title-complete] %r -> %r", row["title"], new)
            else:
                ctx.conn.execute(
                    "UPDATE archive SET title_checked_at = ? WHERE id = ?", (now, row["id"])
                )
            if n % _COMMIT_EVERY == 0:
                ctx.conn.commit()
        ctx.conn.commit()
        result.notes.append(
            f"checked {result.files_processed - unavailable}, completed {completed}"
            + (f", {unavailable} not reachable (asked again next run)" if unavailable else "")
        )
        ctx.record_stage(result)
        return result
