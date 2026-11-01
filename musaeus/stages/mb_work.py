"""
MUSAEUS -- Enrichment: one song held twice under two titles, found by its MusicBrainz work.

Grey, 2026-10-31: "MB does have a 'work' ID for the song itself = this along with
our existing rules should resolve the issue ... B as its own step"; 2026-11-01: "B
list items move copies to review". MusicBrainz links each recording to the work (the
song) it performs. Two library copies by one act (MusicBrainz's first credited
artist) of the same works are one song, whatever their titles or our credit text say:
"Synchronicity I (Live In Atlanta, 1983 ...)" and "Synchronicity I", "Tone-Lōc" and
"Tone-Loc". Each pair is staged as a WORK group; the next run's resolver keeps one by
the keep rule and moves the other to REVIEW.

Left alone (Grey, 2026-11-01, "leave them"):
  - a pair whose losing copy a person kept by hand;
  - a copy under half the other's length: a 48 s instrumental is not the song;
  - a pair already in one duplicate group: kept together, or listed for a person.
The SAME works, not one shared: "Everybody Has A Dream" holds a reprise of "The
Stranger", and "Tune-Up - When Lights Are Low" two tunes.

MusicBrainz's live mark ("live, 1974-01-30: Madison Square Garden", or a release
typed Live) is stored as mb_live, which the keep rule reads -- only when the copy's
own album is one of those live releases: "Before The Flood" names no "live", and
AcoustID matches some studio files to a live recording of the song. Each song is asked about once (mb_work_checked_at), at MusicBrainz's 1
request a second. shortcut: a recording re-fingerprinted later keeps its old answer;
clear mb_work_checked_at to ask again.
"""

from __future__ import annotations

import logging
import re
import time
import uuid
from collections import defaultdict
from datetime import datetime, timezone

from ..brackets import strip_bracketed
from ..context import RunContext, StageResult
from .album_fill import Unavailable, _get_json
from .base import BaseStage
from .dupe_resolver import _rank

logger = logging.getLogger(__name__)

_MB_RECORDING = (
    "https://musicbrainz.org/ws/2/recording/{}?fmt=json"
    "&inc=artist-credits+work-rels+releases+release-groups"
)
_MB_RATE_S = 1.05
#: A recording lookup lists at most 25 releases; past that every release is browsed,
#: so a live album further down still counts (CodeRabbit, review of #276).
_MB_LISTED = 25
_MB_RELEASES = "https://musicbrainz.org/ws/2/release?recording={}&inc=release-groups&fmt=json&limit=100&offset={}"
_COMMIT_EVERY = 50
_LIVE_RE = re.compile(r"\blive\b", re.IGNORECASE)


def recording_works(mbid: str) -> tuple[list[str], str, set[str]]:
    """(work ids, first credited artist, the albums it is live on) of a recording.

    Raises Unavailable when MusicBrainz could not be asked.
    """
    try:
        data = _get_json(_MB_RECORDING.format(mbid))
    except Unavailable:
        raise
    except Exception as exc:  # the network policy's refusal, in a preview
        raise Unavailable(str(exc)) from exc
    works = sorted(
        {r["work"]["id"] for r in data.get("relations") or [] if (r.get("work") or {}).get("id")}
    )
    credit = data.get("artist-credit") or []
    first = str((credit[0].get("name") if credit else "") or "")
    if len(data.get("releases") or []) >= _MB_LISTED:
        data = {**data, "releases": _all_releases(mbid)}
    return works, first, live_albums(data)


def _all_releases(mbid: str) -> list[dict]:
    """Every release of a recording, a page of 100 at a time at MusicBrainz's rate."""
    releases: list[dict] = []
    while True:
        time.sleep(_MB_RATE_S)
        try:
            page = _get_json(_MB_RELEASES.format(mbid, len(releases)))
        except Unavailable:
            raise
        except Exception as exc:  # the network policy's refusal, in a preview
            raise Unavailable(str(exc)) from exc
        got = page.get("releases") or []
        releases += got
        if not got or len(releases) >= int(page.get("release-count") or 0):
            return releases


def album_key(title: str | None) -> str:
    """An album title to compare by: "One More From the Road (Deluxe)" = "One More From The Road"."""
    # "It's Alive" is "Its Alive" (CodeRabbit, review of #276).
    joined = re.sub(r"(?<=\w)['’](?=\w)", "", strip_bracketed(title or "").casefold())
    return re.sub(r"[^\w]+", " ", joined).strip()


