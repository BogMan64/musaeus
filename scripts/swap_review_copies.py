#!/usr/bin/env python3
"""Swap a library master for a better copy waiting in duplicate review.

Grey, 2026-10-03, on the duplicate-review list: "keep the copy that is ALAC
over FLAC, then studio over live, then quality, then length" -- the rule is
musaeus/keep_rule.py. Where the copy in review wins, it replaces the master.
Where the master wins, the review copy is deleted with
scripts/delete_reviewed_tracks.py, which is not this tool's job.

A SWAP IS THREE STEPS, BECAUSE A MASTER MUST NEVER BE MISSING

    --promote   the review copy R becomes a catalogued track again, carrying
                the library row L's artist, album, genre and MusicBrainz
                artist, so it files exactly where L is. Its canonicalize and
                finalize marks are cleared, which is precisely what Act 3
                selects.
    musaeus run
                R goes through like any arrival: Act 1's Corrupt decodes it
                (Finalize holds back an arrival that has not decoded clean, so
                `--act 3` alone files it only if it already has), then
                canonicalize and finalize: journal, hash index. Finalize files it beside L as
                " (2)", because L still holds the name.
    --retire    for every promoted pair whose R is now finalized in the
                masters, L goes through scripts/delete_reviewed_tracks.py --
                every tier, and L's audio onto the deny list.

The next Act 3's organize renames R's " (2)" to the plain name once L has
gone, and the next edition build removes L's copies and makes R's. Until
--retire both are in the library, which is the safe order: at no point is
the song absent.

A PATH IS NOT IDENTITY

The review copy's audio is re-hashed before it is promoted and must match
the hash recorded for it -- the duplicate resolver moved the wrong recording
out of the library in September by trusting a stored path (bug 1).

    python3 scripts/swap_review_copies.py --promote            # dry run
    python3 scripts/swap_review_copies.py --promote --execute --limit 1   # trial
    python3 scripts/swap_review_copies.py --promote --execute
    musaeus run
    python3 scripts/swap_review_copies.py --retire             # dry run
    python3 scripts/swap_review_copies.py --retire --execute
"""

from __future__ import annotations

import argparse
import collections
import functools
import sqlite3
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from musaeus.config import MusicConfig  # noqa: E402
from musaeus.hasher import audio_hash_safe  # noqa: E402
from musaeus.keep_rule import decide, keep_key  # noqa: E402
from musaeus.stages.dupe_resolver import (  # noqa: E402
    _share_baked_mark,
    _share_live_take,
    _share_unknown_album,
    _share_version_label,
)

EVENT = "SWAP_PROMOTED"
#: The library row's facts the promoted copy takes, so it files where L is.
FACTS = ("artist", "album", "genre", "mb_artist_name", "mb_artist_id")
DELETE_TOOL = Path(__file__).resolve().parent / "delete_reviewed_tracks.py"


@dataclass
class Pair:
    review: sqlite3.Row
    library: sqlite3.Row
    step: str


def _note_field(note: str, key: str) -> str | None:
    """'group=G type=T kept=/a path/with spaces' -> the value; kept is last."""
    marker = f"{key}="
    if marker not in note:
        return None
    value = note.split(marker, 1)[1]
    return value if key == "kept" else value.split(" ", 1)[0]


