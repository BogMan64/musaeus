#!/usr/bin/env python3
"""
MUSAEUS — DupeResolver Stage (end of Act 2)

Physically relocates duplicate-group losers out of the batch and into a
review folder that mirrors ALAC-Library's own shape exactly, per Grey's
explicit instruction: "Same as Dedup folder, which should be the same
as ALAC-Library. Artist folder, album folder, song tracks." Nothing is
ever deleted -- this is a hold area for human review, not a resolution.

Runs at the END of Act 2 (after Sentinel/CrossDupe/NearDupe have all had
a chance to flag everything they're going to flag for this batch), and
BEFORE Act 3 (Canonicalize/Forge/Tagger/Finalize). This ordering is the
actual point of building this now rather than later: a confirmed
duplicate gets pulled out of the batch before any ffmpeg conversion or
loudness measurement is wasted on a file that's about to be set aside
anyway.

Destination shape:
    ALAC-Library/DUPES_MOVED_FOR_REVIEW/<YYYY-MM-DD>/<Artist>/<Album>/<Track>.ext

Same dated-batch-folder convention as Finalize's own output, same
Artist/Album/Track naming (via organize.py's build_track_filename/
sanitize_path_component -- identical helpers, so a resolved file that
turns out NOT to be a duplicate after human review lands at exactly the
same path Finalize would have produced for it).

Keeper selection:
  - EXACT / NEAR groups (multiple files WITHIN this batch): Grey's keep
    rule (keep_rule.py, via _keeper_sort_key and _rank) -- the one rule.
    `musaeus dedupe`'s auto writes nothing and leaves groups to it.
  - CROSS_BATCH groups (this batch's file vs. something already in
    ALAC-Library from a prior batch): there is nothing to choose
    between -- the prior-batch copy is untouched and already safe, so
    the incoming file is simply the one that moves.

Two resolution sources feed the same move+manifest logic (Grey's
2026-08-12 fix, after a confirmed incident -- see below):
  1. duplicates-table-driven groups (status='pending') -- NEAR,
     CROSS_BATCH, and any freshly-detected EXACT group.
  2. Live EXACT-hash clusters, derived directly from archive.audio_hash
     collisions among CATALOGUED rows (_get_live_exact_clusters),
     bypassing duplicates.status entirely. This exists because
     `musaeus dedupe --auto`/manual review only ever flips
     duplicates.status -- it never moves a file, and duplicates.file_path
     goes stale the moment a file is later finalized to a new path by
     the normal pipeline. Before this fix, that meant an EXACT decision
     made via `musaeus dedupe` was silently never enforced: confirmed in
     the real vault, 6,434 EXACT-type duplicates rows had a stale
     'archive' decision that nothing downstream ever acted on, and 3,480
     collision-suffixed filenames (e.g. "... (2).m4a") landed in
     ALAC-Library in one night as a direct, physical consequence -- both
     copies of each pair got canonicalized and finalized side by side.
     audio_hash survives every move Canonicalize/Finalize make, so
     re-deriving live duplicate clusters from it (rather than trying to
     reconcile a stale path back to an old decision) catches this
     historical backlog and any future recurrence in one mechanism.

Every move is recorded in a per-run manifest CSV (source, destination,
group_id, duplicate_type, moved_codec, moved_bitrate, kept_path,
kept_codec, kept_bitrate -- the codec/bitrate columns exist so a human
reviewing the CSV can see the actual signal behind each decision at a
glance, not just the decision itself) plus an auto-generated bash
restore script (mkdir -p + mv -n pairs, chmod +x) -- ORPHEUS's own
move_lesser_dedupe_candidates.py pattern, ported directly: never
destructive, always reversible from one file.

What this stage deliberately does NOT do:
  - Decide whether something IS a duplicate. That's Sentinel/CrossDupe/
    NearDupe's job, already done by the time this stage runs, for
    everything except the live-EXACT-hash-cluster path above, which
    re-derives the keeper fresh from current data since a stale prior
    decision can't be reliably replayed.
  - Touch groups/rows already resolved -- a duplicates-table group with
    nothing left in 'pending', or an archive row already at
    status='DUPE_REVIEW', is left alone. Re-running this stage is safe.
"""

from __future__ import annotations

import csv
import logging
import os
import re
import shlex
import shutil
import stat
from collections.abc import Sequence
from datetime import datetime, timezone
from pathlib import Path

from ..context import RunContext, StageResult
from ..db import SET_ASIDE_STATUSES
from ..dedupe import ARCHIVE_USER, KEEP_USER
from ..keep_rule import LENGTH_SLACK_S, keep_key
from .base import BaseStage
from .organize import (
    _remove_emptied_dirs,
    build_track_filename,
    destination_root,
    sanitize_path_component,
    unique_path,
)

logger = logging.getLogger(__name__)


def _batch_date(ctx: RunContext) -> str:
    """Same convention as FinalizeStage._batch_date -- overridable for
    tests, defaults to today's real UTC date, computed once per run."""
    override = ctx.get("finalize_batch_date")
    if override:
        return str(override)
    return datetime.now(tz=timezone.utc).strftime("%Y-%m-%d")


def _connected_groups(conn, group_ids: list[str]) -> list[list[str]]:
    """Merge groups that share a file into single components.

    Groups overlap. NEAR matching is fuzzy, so one recording lands in
    several groups, and resolving each independently lets them contradict
    each other: measured on the live vault 2026-08-31, 2,918 NEAR files sit
    in more than one group, and one -- "Al Green - Let's Stay Together" --
    is marked `keep` in near_f122326e and `archive` in near_2231ee82 at the
    same time. Nothing reconciles those. Which one wins is decided by
    whichever group happens to be processed last.

    `already_moved` in _resolve() was the previous mitigation, but it only
    stops a later group MISREPORTING an earlier group's move as "file
    missing". It does not stop that group deciding to move a file an
    earlier group had chosen to keep -- it just makes the outcome quiet.

    Merging first makes the contradiction unrepresentable: one keeper per
    component, and every other member of it is a loser. This is the same
    correction applied by hand to the 102 ACOUSTIC groups on 2026-08-31,
    where 102 groups collapsed to 95 components.

    EXACT clusters keyed by audio_hash cannot overlap -- a file has exactly
    one hash -- so this only concerns the duplicates-table path.
    """
    if not group_ids:
        return []

    parent: dict[str, str] = {g: g for g in group_ids}

    def find(x: str) -> str:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a: str, b: str) -> None:
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[ra] = rb

    placeholders = ",".join("?" for _ in group_ids)
    rows = conn.execute(
        f"""
        SELECT file_path, group_id FROM duplicates
         WHERE group_id IN ({placeholders}) AND status = 'pending'
        """,
        group_ids,
    ).fetchall()

    by_path: dict[str, list[str]] = {}
    for r in rows:
        by_path.setdefault(r["file_path"], []).append(r["group_id"])
    for shared in by_path.values():
        for g in shared[1:]:
            if g in parent and shared[0] in parent:
                union(shared[0], g)

    components: dict[str, list[str]] = {}
    for g in group_ids:
        components.setdefault(find(g), []).append(g)
    return [sorted(v) for v in components.values()]