def live_albums(data: dict) -> set[str]:
    """The releases on which MusicBrainz says this recording is live, by album_key.

    A live mark counts for a copy only on its OWN album: AcoustID matches some
    studio files to a live recording of the song ("Smoke On The Water" on Machine
    Head, "Changes" on Hunky Dory), whose mark would make the studio copy lose
    (2026-11-01). A whole live album that MusicBrainz types Live counts ("Before
    The Flood", "One More From The Road"); so does every release of a recording
    MusicBrainz itself calls live (Grey, 2026-11-01: "make it a rule").
    """
    marked = _LIVE_RE.search(data.get("disambiguation") or "") is not None
    return {
        album_key(r.get("title"))
        for r in data.get("releases") or []
        if marked or "Live" in ((r.get("release-group") or {}).get("secondary-types") or [])
    } - {""}


def same_song_pairs(rows: list[dict], grouped: set[frozenset[str]]) -> list[tuple[dict, dict, str]]:
    """(keeper, loser, works) for each copy to stage against the keeper of its song.

    *rows* are catalogue rows with mb_work_ids; *grouped* the audio pairs already in
    one duplicate group. Copies linked by staged pairs are one song to the resolver,
    which ranks them together, so they are ranked together here: a copy kept by hand
    never loses, not even through a third copy (2026-11-01, Lynyrd Skynyrd: two pairs
    sharing "Introduction - Workin' For MCA" moved the kept "One More From The Road"
    take). The keeper is the keep rule's among the copies that may lose.
    """
    by_song: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for r in rows:
        if r.get("mb_work_ids") and r.get("mb_work_artist"):
            by_song[(r["mb_work_artist"].casefold(), r["mb_work_ids"])].append(r)
    pairs = []
    for (_, works), members in by_song.items():
        # The copies each one is linked to by a pair, through any number of others.
        linked = {i: {i} for i in range(len(members))}
        fit: set[frozenset[str]] = set()  # pairs that pass every guard on their own
        for i, a in enumerate(members):
            for j in range(i + 1, len(members)):
                b = members[j]
                if not a["audio_hash"] or not b["audio_hash"] or a["audio_hash"] == b["audio_hash"]:
                    continue  # no audio identity, or one recording twice (EXACT finds that)
                da, db = float(a.get("duration") or 0), float(b.get("duration") or 0)
                if min(da, db) <= 0 or 2 * min(da, db) < max(da, db):
                    continue
                # A pair already grouped is not staged again, but it still links its
                # copies: a second run before the resolver must rank 1, 2 and 3 as
                # the resolver will, not stage 2/3 apart (CodeRabbit, review of #274).
                if frozenset((a["audio_hash"], b["audio_hash"])) not in grouped:
                    fit.add(frozenset((a["audio_hash"], b["audio_hash"])))
                joined = linked[i] | linked[j]
                for k in joined:
                    linked[k] = joined
        songs = {id(s): [dict(members[k]) for k in sorted(s)] for s in linked.values()}
        for copies in songs.values():
            if len(copies) < 2:
                continue
            _rank(copies)
            # A copy kept by hand is the keeper or is left out (Grey, 2026-11-01).
            if not copies[0].get("kept_by_person_at"):
                copies = [m for m in copies if not m.get("kept_by_person_at")]
                _rank(copies)
            keeper = copies[0]
            # Only a pair that passes on its own: a link through a copy left out
            # (kept by hand, 160 s between 100 s and 250 s) is no link (CodeRabbit,
            # review of #274). Nor a link through a copy that moves: staging 160/250
            # too would let the resolver rank all three and move the 250 s copy
            # against one under half its length, the pair Grey's guard leaves alone.
            pairs += [
                (keeper, m, works)
                for m in copies[1:]
                if not m.get("kept_by_person_at")
                and frozenset((keeper["audio_hash"], m["audio_hash"])) in fit
            ]
    return pairs


def _grouped(conn) -> set[frozenset[str]]:
    """Audio pairs already together in one duplicate group that still stands."""
    groups: dict[str, set[str]] = defaultdict(set)
    for r in conn.execute(
        "SELECT group_id, audio_hash FROM duplicates "
        "WHERE COALESCE(audio_hash, '') <> '' AND status <> 'stale'"
    ):
        groups[r["group_id"]].add(r["audio_hash"])
    return {frozenset((a, b)) for hs in groups.values() for a in hs for b in hs if a < b}