def library_copy(conn: sqlite3.Connection, review: sqlite3.Row) -> sqlite3.Row | None:
    """The catalogued copy this review row was set aside in favour of.

    The kept path recorded when it was set aside, following any rename since
    (ARTIST_CONSOLIDATED moves) and the masters-first move of 2026-09-25; then
    the audio the duplicate group recorded for that path or for its keeper.
    """
    ev = conn.execute(
        "SELECT note FROM events WHERE event_type='DUPE_MOVED_FOR_REVIEW' AND new_value=? "
        "ORDER BY id DESC LIMIT 1",
        (review["file_path"],),
    ).fetchone()
    if ev is None or not ev["note"]:
        return None
    kept = _note_field(ev["note"], "kept")
    group = _note_field(ev["note"], "group")
    if not kept:
        return None
    moved = dict(
        conn.execute(
            "SELECT old_value, new_value FROM events WHERE event_type='ARTIST_CONSOLIDATED'"
        ).fetchall()
    )
    path, seen = kept, set()
    while path in moved and path not in seen:
        seen.add(path)
        path = moved[path]
    for candidate in (path, path.replace("/Libraries/ALAC_Library/", "/Libraries/ALAC-Archival/")):
        row = conn.execute(
            "SELECT * FROM archive WHERE file_path=? AND status='CATALOGUED'", (candidate,)
        ).fetchone()
        if row is not None:
            return row
    found = (
        conn.execute(
            "SELECT audio_hash FROM duplicates WHERE file_path=? AND audio_hash IS NOT NULL LIMIT 1",
            (kept,),
        ).fetchone()
        or conn.execute(
            "SELECT audio_hash FROM duplicates WHERE group_id=? AND status IN ('keep', 'keep_user') "
            "AND audio_hash IS NOT NULL LIMIT 1",
            (group,),
        ).fetchone()
    )
    if found is None:
        return None
    return conn.execute(
        "SELECT * FROM archive WHERE audio_hash=? AND status='CATALOGUED' ORDER BY id LIMIT 1",
        (found[0],),
    ).fetchone()


def review_wins(
    conn: sqlite3.Connection, losers: list[int] | None = None
) -> tuple[list[Pair], collections.Counter]:
    """The best review copy of each song, where it beats the library copy; a tally of the rest.

    *losers*, if given, collects the review rows the rule says to delete: the
    library copy wins or ties, or another review copy of the same song is
    better. One rule decides both lists.

    All review copies of a song are ranked TOGETHER. On 2026-10-03 each was
    compared with the master alone, so a song with two better copies had both
    promoted -- 40 duplicate masters to clean up by hand.
    """
    wins: list[Pair] = []
    tally: collections.Counter = collections.Counter()
    by_song: dict[int, tuple[sqlite3.Row, list[sqlite3.Row]]] = {}
    for review in conn.execute("SELECT * FROM archive WHERE status='DUPE_REVIEW' ORDER BY id"):
        library = library_copy(conn, review)
        if library is None:
            tally["no library copy"] += 1
            continue
        # A person kept this master over the copies in review (Grey's Oct 21 sheet):
        # never swapped, never deleted here -- 143 of 144 swaps listed 2026-10-25.
        if library["kept_by_person_at"]:
            tally["kept by a person"] += 1
            continue
        by_song.setdefault(library["id"], (library, []))[1].append(review)

    def better_first(a: dict, b: dict) -> int:
        winner, _ = decide(a, b)
        return -1 if winner == "review" else 1 if winner == "library" else 0

    for library, reviews in by_song.values():
        # Loudness shared across the master and every copy before ranking, as
        # decide() shares it for a pair: ranked apart, a copy with the master's
        # audio never looked baked (Codex review of #226, round 2).
        rows = {r["id"]: r for r in reviews}
        lib = dict(library)
        copies = [dict(r) for r in reviews]
        # A baked record on any row with the same audio counts, not only on the
        # library copy (CodeRabbit, review of #262).
        for c in (lib, *copies):
            if not c.get("lufs_baked_at") and c.get("audio_hash"):
                rec = conn.execute(
                    "SELECT lufs_baked_at FROM archive WHERE audio_hash=? AND lufs_baked_at != '' "
                    "LIMIT 1",
                    (c["audio_hash"],),
                ).fetchone()
                if rec:
                    c["lufs_baked_at"] = rec[0]
        _share_baked_mark([lib, *copies])
        _share_live_take([lib, *copies])
        _share_version_label([lib, *copies])
        _share_unknown_album([lib, *copies])
        # Ties keep catalogue order -- filed first, then the older row -- as the
        # resolver's _keeper_sort_key does (CodeRabbit, review of #262).
        copies.sort(key=lambda r: (0 if r["finalized_at"] else 1, r["id"]))
        ranked = sorted(copies, key=functools.cmp_to_key(better_first))  # stable
        # A 2 s tie is not transitive: 201.9 s ties 202.1 s, but only 202.1 s
        # beats a 200.0 s master. Among copies tied on every step before
        # length, the longest goes first, as the resolver's _rank does (Codex
        # review of #226: FLAC copies now reach the length step).
        tied = [r for r in ranked if keep_key(r) == keep_key(ranked[0])]
        longest = max(tied, key=lambda r: float(r["duration"] or 0))
        if decide(longest, lib)[0] != "review":
            longest = tied[0]  # length wins nothing: the copy first in catalogue order
        ranked.remove(longest)
        ranked.insert(0, longest)
        best, rest = ranked[0], ranked[1:]
        winner, step = decide(best, lib)
        # A full tie: the copy that was in the library first wins (Grey,
        # 2026-10-29), as the resolver's _keeper_sort_key ranks it.
        if winner == "tie" and best["finalized_at"] and best["id"] < lib["id"]:
            winner, step = "review", "tie: in the library first"
        tally[f"{winner} ({step})" if step else winner] += 1
        if winner == "review":
            wins.append(Pair(rows[best["id"]], library, step))
        elif losers is not None:
            losers.append(best["id"])
        for other in rest:
            tally["beaten by a better review copy" if winner == "review" else
                  ("tie" if decide(other, lib)[0] == "tie" else "library (beaten)")] += 1  # fmt: skip
            if losers is not None:
                losers.append(other["id"])
    wins.sort(key=lambda p: p.review["id"])
    return wins, tally