def _get_pending_groups(conn) -> list[str]:
    rows = conn.execute(
        f"SELECT DISTINCT group_id FROM duplicates WHERE {ACTED_ON_SQL} ORDER BY group_id"
    ).fetchall()
    return [r[0] for r in rows]


# Markers of a reissued/reprocessed version rather than the original
# release. Grey's rule (2026-08-21): a remaster and its original are the
# SAME song for grouping purposes, but when one must be kept, "the
# original trumps the remaster".
_REISSUE_MARKERS: tuple[str, ...] = (
    "remaster",
    "remastered",
    "remix",
    "re-recorded",
    "rerecorded",
    "anniversary edition",
    "deluxe edition",
    "expanded edition",
    "digital remaster",
)
_REISSUE_RE = re.compile(
    r"\b(?:"
    + "|".join(re.escape(w) for w in sorted(_REISSUE_MARKERS, key=len, reverse=True))
    + r")\b",
    re.IGNORECASE,
)


# A live recording is a different performance, not a worse copy -- but when
# a duplicate group holds both and only one can be kept, Grey's rule
# (confirmed 2026-08-22) is studio first. Ranked BELOW the reissue test so
# a studio remaster does not beat a live original on this alone; the codec
# constraint still outranks both.
_LIVE_MARKERS: tuple[str, ...] = (
    "live",
    "in concert",
    "unplugged",
    "concert",
    "at the bbc",
    "bbc session",
    "radio session",
    "live session",
)
_LIVE_RE = re.compile(
    r"\b(?:"
    + "|".join(re.escape(w) for w in sorted(_LIVE_MARKERS, key=len, reverse=True))
    + r")\b",
    re.IGNORECASE,
)


def _is_live(m: dict) -> bool:
    """True if this copy advertises itself as a live recording.

    Read from title AND album: sources put it in either -- "Stormy Monday
    (live at Fillmore East)" as a title, "Unplugged" as an album.
    """
    haystack = f"{m.get('title') or ''} {m.get('album') or ''}"
    return bool(_LIVE_RE.search(haystack))


def _is_reissue(m: dict) -> bool:
    """True if this copy advertises itself as a remaster/reissue.

    Read from title AND album, because the marker lands in either
    depending on the source -- "A Monday Date (Remastered)" as a title,
    "Chicago High Life (2013 Remaster)" as an album.
    """
    haystack = f"{m.get('title') or ''} {m.get('album') or ''}"
    return bool(_REISSUE_RE.search(haystack))


#: duplicates.status values meaning "this row has already been dealt with".
#: A member in one of these states is not a candidate to keep.
# Not 'keep'. An EXACT group is named after its recording (dup_<hash>), so a
# new arrival identical to a library master joins the master's OLD group,
# where the master is still marked 'keep'. Ranked as already dealt with, the
# master lost to its own copy (P!nk, Pointer Sisters, SRV, 2026-09-26).
_ALREADY_RESOLVED: frozenset[str] = frozenset({"archive", "review"})
#: Never the keeper: moved away already, or archived by a person.
_NEVER_KEEPER: frozenset[str] = _ALREADY_RESOLVED | {ARCHIVE_USER}
#: The statuses the resolver acts on, and that the plan and status counts count:
#: a group a person decided whole has no 'pending' row and still moves files
#: (review of #123, finding 7).
ACTED_ON_SQL = f"status IN ('pending', '{ARCHIVE_USER}')"


#: The retired bake's target. A copy measured here (within the tolerance) was
#: made by it; an original sits here only by chance, rarely. A copy not yet
#: measured -- every new arrival at Act 2 -- is never taken for one.
_BAKED_LUFS = -18.0
_BAKED_TOLERANCE = 0.3


def _looks_baked(m: dict) -> bool:
    lufs = m.get("lufs")
    return lufs is not None and abs(float(lufs) - _BAKED_LUFS) <= _BAKED_TOLERANCE


def _share_loudness(members: list[dict]) -> None:
    """Give an unmeasured member the loudness of a measured one with the same audio.

    Loudness is measured in Act 3, so at Act 2 a new arrival has none. An
    arrival with the SAME audio as a master measured at -18 LUFS is the same
    baked copy, but read on its own it never looked baked, and won: 119
    masters were swapped for identical copies of themselves (2026-09-26).
    """
    measured: dict[str, float] = {}
    for m in members:
        h = m.get("current_hash") or m.get("recorded_hash") or m.get("audio_hash")
        if h and m.get("lufs") is not None:
            measured.setdefault(h, m["lufs"])
    for m in members:
        h = m.get("current_hash") or m.get("recorded_hash") or m.get("audio_hash")
        if h and m.get("lufs") is None and h in measured:
            m["lufs"] = measured[h]