class MBWorkStage(BaseStage):
    """Stage copies of one song held under two titles, found by MusicBrainz work."""

    NAME = "mb-work"

    def validate(self, ctx: RunContext) -> None:
        """Nothing to refuse: a copy with no recording is simply not asked about."""

    def _to_ask(self, ctx: RunContext) -> list:
        from .acousticid import _ensure_columns as _fingerprint_columns

        _fingerprint_columns(ctx.conn)  # acousticid_recording, on a vault that has never had it
        return ctx.conn.execute(
            "SELECT id, file_path, album, acousticid_recording FROM archive "
            "WHERE status = 'CATALOGUED' AND COALESCE(acousticid_recording, '') <> '' "
            "AND COALESCE(mb_work_checked_at, '') = '' ORDER BY id"
        ).fetchall()

    def _pairs(self, ctx: RunContext) -> list[tuple[dict, dict, str]]:
        rows = [
            dict(r)
            for r in ctx.conn.execute(
                "SELECT * FROM archive WHERE status = 'CATALOGUED' "
                "AND COALESCE(mb_work_ids, '') <> ''"
            )
        ]
        return same_song_pairs(rows, _grouped(ctx.conn))

    def dry_run(self, ctx: RunContext) -> StageResult:
        result = self._make_result(dry_run=True)
        asks, pairs = self._to_ask(ctx), self._pairs(ctx)
        result.files_processed = len(asks)
        result.notes.append(
            f"{len(asks)} recording(s) to ask MusicBrainz about (no lookup in a preview); "
            f"{len(pairs)} pair(s) of one song would be staged from what is known"
        )
        ctx.record_stage(result)
        return result

    def verify_effect(self, ctx: RunContext, result: StageResult) -> list[str]:
        """Every pair this run says it staged must be a pending WORK group."""
        rows = ctx.conn.execute(
            "SELECT e.file_path FROM events e WHERE e.run_id = ? "
            "AND e.event_type = 'MB_WORK_DUPE_FOUND' AND NOT EXISTS (SELECT 1 FROM duplicates d "
            "WHERE d.file_path = e.file_path AND d.duplicate_type = 'WORK' AND d.run_id = e.run_id)",
            (ctx.run_id,),
        ).fetchall()
        return [f"{len(rows)} staged pair(s) have no WORK group: {rows[0][0]}"] if rows else []

    def run(self, ctx: RunContext) -> StageResult:
        result = self._make_result(dry_run=False)
        rows = self._to_ask(ctx)
        unavailable = 0
        for n, row in enumerate(rows, 1):
            result.files_processed += 1
            try:
                works, first, live_on = recording_works(row["acousticid_recording"])
            except Unavailable as exc:
                unavailable += 1
                logger.debug("[mb-work] could not ask for %s: %s", row["file_path"], exc)
                if unavailable >= 20 and unavailable == n:
                    result.notes.append(
                        "MusicBrainz could not be reached -- stopped; nothing stamped"
                    )
                    break
                continue
            finally:
                time.sleep(_MB_RATE_S)
            ctx.conn.execute(
                "UPDATE archive SET mb_work_ids = ?, mb_work_artist = ?, mb_live = ?, "
                "mb_work_checked_at = ? WHERE id = ?",
                (",".join(works), first, int(album_key(row["album"]) in live_on),
                 datetime.now(timezone.utc).isoformat(timespec="seconds"), row["id"]),
            )  # fmt: skip
            if n % _COMMIT_EVERY == 0:
                ctx.conn.commit()
        ctx.conn.commit()

        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        pairs = self._pairs(ctx)
        for keeper, loser, song_works in pairs:
            group_id = f"work_{uuid.uuid4().hex[:8]}"
            for member in (keeper, loser):
                ctx.conn.execute(
                    "INSERT OR IGNORE INTO duplicates (group_id, file_path, duplicate_type, "
                    "confidence, status, run_id, staged_at, audio_hash) "
                    "VALUES (?, ?, 'WORK', 1.0, 'pending', ?, ?, ?)",
                    (group_id, member["file_path"], ctx.run_id, now, member["audio_hash"]),
                )
            ctx.log_event(
                "MB_WORK_DUPE_FOUND",
                file_path=loser["file_path"],
                new_value=keeper["file_path"],
                stage=self.NAME,
                note=f"one song by MusicBrainz work {song_works}; the keep rule keeps {keeper['title']!r}",
            )
            logger.info("[mb-work] %r == %r", loser["title"], keeper["title"])
        ctx.conn.commit()
        result.files_changed = len(pairs)
        result.notes.append(
            f"asked {result.files_processed - unavailable}; {len(pairs)} pair(s) of one song "
            "staged for the resolver (moved to REVIEW next run)"
            + (f"; {unavailable} not reachable (asked again next run)" if unavailable else "")
        )
        ctx.record_stage(result)
        return result
