#!/usr/bin/env python3
"""
MUSAEUS — Dedupe Review Console

Grey chooses which copies of a song to keep. Groups that share a file are
shown together as one set, every copy once, numbered, ranked by the keep rule
as the resolver ranks them.

A choice is carried out at once (Grey, 2026-10-09). When every copy in a set
has a choice, and at least one is kept, the console asks once and then moves
the archived copies to review with the resolver's own move (restore script,
masters lock); each kept copy is marked on its catalogue row, so the resolver
never moves it, wherever it is filed later. Nothing is saved for later: four
reviews running found choices saved by path misapplied once files were
refiled. Quitting or skipping part way writes nothing.

Controls:
  Nk — keep copy N        Na — archive copy N
  A  — leave this set to the keep rule (the resolver decides it at Act 2)
  s  — skip this set      q  — quit      ?  — help
"""

from __future__ import annotations

import logging
import signal
import threading
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from pathlib import Path

logger = logging.getLogger(__name__)

#: The groups still to be resolved. The resolver acts on these, and every count
#: of them uses this one condition.
ACTED_ON_SQL = "status = 'pending'"


# ── Formatting helpers ────────────────────────────────────────────────────────


def _human_size(n: int | None) -> str:
    if n is None:
        return "?"
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.0f} {unit}"
        n /= 1024  # type: ignore[assignment]
    return f"{n:.1f} TB"


def _fmt_row(idx: int, row: dict, choice: str | None = None) -> str:
    path = row.get("file_path", "?")
    ext = row.get("ext", "?") or "?"
    size = _human_size(row.get("size_bytes"))
    br = row.get("bitrate")
    br_s = f"{br} kbps" if br else "?"
    lufs = row.get("lufs")
    lufs_s = f"{lufs:.1f} LUFS" if lufs else "no LUFS"
    title = row.get("title") or Path(path).stem
    artist = row.get("artist") or "?"
    album = row.get("album") or "?"
    mark = {"k": "KEEP", "a": "ARCHIVE"}.get(choice or "", "undecided")
    return "\n".join(
        [
            f"  [{idx}] {path}",
            f"       {artist} — {album}",
            f"       {title}",
            f"       {ext.upper()}  {br_s}  {size}  {lufs_s}  [{mark}]",
        ]
    )


# ── DB helpers ────────────────────────────────────────────────────────────────


def _get_sets(conn) -> list[list[str]]:
    """The pending groups, joined where they share a file -- through rows of
    any status: a file kept in one group and still pending in another was
    shown in two sets, kept in the first and archived in the second (reviews
    of #135-#139, finding 1, and #140-#142, finding 3). The resolver's own
    join, so the console and Act 2 see the same sets (Grey's code review,
    2026-10-11, finding 4)."""
    from .stages.dupe_resolver import _connected_groups, _get_pending_groups

    return _connected_groups(conn, _get_pending_groups(conn))


def _set_members(conn, groups: Sequence[str]) -> list[dict]:
    """The copies a person chooses among: the resolver's own members, ranked
    as it ranks them, without those set aside or no longer in the catalogue."""
    from .db import SET_ASIDE_STATUSES
    from .stages.dupe_resolver import _component_members

    return [
        m
        for m in _component_members(conn, groups)
        if m.get("current_row") is not None and m.get("current_status") not in SET_ASIDE_STATUSES
    ]


#: What auto says. Auto writes nothing: the resolver applies the keep rule
#: itself to every set nobody decided (Grey, 2026-10-09: "leave it to the
#: resolver").
LEFT_TO_KEEP_RULE = "left to the keep rule: the resolver decides it at the next Act 2"

#: Carries a finished choice out: (groups, kept paths, archived paths) ->
#: (carried out, what happened in one line).
CarryOut = Callable[[Sequence[str], list[str], list[str]], tuple[bool, str]]


#: A stop pressed again this long after the first forces it: a double tap waits
#: (stopped mid-move, a carry-out lost the keep -- review of #149), a hang does not.
_FORCE_AFTER_S = 10.0