def _keeper_sort_key(m: dict) -> tuple[int, ...]:
    """Best keeper first: Grey's keep rule (musaeus/keep_rule.py), one rule for
    the resolver and the swap tool.

    The resolver ranked by codec, baked, reissue, live, then BITRATE and size;
    Grey's rule of 2026-10-03 says sample rate only ("bitrate between two ALAC
    copies is noise") and then the longer copy. Planned on the live vault
    2026-10-05: 32 of 256 moves kept the shorter copy and 3 kept the lower
    sample rate. Grey: "you may change the keep rule". The length step is
    applied by _rank (a sort key cannot hold a 2 s tolerance).

    Order: already moved away; format; an original over an old baked copy;
    studio over live; original over remaster; sample rate; then the copy
    already filed stays; bitrate and size only break a tie nothing else can
    (a fixed order, not a judgement of quality).
    """
    return (
        # A member this resolver has already moved away cannot be the keeper.
        # Ranked first because it is not a quality judgement at all -- it is
        # whether the file is still where the library expects it.
        #
        # P0-E, 2026-09-09: losers are marked 'archive' while the keeper stays
        # 'pending', so the group is still pending on the next run and every
        # member -- moved ones included -- is re-ranked. An already-moved
        # member winning would be named keeper and the real keeper moved away
        # after it, leaving the library with neither.
        #
        # A member with no catalogue row at its path is not there either: the
        # file was filed, moved or removed since the group was found.
        # A copy a person archived in `musaeus dedupe` is never the keeper.
        1
        if (m.get("dup_status") or "") in _NEVER_KEEPER
        or ("current_row" in m and m["current_row"] is None)
        else 0,
        *keep_key(m),
        # Equally good copies: keep the one already filed. Size decided this
        # before, so a few bytes of tags on a new arrival swapped 79 library
        # copies for identical ones (2026-09-25).
        0 if m.get("finalized_at") else 1,
        -(m.get("bitrate") or 0),
        -(m.get("size_bytes") or 0),
    )


def _rank(members: list[dict]) -> None:
    """Sort best keeper first, then Grey's length step (keep_rule step 5).

    Among the members tied with the best on every step before length, the
    longest is kept when it is at least LENGTH_SLACK_S longer than the one
    otherwise first: "Higher Love" 5:48.7 and 5:51.5, both 192 kHz, kept the
    shorter by bitrate (2026-10-05).
    """
    members.sort(key=_keeper_sort_key)
    if len(members) < 2:
        return
    steps = len(keep_key(members[0])) + 1
    best = _keeper_sort_key(members[0])[:steps]
    tied = [m for m in members if _keeper_sort_key(m)[:steps] == best]
    longest = max(tied, key=lambda m: float(m.get("duration") or 0))
    gap = float(longest.get("duration") or 0) - float(members[0].get("duration") or 0)
    if longest is not members[0] and gap >= LENGTH_SLACK_S:
        members.remove(longest)
        members.insert(0, longest)


def _get_group_members(conn, group_id: str) -> list[dict]:
    """Members of one duplicate group, per the duplicates table, sorted
    by _keeper_sort_key (best keeper candidate first)."""
    rows = conn.execute(
        """
        SELECT d.file_path, d.duplicate_type, d.confidence, d.status AS dup_status,
               d.audio_hash AS recorded_hash, a.audio_hash AS current_hash, a.id AS current_row,
               a.status AS current_status, a.finalized_at, a.lufs,
               a.artist, a.album, a.title, a.ext, a.codec, a.bitrate, a.size_bytes,
               a.sample_rate, a.duration
          FROM duplicates d
          LEFT JOIN archive a USING (file_path)
         WHERE d.group_id = ?
        """,
        (group_id,),
    ).fetchall()
    members = [dict(r) for r in rows]
    _rank(members)
    return members


def _get_live_exact_clusters(conn) -> list[list[dict]]:
    """
    Find clusters of CATALOGUED archive rows sharing the same audio_hash
    -- i.e. exact-content duplicates still sitting live in the pipeline
    right now -- derived directly from archive.audio_hash rather than
    the duplicates table's own bookkeeping.

    This is a deliberate, confirmed-necessary bypass, not a stylistic
    choice (2026-08-12 incident): `musaeus dedupe --auto`/manual review
    only ever flips duplicates.status to 'keep'/'archive' -- it never
    moves a file or touches archive.file_path or archive.status. And
    duplicates.file_path itself goes stale the moment a file is later
    finalized to a new path by the normal pipeline (INBOX -> STAGING ->
    ALAC-Library), which happens to EVERY file regardless of any
    dedupe.py decision, since nothing downstream consults
    duplicates.status at all. The result, confirmed in the real vault:
    6,434 EXACT-type duplicates rows had a stale 'archive' decision that
    was never physically enforced, and 3,480 collision-suffixed
    filenames (e.g. "... (2).m4a") landed in ALAC-Library in a single
    night as a direct, physical consequence -- both the "keeper" and the
    "archived" copy of each pair were canonicalized and finalized side
    by side, because nothing after Sentinel/dedupe.py ever looked at
    duplicates.status again.

    audio_hash is the fix: Canonicalize/Finalize update file_path and
    status but never touch audio_hash, so it stays a reliable identity
    key across any number of moves -- unlike file_path, which is exactly
    what goes stale. Querying live CATALOGUED rows by audio_hash finds
    every still-unresolved exact-content duplicate regardless of what
    duplicates.status claims, catching both this historical backlog and
    any future recurrence in one mechanism, rather than trying to
    reconcile stale paths back to old decisions that can no longer be
    reliably replayed.
    """
    rows = conn.execute(
        """
        SELECT audio_hash, COUNT(*) as n
          FROM archive
         WHERE status = 'CATALOGUED' AND audio_hash IS NOT NULL AND audio_hash != ''
         GROUP BY audio_hash
        HAVING COUNT(*) > 1
        """
    ).fetchall()

    clusters: list[list[dict]] = []
    for row in rows:
        members = conn.execute(
            """
            SELECT file_path, artist, album, title, ext, codec, bitrate, size_bytes, finalized_at,
                   lufs, audio_hash, sample_rate, duration
              FROM archive
             WHERE audio_hash = ? AND status = 'CATALOGUED'
            """,
            (row["audio_hash"],),
        ).fetchall()
        member_dicts = [dict(m) for m in members]
        _share_loudness(member_dicts)
        _rank(member_dicts)
        clusters.append(member_dicts)
    return clusters