def refusal(pair: Pair) -> str | None:
    """Why this pair must not be promoted, or None."""
    r, lib = pair.review, pair.library
    if not Path(r["file_path"]).is_file():
        return "review copy missing on disk"
    if not Path(lib["file_path"]).is_file():
        return "library copy missing on disk"
    if not r["audio_hash"]:
        return "review copy has no recorded audio hash to check against"
    found, err = audio_hash_safe(Path(r["file_path"]))
    if found != r["audio_hash"]:
        return f"review copy's audio is not the recording that was set aside ({err or found})"
    return None


def promote(conn: sqlite3.Connection, pairs: list[Pair], execute: bool) -> collections.Counter:
    now = datetime.now(timezone.utc)
    run_id = f"swap_promote_{now:%Y%m%dT%H%M%SZ}"
    ts = now.strftime("%Y-%m-%d %H:%M:%S")
    out: collections.Counter = collections.Counter()
    for pair in pairs:
        r, lib = pair.review, pair.library
        why = refusal(pair)
        label = f"id={r['id']} {r['artist']} - {r['title']}  (beats id={lib['id']} on {pair.step})"
        if why:
            print(f"  REFUSED {label}: {why}")
            out["refused"] += 1
            continue
        print(f"  {'PROMOTE' if execute else 'would promote'} {label}")
        out["promoted"] += 1
        if not execute:
            continue
        # An empty fact on the library copy keeps the copy's own: a download with no
        # album blanked the album of 16 put-back copies, which then read as "album
        # unknown" and lost to the same download again (2026-10-30).
        sets = ", ".join(f"{f} = COALESCE(?, {f})" for f in FACTS)
        conn.execute(
            f"UPDATE archive SET status='CATALOGUED', {sets}, canonicalized_at=NULL, "
            "finalized_at=NULL, "
            # The audio of a recorded baked copy is baked: the record must not be
            # lost when the master that held it is retired (CodeRabbit, #262).
            "lufs_baked_at=COALESCE(NULLIF(lufs_baked_at, ''), (SELECT b.lufs_baked_at "
            "FROM archive b WHERE b.audio_hash=archive.audio_hash AND b.lufs_baked_at != '' "
            "LIMIT 1)), lufs_baked_target=COALESCE(lufs_baked_target, (SELECT "
            "b.lufs_baked_target FROM archive b WHERE b.audio_hash=archive.audio_hash "
            "AND b.lufs_baked_at != '' LIMIT 1)) "
            "WHERE id=? AND status='DUPE_REVIEW'",
            (*(lib[f] if str(lib[f] or "").strip() else None for f in FACTS), r["id"]),
        )
        conn.execute(
            "INSERT INTO events (run_id, ts, event_type, file_path, old_value, new_value, stage, note) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (
                run_id, ts, EVENT, r["file_path"], str(lib["id"]), str(r["id"]), "swap",
                f"keep rule: {pair.step}; replaces id={lib['id']} {lib['file_path']}",
            ),
        )  # fmt: skip
        conn.commit()
    return out