@contextmanager
def _not_interrupted() -> Iterator[None]:
    """Ctrl-C, a closed terminal or a TERM wait until the carry-out is done.
    Stopped part way, the archived copies were moved and the keep never
    recorded, and the next Act 2 could move the copy kept (review of
    #140-#142, finding 1). The stop comes once it is done."""
    if threading.current_thread() is not threading.main_thread():
        yield
        return
    sigs = (signal.SIGINT, signal.SIGTERM, signal.SIGHUP)
    old = {s: signal.getsignal(s) for s in sigs}
    asked: list[float] = []

    def wait(n: int, frame: object) -> None:
        import time

        now = time.monotonic()
        if not asked:
            print("\n  Finishing the carry-out, then stopping. Press again in 10 s to force.")
        elif now - asked[0] >= _FORCE_AFTER_S:
            raise KeyboardInterrupt  # a hung move must still be stoppable
        asked.append(now)

    for s in sigs:
        signal.signal(s, wait)
    try:
        yield
    finally:
        for s, h in old.items():
            signal.signal(s, h)
    if asked:
        raise KeyboardInterrupt


def carry_out_with(config) -> CarryOut:
    """The real carry-out: the masters lock, held only while moving (an open
    console must not block the backup or the bit-rot check), a run of its own,
    and the resolver's carry_out."""

    def carry_out(groups: Sequence[str], kept: list[str], archived: list[str]) -> tuple[bool, str]:
        from .context import RunContext
        from .db import open_db
        from .masters_lock import MastersBusy, masters_lock
        from .stages.dupe_resolver import DupeResolverStage

        try:
            with (
                _not_interrupted(),
                masters_lock(config.runs_root, exclusive=True, what="musaeus dedupe"),
            ):
                conn = open_db(config.db_path)
                try:
                    ctx = RunContext.new(config, conn, dry_run=False)
                    result = DupeResolverStage().carry_out(ctx, groups, kept, archived)
                    # A run with a RUN_END: left open, the next pipeline warned
                    # "Previous run did not complete" (finding 9).
                    ctx.finish()
                finally:
                    conn.close()
        except MastersBusy as exc:
            return False, f"nothing moved: {exc}"
        # Partly carried out still counts: the copies that moved are in review
        # and the keeps are marked, and the set is not offered again as if
        # nothing had happened (review of #149).
        carried = result.success or result.files_changed > 0
        return carried, "; ".join(result.errors + result.notes) or "done"

    return carry_out


# ── A decided sheet (Grey, 2026-10-21) ────────────────────────────────────────

#: The columns `musaeus dedupe --sheet` reads. A sheet may have more.
SHEET_COLUMNS = ("Group", "Action", "DB id", "Master file", "Audio hash")
#: A group with no REMOVE copy (a pair a person kept): nothing to do, not a failure.
NOTHING_TO_REMOVE = "nothing to remove"