def _mark(ctx: RunContext, group_ids: Sequence[str], file_path: str, status: str) -> None:
    """Record a verdict on a path in EVERY group of its component.

    Groups sharing a file are resolved as one, but only the first group's
    rows were marked; the rest stayed 'pending' (145 after the Act 2 of
    2026-09-26) and came back on a later Act 2 as "the file at that path is
    now a different recording" once Act 3 had renamed their files.
    """
    marks = ",".join("?" for _ in group_ids)
    ctx.conn.execute(
        f"UPDATE duplicates SET status = ? WHERE group_id IN ({marks}) AND file_path = ?",
        (status, *group_ids, file_path),
    )


def _mismatch(m: dict) -> bool:
    """True if a group member cannot be trusted to be the recording the group was about.

    Bug 1. A member is recorded by PATH, and by now that path may hold a
    different recording -- renamed files free paths and other files take
    them. The recording the group was about is duplicates.audio_hash,
    captured when the group was found; the file there now is
    archive.audio_hash at that path. Unless every member still matches,
    nothing in the group moves: a wrong keeper is as bad as a wrong loser.
    Unverifiable -- no identity recorded (rows from before 2026-09-24), or a
    catalogue row there with no audio_hash -- counts as not matching, because
    that is precisely the population the bug came from.

    A path with NO catalogue row is different: the file was moved away by
    another stage, nothing can be moved wrongly from there, and _move_losers
    already skips such a member (see its relocation handling).
    """
    if not m.get("recorded_hash"):
        return True
    if m.get("current_row") is None:
        return False
    return bool(m.get("current_hash") != m.get("recorded_hash"))


def _pick_keeper_and_losers(members: list[dict]) -> tuple[dict | None, list[dict]]:
    """
    Members are already ranked by Grey's keep rule (_rank, keep_rule.py),
    so the first row is the keeper. CROSS_BATCH groups only ever have one member in THIS
    batch's duplicates table (the incoming file -- the prior-batch copy
    isn't a row here at all), so that lone member is always the "loser"
    relative to the untouched, already-safe library copy.
    """
    if not members:
        return None, []
    keep = members[0]
    losers = members[1:] if len(members) > 1 else members
    if len(members) == 1:
        return None, members  # CROSS_BATCH: nothing to keep, incoming file moves
    return keep, losers


_MANIFEST_FIELDS = [
    "source",
    "destination",
    "group_id",
    "duplicate_type",
    "moved_codec",
    "moved_bitrate",
    "kept_path",
    "kept_codec",
    "kept_bitrate",
]


class _MoveLog:
    """The run's manifest and restore script, written as each move happens.

    Both were written once, at the end: a killed run left files moved with no
    record of where they came from (review of #86, finding 10). A move's
    restore line is on disk before the file moves, and only acts if the file
    is where the move put it, so the script is right after a kill at any point.
    The files appear with the first move; a run that moves nothing writes none.
    """

    def __init__(self, review_dir: Path) -> None:
        self.review_dir = review_dir
        self.stamp = datetime.now(tz=timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        self.manifest_path: Path | None = None
        self.restore_path: Path | None = None

    def _open(self) -> None:
        if self.restore_path is not None:
            return
        self.review_dir.mkdir(parents=True, exist_ok=True)
        self.manifest_path = self.review_dir / f"moved_manifest_{self.stamp}.csv"
        with open(self.manifest_path, "w", newline="", encoding="utf-8") as fh:
            csv.DictWriter(fh, fieldnames=_MANIFEST_FIELDS).writeheader()
        self.restore_path = self.review_dir / f"restore_{self.stamp}.sh"
        self.restore_path.write_text("#!/usr/bin/env bash\nset -euo pipefail\n\n", encoding="utf-8")
        self.restore_path.chmod(self.restore_path.stat().st_mode | stat.S_IEXEC)

    @staticmethod
    def _append(path: Path, text: str) -> None:
        with open(path, "a", encoding="utf-8", newline="") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())

    def before_move(self, source: str, destination: str) -> None:
        self._open()
        assert self.restore_path is not None
        # shlex.quote: a path is data, never shell (review of #86, finding 4).
        src, dst = shlex.quote(source), shlex.quote(destination)
        self._append(
            self.restore_path,
            f"if [ -e {dst} ]; then mkdir -p {shlex.quote(os.path.dirname(source))}; "
            f"mv -n {dst} {src}; fi\n",
        )

    def after_move(self, row: dict) -> None:
        self._open()
        assert self.manifest_path is not None
        with open(self.manifest_path, "a", newline="", encoding="utf-8") as fh:
            csv.DictWriter(fh, fieldnames=_MANIFEST_FIELDS, extrasaction="ignore").writerow(row)
            fh.flush()
            os.fsync(fh.fileno())