def to_retire(conn: sqlite3.Connection, masters: Path) -> list[int]:
    """Library rows whose promoted replacement is now a finalized master."""
    ids = []
    for ev in conn.execute(f"SELECT old_value, new_value FROM events WHERE event_type='{EVENT}'"):
        lib = conn.execute(
            "SELECT id, status FROM archive WHERE id=?", (int(ev["old_value"]),)
        ).fetchone()
        new = conn.execute(
            "SELECT file_path, status, finalized_at FROM archive WHERE id=?",
            (int(ev["new_value"]),),
        ).fetchone()
        if lib is None or lib["status"] != "CATALOGUED" or new is None:
            continue
        p = Path(new["file_path"])
        if (
            new["status"] == "CATALOGUED"
            and new["finalized_at"]
            and p.is_relative_to(masters)
            and p.is_file()
        ):
            ids.append(lib["id"])
    return sorted(set(ids))


def retire(cfg, ids: list[int], execute: bool) -> int:
    if not ids:
        print("  nothing to retire: no promoted copy has been finalized yet (run Act 3)")
        return 0
    ids_file = Path(cfg.runs_root) / "LOGS" / f"swap_retire_{datetime.now():%Y%m%d_%H%M%S}.txt"
    ids_file.parent.mkdir(parents=True, exist_ok=True)
    ids_file.write_text("".join(f"{i}\n" for i in ids))
    cmd = [
        sys.executable, str(DELETE_TOOL), str(ids_file),
        "--reason", "Grey 2026-10-03 keep rule: replaced by a better copy (swap_review_copies)",
    ]  # fmt: skip
    if execute:
        cmd.append("--execute")
    print(f"  {len(ids)} replaced master(s) -> {DELETE_TOOL.name} ({ids_file})")
    from musaeus.masters_lock import handoff

    fds, env = handoff()
    return subprocess.run(cmd, check=False, pass_fds=fds, env=env).returncode


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    step = ap.add_mutually_exclusive_group(required=True)
    step.add_argument(
        "--promote", action="store_true", help="return winning review copies to Act 3"
    )
    step.add_argument(
        "--retire", action="store_true", help="delete masters a promoted copy has replaced"
    )
    step.add_argument(
        "--write-losers",
        metavar="FILE",
        help="write the review rows the rule says to DELETE (library wins or ties), one id "
        "per line, for scripts/delete_reviewed_tracks.py; reads only",
    )
    ap.add_argument("--execute", action="store_true", help="act (default: dry run)")
    ap.add_argument(
        "--limit",
        type=int,
        default=0,
        metavar="N",
        help="--promote at most N (a one-song trial before the rest: --limit 1)",
    )
    args = ap.parse_args()
    # The masters lock (review of #87, findings 9 and 10). For --retire it is
    # held from choosing the ids to deleting them and handed to the delete
    # tool (masters_lock.handoff): the child used to ask for it afresh and be
    # refused by its own parent (exit 75), so nothing was ever retired (Codex
    # audit 2026-10-16, F9) -- and let go in between, another job could change
    # the choice before the deletion (review of #202).
    if args.execute:
        from musaeus.masters_lock import hold_for_process

        hold_for_process(exclusive=True, what="scripts/swap_review_copies.py")

    cfg = MusicConfig.from_env()
    conn = sqlite3.connect(cfg.db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout = 60000")
    if args.write_losers:
        losers: list[int] = []
        _, tally = review_wins(conn, losers)
        Path(args.write_losers).write_text("".join(f"{i}\n" for i in losers))
        for k, v in tally.most_common():
            print(f"  {v:6,}  {k}")
        print(f"\n{len(losers):,} review row(s) to delete -> {args.write_losers}")
        return 0
    if args.promote:
        wins, tally = review_wins(conn)
        for k, v in tally.most_common():
            print(f"  {v:6,}  {k}")
        out = promote(conn, wins[: args.limit] if args.limit > 0 else wins, args.execute)
        verb = "PROMOTED" if args.execute else "DRY RUN: would promote"
        print(f"\n{verb} {out['promoted']:,}; refused {out['refused']:,}")
        if args.execute and out["promoted"]:
            print("Next: musaeus run (Act 1 decodes, Act 3 files), then --retire")
        return 0
    return retire(cfg, to_retire(conn, Path(cfg.alac_archive)), args.execute)


if __name__ == "__main__":
    sys.exit(main())