def _read_sheet(path: Path) -> dict[str, list[tuple[str, int, str, str]]]:
    """Group -> [(KEEP or REMOVE, catalogue id, master path, audio hash)].
    "REMOVE?" reads as REMOVE."""
    import csv

    with open(path, encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        missing = [c for c in SHEET_COLUMNS if c not in (reader.fieldnames or [])]
        if missing:
            raise ValueError(f"the sheet has no column {', '.join(missing)}")
        groups: dict[str, list[tuple[str, int, str, str]]] = {}
        for row in reader:
            action = row["Action"].strip().upper().rstrip("?")
            if action not in ("KEEP", "REMOVE"):
                raise ValueError(
                    f"group {row['Group']}: {row['Action']!r} is neither KEEP nor REMOVE"
                )
            groups.setdefault(row["Group"], []).append(
                (action, int(row["DB id"]), row["Master file"], row["Audio hash"])
            )
    return groups


def _check_group(
    conn, rows: list[tuple[str, int, str, str]]
) -> tuple[list[str], list[str], list[tuple[str, str]], str | None]:
    """(kept paths, removed paths, every copy with its audio) for one group, or why not.

    The sheet was made earlier: each copy must still be the catalogued row at
    the path the sheet names, and a copy a person kept is never removed by it.
    """
    kept: list[str] = []
    removed: list[str] = []
    members: list[tuple[str, str]] = []
    for action, rid, path, audio in rows:
        r = conn.execute(
            "SELECT file_path, status, audio_hash, kept_by_person_at FROM archive WHERE id = ?",
            (rid,),
        ).fetchone()
        if r is None or r[1] != "CATALOGUED":
            return [], [], [], f"copy {rid} is no longer in the catalogue"
        if r[0] != path:
            return [], [], [], f"copy {rid} has moved since the sheet was made"
        # A path can take another recording (Codex review of #222).
        if not audio or r[2] != audio:
            return [], [], [], f"copy {rid} is not the recording the sheet was made from"
        if action == "REMOVE" and r[3]:
            return [], [], [], f"copy {rid} was kept by a person"
        (kept if action == "KEEP" else removed).append(path)
        members.append((path, r[2]))
    if not kept:
        return [], [], [], "no copy is kept"
    if not removed:
        return [], [], [], NOTHING_TO_REMOVE
    return kept, removed, members, None


def apply_sheet(
    config, sheet: Path, *, execute: bool = False, out: Callable[[str], object] = print
) -> int:
    """`musaeus dedupe --sheet FILE [--execute]`: carry out a decided sheet.

    Each group's REMOVE copies move to review by the resolver's own carry-out,
    as a choice made in the console does (restore script; the kept copies are
    marked kept by a person). Without --execute nothing is written. With it,
    every group is checked again under the masters lock, so no pipeline run
    meets a half-written group, and a group not carried out is closed rather
    than left pending for Act 2 to decide its own way.
    Returns 0, 1 when an approved removal was skipped or not carried out
    (Codex review of #222), or EXIT_BUSY.
    """
    from .context import RunContext
    from .db import open_db
    from .masters_lock import EXIT_BUSY, MastersBusy, masters_lock
    from .stages.dupe_resolver import DupeResolverStage

    groups = _read_sheet(sheet)
    conn = open_db(config.db_path)
    moved = failed = skipped = 0
    try:
        if not execute:
            would = 0
            for name, rows in groups.items():
                _, removed, _, why = _check_group(conn, rows)
                if why:
                    skipped += 1
                    out(f"  skip group {name}: {why}")
                else:
                    would += len(removed)
            out(
                f"DRY RUN: {would} copies in {len(groups) - skipped} groups would move to review; "
                f"{skipped} groups skipped. Nothing changed. Run again with --execute."
            )
            return 0
        try:
            with (
                _not_interrupted(),
                masters_lock(config.runs_root, exclusive=True, what="musaeus dedupe --sheet"),
            ):
                ctx = RunContext.new(config, conn, dry_run=False)
                stage = DupeResolverStage()
                for name, rows in groups.items():
                    kept, removed, members, why = _check_group(conn, rows)
                    if why:
                        skipped += why != NOTHING_TO_REMOVE
                        out(f"  skip group {name}: {why}")
                        continue
                    gid = f"sheet_{ctx.run_id}_{name}"
                    conn.executemany(
                        "INSERT INTO duplicates (group_id, file_path, duplicate_type, confidence, "
                        "run_id, audio_hash) VALUES (?, ?, 'PERSON', 1.0, ?, ?)",
                        [(gid, p, ctx.run_id, h) for p, h in members],
                    )
                    conn.commit()
                    result = stage.carry_out(ctx, [gid], kept, removed)
                    conn.execute(
                        "UPDATE duplicates SET status = 'stale' WHERE group_id = ? AND status = 'pending'",
                        (gid,),
                    )
                    conn.commit()
                    moved += result.files_changed
                    if not result.success:
                        failed += 1
                        out(f"  group {name}: {'; '.join(result.errors)}")
                ctx.finish()
        except MastersBusy as exc:
            out(f"nothing moved: {exc}")
            return EXIT_BUSY
    finally:
        conn.close()
    out(f"Moved {moved} copies to review; {skipped} groups skipped; {failed} not carried out.")
    return 1 if failed or skipped else 0


HELP = """
  Nk — keep copy N           e.g. 1k
  Na — archive copy N        e.g. 2a
  A  — leave this set to the keep rule (the resolver decides it at Act 2)
  s  — skip this set (nothing written)
  q  — quit (nothing written for an unfinished set)

When every copy has a choice and at least one is kept, you are asked once;
then the archived copies move to review (a restore script is written) and the
kept copies stay -- the resolver will never move them.
"""


def _read_key(prompt: str) -> str:
    try:
        return input(prompt).strip()
    except (EOFError, KeyboardInterrupt):
        return "q"


def run_dedupe_console(conn, *, auto_mode: bool = False, carry_out: CarryOut | None = None) -> None:
    """Review the duplicate sets. *carry_out*: how a finished choice is carried
    out (carry_out_with(config) from the CLI and the console); without it,
    choices cannot be carried out and nothing is written."""
    sets = _get_sets(conn)
    if not sets:
        print("\n  ✓  No pending duplicate groups. All resolved.")
        return
    print(f"\n  Dedupe Review — {len(sets)} set(s) pending")
    if auto_mode:
        print(f"  AUTO: {len(sets)} set(s) {LEFT_TO_KEEP_RULE}. Nothing changed here.")
        return
    print("  Type ? for help.\n")
    done = skipped = 0
    for n, groups in enumerate(sets, 1):
        members = _set_members(conn, groups)
        if len(members) < 2:
            skipped += 1
            continue  # one live copy: nothing to choose; the resolver closes it
        choices: dict[str, str] = {}
        print(f"\n{'─' * 70}")
        kind = members[0].get("duplicate_type", "?")
        print(f"  Set {n}/{len(sets)}  [{', '.join(groups)}]  {kind}")
        for i, m in enumerate(members, 1):
            print(_fmt_row(i, m))
        while True:
            cmd = _read_key("\n  Action ([#]k/[#]a/A/s/q/?) > ")
            if cmd in ("?", "q", "Q", "s", "S"):
                cmd = cmd.lower()
            if cmd == "?":
                print(HELP)
                continue
            if cmd == "q":
                left = len(sets) - n + 1
                print(f"\n  Quit. Carried out={done} Skipped={skipped} Remaining={left}")
                return
            if cmd == "s":
                skipped += 1
                break
            if cmd == "A":
                skipped += 1
                print(f"  → Auto: {LEFT_TO_KEEP_RULE}")
                break
            if cmd in ("a", "k"):
                print("  Which copy? Put its number first, e.g. 2a or 1k.")
                continue
            try:
                idx, action = int(cmd[:-1]) - 1, cmd[-1].lower()
            except (ValueError, IndexError):
                print("  ? — unknown command. Type ? for help.")
                continue
            if not (0 <= idx < len(members)) or action not in ("k", "a"):
                print("  ? — unknown command. Type ? for help.")
                continue
            choices[members[idx]["file_path"]] = action
            # Shown again, in the order first shown: the numbers stay on the
            # files (review of #129-#134, finding 1).
            for i, m in enumerate(members, 1):
                print(_fmt_row(i, m, choices.get(m["file_path"])))
            if len(choices) < len(members):
                continue
            kept = [p for p, c in choices.items() if c == "k"]
            archived = [p for p, c in choices.items() if c == "a"]
            if not kept:
                print("  Keep at least one copy: every copy archived would leave the song out.")
                choices.clear()
                continue
            if carry_out is None:
                print("  Choices cannot be carried out here; nothing written.")
                return
            answer = _read_key(f"  Move {len(archived)} to review and keep {len(kept)}? [y/n] > ")
            if answer.lower() != "y":
                print("  Not carried out. Choose again, or s to skip.")
                choices.clear()
                continue
            ok, said = carry_out(groups, kept, archived)
            print(f"  → {said}")
            if not ok:
                # Not counted as carried out (finding 10): choose again, or skip.
                print("  Not carried out. Choose again, or s to skip.")
                choices.clear()
                continue
            done += 1
            break
    print(f"\n  Session complete. Carried out={done} Skipped={skipped}")


# ── Report ────────────────────────────────────────────────────────────────────


def print_dedupe_report(conn) -> None:
    """Print a summary of all duplicate groups and their resolution status."""
    rows = conn.execute(
        """
        SELECT group_id,
               COUNT(*) as total,
               SUM(CASE WHEN status = 'keep' THEN 1 ELSE 0 END) AS keep_count,
               SUM(CASE WHEN status = 'archive' THEN 1 ELSE 0 END) AS archive_count,
               SUM(CASE WHEN status = 'pending' THEN 1 ELSE 0 END) AS pending_count,
               MAX(duplicate_type) AS dup_type
          FROM duplicates
         GROUP BY group_id
         ORDER BY pending_count DESC, group_id
        """
    ).fetchall()

    if not rows:
        print("  No duplicate groups found.")
        return

    print(f"\n  Duplicate Groups ({len(rows)} total)")
    print(f"  {'Group':<36} {'Type':<16} {'Total':>5} {'Keep':>5} {'Archive':>7} {'Pending':>7}")
    print("  " + "─" * 78)
    for r in rows:
        print(
            f"  {r['group_id']:<36} {(r['dup_type'] or '?'):<16} "
            f"{r['total']:>5} {r['keep_count']:>5} {r['archive_count']:>7} {r['pending_count']:>7}"
        )
    print()