class DupeResolverStage(BaseStage):
    """
    DupeResolver — physically relocate duplicate-group losers into
    ALAC-Library/DUPES_MOVED_FOR_REVIEW/, mirroring ALAC-Library's own
    Artist/Album/Track shape. Never deletes; always reversible.
    """

    def verify_effect(self, ctx: RunContext, result: StageResult) -> list[str]:
        """A file this stage claims to have moved must be AT the new path.

        Moves are the costliest thing to get silently wrong: a stage that
        reports "moved 6,480 files" while the DB and disk disagree leaves
        rows pointing at nothing, and that is precisely how a file ended up
        treated as its own duplicate (scope doc section 4.17). Sampling a
        few is enough to catch a wholesale failure.
        """
        # This run's own moves, not whichever rows happen to be in review: a
        # check that sampled nothing used to answer "nothing wrong" (R4).
        moves = ctx.conn.execute(
            "SELECT new_value FROM events WHERE run_id = ? AND event_type = 'DUPE_MOVED_FOR_REVIEW' "
            "ORDER BY id DESC LIMIT 5",
            (ctx.run_id,),
        ).fetchall()
        # Every real move writes this event, and a change here IS a move.
        if result.files_changed and not moves and not result.dry_run:
            return [f"reported {result.files_changed} move(s) but recorded none in this run"]
        missing = [r["new_value"] for r in moves if not Path(r["new_value"]).exists()]
        if not missing:
            return []
        return [
            f"reported {result.files_changed} change(s) but {len(missing)} of "
            f"{len(moves)} files this run moved for review are not on disk"
        ]

    @classmethod
    def plan_candidates(cls, conn, cfg) -> tuple[int, str]:
        """Rows this stage would act on. Read-only; see planner.py."""
        n = conn.execute(
            f"SELECT COUNT(DISTINCT group_id) FROM duplicates WHERE {ACTED_ON_SQL}"
        ).fetchone()[0]
        return int(n), "duplicate groups awaiting resolution"

    NAME = "dupe-resolver"

    def validate(self, ctx: RunContext) -> None:
        count = len(_get_pending_groups(ctx.conn))
        logger.info("[dupe-resolver] %d pending duplicate group(s)", count)

    @staticmethod
    def _roots(ctx: RunContext) -> list[Path]:
        """Where a loser can be moved FROM, for the emptied-folder cleanup."""
        archive = getattr(getattr(ctx, "config", None), "alac_archive", None)
        tiers = [ctx.alac_library] + ([Path(archive)] if archive is not None else [])
        return [*tiers, ctx.inbox, ctx.staging]

    def _target_path(self, ctx: RunContext, member: dict, source: Path, batch_date: str) -> Path:
        artist = member.get("artist") or "Unknown Artist"
        album = member.get("album") or "Unsorted"
        title = member.get("title") or "Unknown Title"

        new_filename = build_track_filename(artist, title, source.suffix)
        artist_safe = sanitize_path_component(artist)
        album_safe = sanitize_path_component(album)

        target_dir = ctx.config.dupes_review_dir / batch_date / artist_safe / album_safe
        candidate = target_dir / new_filename
        return unique_path(candidate)

    def _write_manifest_and_restore_script(
        self, ctx: RunContext, batch_date: str, moves: list[dict]
    ) -> tuple[Path, Path]:
        """
        moves: list of dicts (source, destination, group_id,
        duplicate_type, moved_codec, moved_bitrate, kept_path,
        kept_codec, kept_bitrate). The codec/bitrate columns exist so a
        human reviewing the CSV can see, per row, the actual signal that
        drove each decision at a glance -- Grey's explicit ask
        (2026-08-12): without them, reviewing any single group meant
        manually joining archive.codec/bitrate by hand, which isn't
        workable across thousands of rows.
        Returns (manifest_path, restore_script_path).
        """
        log = _MoveLog(ctx.config.dupes_review_dir / batch_date)
        for m in moves:
            log.before_move(m["source"], m["destination"])
            log.after_move(m)
        assert log.manifest_path is not None and log.restore_path is not None
        return log.manifest_path, log.restore_path

    #: The run's move log; None in a dry run.
    _log: _MoveLog | None = None

    def _move_losers(
        self,
        ctx: RunContext,
        result: StageResult,
        batch_date: str,
        moved: list[dict],
        group_id: str,
        keeper: dict | None,
        losers: list[dict],
        dry_run: bool,
        update_duplicates_table: bool,
        already_moved: dict[str, str],
        group_ids: Sequence[str] = (),
    ) -> None:
        """
        Shared per-loser move+update+log+manifest-append logic, used by
        both resolution sources in _resolve() (duplicates-table-driven
        groups, and audio_hash-derived live EXACT clusters). Mutates
        result and moved in place.

        already_moved: source-path -> group_id that already resolved it,
        accumulated across every group processed so far THIS stage run.
        Members list is a snapshot taken once per group from _get_group_members/
        _get_live_exact_clusters -- it does not see moves made by other
        groups processed earlier in the same _resolve() call. A single
        physical file frequently gets staged into more than one group
        (e.g. flagged by both the EXACT/NEAR detector and CROSS_BATCH
        detector), so without this check, the second group to reach that
        file finds it already gone from its original path and misreports
        a legitimate prior move as "file missing on disk" -- confirmed
        as the dominant cause of a 51,310-error DupeResolver failure
        (2026-08-14 run): ~19,000 of those were exactly this, not real
        errors.
        """
        keeper_desc = keeper["file_path"] if keeper else "(no keeper on record)"
        gids = tuple(group_ids) or (group_id,)
        if losers and keeper is not None and not Path(keeper["file_path"]).exists():
            # Moving the losers would leave the library with no copy: the one
            # to keep is not on disk. Nobody checked, so a keeper whose file
            # had gone meant every real copy was moved out (review of #86,
            # finding 6). The group stays pending for a person to look at.
            # (No keeper at all is the lone CROSS_BATCH member, whose twin is
            # the master CrossDupe found in the library.)
            result.files_skipped += len(losers)
            result.notes.append(
                f"group {group_id}: the copy to keep ({keeper_desc}) is not on disk -- "
                f"nothing moved"
            )
            return
        for item_index, loser in enumerate(losers):
            result.files_processed += 1
            source = Path(loser["file_path"])
            source_key = str(source)
            dtype = loser.get("duplicate_type") or ""

            prior_group = already_moved.get(source_key)
            if prior_group is not None:
                result.files_skipped += 1
                result.notes.append(
                    f"[{dtype}] skipped {source.name}: already resolved under group {prior_group}"
                )
                continue

            if not source.exists():
                # Before treating this as a real error: the same physical
                # file may already have been resolved by a *different*
                # group in an earlier run (a title re-flagged as a
                # duplicate a second time after already being quarantined
                # once -- e.g. re-downloaded and re-detected). The
                # `already_moved` dict above only covers moves made earlier
                # in *this* _resolve() call; it can't see a prior run's
                # moves. The events log is the one place that history is
                # actually recorded (old_value=source at the time of the
                # original move), so check it before giving up. Confirmed
                # 2026-08-18: 422 duplicates-table rows stuck 'pending'
                # forever this way, all 370 distinct paths already moved
                # per a matching DUPE_MOVED_FOR_REVIEW event -- not lost
                # files, just a group that never got told its file was
                # already handled elsewhere.
                already_handled = ctx.conn.execute(
                    "SELECT 1 FROM events WHERE event_type = 'DUPE_MOVED_FOR_REVIEW' "
                    "AND old_value = ? LIMIT 1",
                    (source_key,),
                ).fetchone()
                if already_handled:
                    result.files_skipped += 1
                    result.notes.append(
                        f"[{dtype}] skipped {source.name}: already resolved by a prior run "
                        f"(stale duplicates-table row)"
                    )
                    if update_duplicates_table and not dry_run:
                        _mark(ctx, gids, source_key, "archive")
                    continue

                # Before calling it lost, ask the archive row where the file
                # lives now. The events check above only recognises a move
                # this stage itself made; a file relocated by any other
                # stage -- ClassicalComposer refiling under a composer, a
                # manual DUPE_REVIEW_REVERSED restore -- is equally moved,
                # and equally not missing. Measured 2026-08-25: five such
                # rows failed the whole stage (rc=1) when every one of the
                # files was safely on disk under a new path.
                # By its recording, not by path: the path is exactly what
                # changed. The old query compared a path with itself and could
                # never match, and its result was never used (R5, 2026-09-23).
                moved_elsewhere = None
                recorded = loser.get("recorded_hash") or loser.get("current_hash")
                if recorded:
                    for r in ctx.conn.execute(
                        "SELECT file_path FROM archive WHERE audio_hash = ? AND file_path != ?",
                        (recorded, source_key),
                    ):
                        if Path(r["file_path"]).exists():
                            moved_elsewhere = r["file_path"]
                            break
                if moved_elsewhere is None:
                    ev = ctx.conn.execute(
                        "SELECT file_path FROM events WHERE old_value = ? "
                        "AND file_path IS NOT NULL ORDER BY id DESC LIMIT 1",
                        (source_key,),
                    ).fetchone()
                    if ev and Path(ev["file_path"]).exists():
                        moved_elsewhere = ev["file_path"]
                if moved_elsewhere:
                    result.files_skipped += 1
                    result.notes.append(
                        f"[{dtype}] skipped {source.name}: relocated by another stage"
                    )
                    if update_duplicates_table and not dry_run:
                        _mark(ctx, gids, source_key, "archive")
                    continue

                # No file, and no archive row either: this duplicates-table
                # entry names a path the library stopped tracking long ago
                # -- an old INBOX path from before ingest moved the file, or
                # one already handled by a DUPE_RESTORED/STALE_ROW_DROPPED.
                # It is stale history, not a lost file.
                #
                # Safe to key on the missing row: a genuinely lost file
                # KEEPS its archive row pointing at the gone path, and
                # doctor's "rows with a missing file" check is what catches
                # that. This branch only fires where the library itself has
                # no record of the path at all.
                still_tracked = ctx.conn.execute(
                    "SELECT 1 FROM archive WHERE file_path = ? LIMIT 1", (source_key,)
                ).fetchone()
                if not still_tracked:
                    result.files_skipped += 1
                    result.notes.append(
                        f"[{dtype}] skipped {source.name}: stale duplicates-table row, "
                        f"no archive row for this path"
                    )
                    if update_duplicates_table and not dry_run:
                        _mark(ctx, gids, source_key, "archive")
                    continue

                result.files_errored += 1
                result.errors.append(f"{source}: file missing on disk")
                continue

            # Already set aside. Its target is its own path, which unique_path
            # turns into " (2)" -- a rename of a file nobody asked to move,
            # repeated on every run (2026-09-25, two Badfinger review copies).
            # Close the group so it is not retried.
            if source.is_relative_to(ctx.config.dupes_review_dir):
                result.files_skipped += 1
                result.notes.append(f"[{dtype}] left {source.name}: already in the review folder")
                if update_duplicates_table and not dry_run:
                    _mark(ctx, gids, source_key, "archive")
                continue

            # Inside the guard, not above it. The move below has isolated
            # per-item OSErrors since it was written, but _target_path was
            # one line outside that guard -- and it does filesystem work
            # (unique_path's exists() check), so it raises the same class of
            # error. On 2026-09-03 one 388-byte artist name raised OSError 36
            # here and aborted every remaining move in the run, leaving the
            # dedupe half-done. One bad path should cost one file, not the
            # stage. No rollback needed: nothing has been written yet.
            try:
                target = self._target_path(ctx, loser, source, batch_date)
            except OSError as exc:
                result.files_errored += 1
                result.errors.append(f"{source}: cannot build a target path: {exc}")
                logger.warning("[dupe-resolver] target path failed %s: %s", source, exc)
                continue

            if dry_run:
                result.notes.append(
                    f"[{dtype}] would move {source.name} -> {target} (keeping {keeper_desc})"
                )
                result.files_changed += 1
                already_moved[source_key] = group_id
                continue

            # Row first, then the move (scope section 4.25). A move cannot
            # be rolled back and a database write can, so this ordering
            # leaves neither half applied when something fails.
            #
            # Scoped to THIS item with a SAVEPOINT. There is one commit, after
            # every group, so the previous conn.rollback() here was not scoped
            # to the item at all -- it discarded the whole uncommitted
            # transaction. A failure on file 41 reverted all 40 earlier archive
            # updates while their files stayed physically moved: rows left
            # CATALOGUED pointing at sources that no longer existed, and a
            # manifest claiming all 40 had moved. That is precisely the
            # divergence this ordering exists to prevent.
            #
            # The savepoint name is built from a loop counter and never from a
            # filename: savepoint names are identifiers, so they cannot be
            # bound parameters and have to be interpolated.
            savepoint = f"dupe_item_{item_index}"
            ctx.conn.execute(f"SAVEPOINT {savepoint}")
            ctx.conn.execute(
                "UPDATE archive SET status = 'DUPE_REVIEW', file_path = ? WHERE file_path = ?",
                (str(target), str(source)),
            )
            try:
                target.parent.mkdir(parents=True, exist_ok=True)
                if self._log is not None:
                    self._log.before_move(str(source), str(target))  # on disk first
                shutil.move(str(source), str(target))
            except OSError as exc:
                # ROLLBACK TO does not release; without the RELEASE these
                # accumulate on the transaction stack for the whole run.
                # OSError covers shutil.Error, which subclasses it. A
                # sqlite3.Error here means something worse and is deliberately
                # not swallowed per-item.
                ctx.conn.execute(f"ROLLBACK TO SAVEPOINT {savepoint}")
                ctx.conn.execute(f"RELEASE SAVEPOINT {savepoint}")
                result.files_errored += 1
                result.errors.append(f"{source}: {exc}")
                logger.warning("[dupe-resolver] move failed %s: %s", source, exc)
                continue

            # The folder the file left may now be empty -- an album folder in
            # ALAC-Archival is a phantom album in a folder-browsed library
            # (2026-09-25: four of them from one Act 2). Up to, never including,
            # the root the file lived under.
            # The MOST SPECIFIC root, as organize decides it: a tier nested in
            # another (MUSAEUS_ALAC_ARCHIVE inside ALAC_Library) must stop the
            # climb, or the tier itself is removed (cloud review of #39).
            root = destination_root(source, self._roots(ctx))
            if root is not None:
                _remove_emptied_dirs(source.parent, root)

            if update_duplicates_table:
                _mark(ctx, gids, str(source), "archive")
            # The archive row's status must change too, and file_path
            # must follow the file to its new location -- otherwise a
            # later stage's WHERE status='CATALOGUED' query has no way
            # to know this row moved, and would try to act on a path
            # that no longer has a file (confirmed as a real failure
            # during a full-chain dry run: Canonicalize picked up a
            # DupeResolver-relocated row and errored on the missing
            # source). status='DUPE_REVIEW' is a new, distinct status
            # (not CATALOGUED, not GHOST -- this is an intentional,
            # tracked relocation, not a disappearance).
            ctx.log_event(
                "DUPE_MOVED_FOR_REVIEW",
                file_path=str(target),
                old_value=str(source),
                new_value=str(target),
                stage=self.NAME,
                note=f"group={group_id} type={dtype} kept={keeper_desc}",
            )
            moved.append(
                {
                    "source": str(source),
                    "destination": str(target),
                    "group_id": group_id,
                    "duplicate_type": dtype,
                    "moved_codec": loser.get("codec") or "",
                    "moved_bitrate": loser.get("bitrate") or "",
                    "kept_path": keeper["file_path"] if keeper else "",
                    "kept_codec": (keeper.get("codec") or "") if keeper else "",
                    "kept_bitrate": (keeper.get("bitrate") or "") if keeper else "",
                }
            )
            # The item is complete -- row, file, duplicates table and event.
            # Releasing here rather than straight after the move keeps the
            # bookkeeping inside the same unwind unit as the move itself.
            ctx.conn.execute(f"RELEASE SAVEPOINT {savepoint}")
            # Committed as each move completes: a single commit at the end let
            # a killed run keep its files moved and roll their rows back
            # (review of #86, finding 10).
            ctx.conn.commit()
            if self._log is not None:
                self._log.after_move(moved[-1])
            result.files_changed += 1
            already_moved[source_key] = group_id
            logger.info("[dupe-resolver] moved %s -> %s", source, target)

    def _resolve(self, ctx: RunContext, dry_run: bool) -> StageResult:
        result = self._make_result(dry_run=dry_run)
        groups = _get_pending_groups(ctx.conn)
        live_exact_clusters = _get_live_exact_clusters(ctx.conn)
        result.notes.append(f"pending duplicate group(s): {len(groups)}")
        if live_exact_clusters:
            result.notes.append(
                f"live EXACT-hash cluster(s) needing resolution "
                f"(audio_hash collisions among CATALOGUED rows, independent of "
                f"duplicates-table state): {len(live_exact_clusters)}"
            )

        if not groups and not live_exact_clusters:
            result.notes.append(
                "nothing to resolve — no pending duplicate groups or live exact clusters"
            )
            ctx.record_stage(result)
            return result

        batch_date = _batch_date(ctx)
        moved: list[dict] = []
        self._log = None if dry_run else _MoveLog(ctx.config.dupes_review_dir / batch_date)
        # Source-path -> group_id, accumulated across every group processed
        # in this _resolve() call. See _move_losers' docstring: a single
        # physical file often gets staged into more than one group, and
        # without this a later group misreports an earlier group's
        # successful move as "file missing on disk".
        already_moved: dict[str, str] = {}

        # ── Source 1: duplicates-table-driven groups (NEAR, CROSS_BATCH,
        # and any freshly-detected EXACT group still genuinely 'pending') ──
        # Resolve COMPONENTS, not groups. Overlapping groups otherwise reach
        # contradictory verdicts on the same file -- see _connected_groups.
        # Judged group by group, BEFORE groups sharing a file are joined:
        # judged per component, one group whose file had changed closed every
        # group joined to it, sound ones included -- 7 baked copies stayed
        # beside their originals that way (2026-09-26). A dropped group's
        # verdict on its members is void; the sound groups decide alone.
        sound: list[str] = []
        for gid in groups:
            group_members = _get_group_members(ctx.conn, gid)
            stale = [m for m in group_members if _mismatch(m)]
            if not stale:
                sound.append(gid)
                continue
            for m in stale:
                why = (
                    "no recording identity was stored with the group"
                    if not m.get("recorded_hash")
                    else "the file at that path is now a different recording"
                    if m.get("current_hash")
                    else "the catalogue row at that path has no audio fingerprint"
                )
                result.errors.append(
                    f"duplicate group {gid}: nothing moved -- {m['file_path']}: {why}"
                )
            result.files_skipped += len(group_members)
            if not dry_run:
                ctx.conn.execute(
                    f"UPDATE duplicates SET status = 'stale' WHERE group_id = ? AND {ACTED_ON_SQL}",
                    (gid,),
                )

        for component in _connected_groups(ctx.conn, sound):
            group_id = component[0]
            members = []
            seen_paths: set[str] = set()
            for gid in component:
                for m in _get_group_members(ctx.conn, gid):
                    if m["file_path"] in seen_paths:
                        continue
                    seen_paths.add(m["file_path"])
                    members.append(m)
            if not members:
                continue

            # A member already set aside is not a candidate at all -- neither
            # to move nor to KEEP. As a keeper it was the worse failure: a
            # review copy that outranked the library master kept its place,
            # and the master was moved out as the loser, leaving the library
            # with no copy (cloud review of #37, 2026-09-25). If that leaves
            # one member, it is the only live copy: nothing to resolve.
            aside = [m for m in members if m.get("current_status") in SET_ASIDE_STATUSES]
            if aside:
                members = [m for m in members if m not in aside]
                result.notes.append(
                    f"group {group_id}: {len(aside)} member(s) already set aside, left alone"
                )
                # Left alone, and closed: its row stayed 'pending' for ever,
                # so the group came back on every Act 2 listing the same
                # rows -- 17 groups, 2026-09-27.
                if not dry_run:
                    for m in aside:
                        _mark(ctx, component, m["file_path"], "archive")
                if len(members) < 2:
                    result.files_skipped += len(members) + len(aside)
                    if not dry_run:
                        ctx.conn.executemany(
                            "UPDATE duplicates SET status = 'archive' "
                            f"WHERE group_id = ? AND {ACTED_ON_SQL}",
                            [(gid,) for gid in component],
                        )
                    continue

            # One keeper for the whole component, so a file kept by one of
            # its groups can no longer be moved as another's loser.
            _share_loudness(members)
            _rank(members)
            keeper, losers = _pick_keeper_and_losers(members)
            # An incoming CROSS_BATCH duplicate always moves, also when its
            # group was merged with a NEAR group: the library already holds
            # it. Ranked with the rest it could be kept and an unrelated song
            # moved instead (R1 of the 2026-09-23 review, still true 2026-10-07).
            qs = ",".join("?" * len(component))
            cross = {
                r[0]
                for r in ctx.conn.execute(
                    f"SELECT file_path FROM duplicates WHERE duplicate_type = 'CROSS_BATCH' "
                    f"AND group_id IN ({qs})",
                    list(component),
                )
            }
            if cross and len(members) > 1:
                rest = [m for m in members if m["file_path"] not in cross]
                # Every member flagged: keep the ranked keeper. Choosing none
                # moved the whole group, library copies too (review of #86,
                # finding 3, a regression from the R1 fix).
                if rest:
                    keeper = rest[0]
                    losers = [m for m in members if m is not keeper]
            # A person's decision in `musaeus dedupe` wins over the ranking: the
            # copy they kept is the keeper, and every copy they kept stays
            # (review of #86, finding 9).
            # A kept copy no longer at its path cannot be the keeper: nothing
            # would ever move (review of #123, finding 3).
            kept = [m for m in members if m.get("dup_status") == KEEP_USER]
            user_kept = [m for m in kept if m.get("current_row") is not None]
            for m in kept:
                if m not in user_kept:
                    result.notes.append(
                        f"group {group_id}: a copy kept in `musaeus dedupe` is no longer at "
                        f"{m['file_path']}; the keep rule chose the keeper"
                    )
            if user_kept:
                keeper = user_kept[0]
                losers = [m for m in members if m.get("dup_status") != KEEP_USER]
            elif members and all(m.get("dup_status") == ARCHIVE_USER for m in members):
                # Every copy archived: one stays, so the library keeps the song
                # -- said, not done silently (review of #123, finding 11).
                result.notes.append(
                    f"group {group_id}: every copy was archived in `musaeus dedupe`; "
                    f"one is kept so the song stays: {keeper['file_path'] if keeper else '?'}"
                )
            self._move_losers(
                ctx,
                result,
                batch_date,
                moved,
                group_id,
                keeper,
                losers,
                dry_run=dry_run,
                update_duplicates_table=True,
                already_moved=already_moved,
                group_ids=component,
            )
            if keeper and not dry_run and keeper.get("dup_status") != KEEP_USER:
                _mark(ctx, component, keeper["file_path"], "keep")

        # ── Source 2: live EXACT-hash clusters, derived directly from
        # archive.audio_hash -- catches both the historical backlog left
        # behind by a stale duplicates.status decision, and any future
        # recurrence, without trying to reconcile a path that may no
        # longer be reliable. See _get_live_exact_clusters' docstring. ──
        # A person's keep counts for the recording they kept, not for whatever
        # is at that path now; and when they kept as many copies of a recording
        # as there are, none moves, filed under new paths or not (review of
        # #123, finding 6).
        kept_recording: dict[str, str] = {}
        kept_per_recording: dict[str, int] = {}
        for path, recorded in ctx.conn.execute(
            "SELECT file_path, audio_hash FROM duplicates WHERE status = ?", (KEEP_USER,)
        ):
            kept_recording[path] = recorded or ""
            if recorded:
                kept_per_recording[recorded] = kept_per_recording.get(recorded, 0) + 1
        for idx, members in enumerate(live_exact_clusters):
            # These lists were made before the groups above moved anything.
            # A member moved there is gone, and where the two sources ranked
            # a pair differently, re-ranking it here moved the copy the
            # groups had kept, leaving the library with neither.
            members = [m for m in members if m["file_path"] not in already_moved]
            if len(members) < 2:
                continue
            for m in members:
                m["duplicate_type"] = "EXACT"
            keeper, losers = members[0], members[1:]
            recording = members[0].get("audio_hash") or ""
            if recording and kept_per_recording.get(recording, 0) >= len(members):
                result.notes.append(
                    f"{len(members)} identical copies all kept in `musaeus dedupe`: left alone"
                )
                continue
            kept_by_person = [
                m for m in members if recording and kept_recording.get(m["file_path"]) == recording
            ]
            if kept_by_person:  # a person's keep in `musaeus dedupe` wins here too
                keeper = kept_by_person[0]
                losers = [m for m in members if m not in kept_by_person]
            synthetic_group_id = f"exacthash_{idx:06d}"
            self._move_losers(
                ctx,
                result,
                batch_date,
                moved,
                synthetic_group_id,
                keeper,
                losers,
                dry_run=dry_run,
                update_duplicates_table=False,
                already_moved=already_moved,
            )

        if not dry_run:
            ctx.conn.commit()

        if moved and self._log is not None:
            result.notes.append(f"moved {len(moved)} file(s) to review")
            result.notes.append(f"manifest: {self._log.manifest_path}")
            result.notes.append(f"restore script: {self._log.restore_path}")
        elif dry_run and result.files_changed:
            result.notes.append(
                f"[DRY RUN] would move {result.files_changed} file(s) — no manifest written"
            )

        if result.files_errored:
            result.success = False

        ctx.record_stage(result)
        return result

    def dry_run(self, ctx: RunContext) -> StageResult:
        return self._resolve(ctx, dry_run=True)

    def run(self, ctx: RunContext) -> StageResult:
        return self._resolve(ctx, dry_run=False)
