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
    of #135-#139, finding 1, and #140-#142, finding 3)."""
    from .stages.dupe_resolver import _get_pending_groups

    groups = _get_pending_groups(conn)
    parent = {g: g for g in groups}

    def find(g: str) -> str:
        while parent[g] != g:
            parent[g] = parent[parent[g]]
            g = parent[g]
        return g

    if groups:
        qs = ",".join("?" * len(groups))
        first: dict[str, str] = {}
        for gid, path in conn.execute(
            f"SELECT group_id, file_path FROM duplicates WHERE group_id IN ({qs})", groups
        ):
            if path in first:
                parent[find(gid)] = find(first[path])
            else:
                first[path] = gid
    sets: dict[str, list[str]] = {}
    for g in groups:
        sets.setdefault(find(g), []).append(g)
    return [sorted(v) for v in sets.values()]


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
    asked: list[int] = []
    for s in sigs:
        signal.signal(s, lambda n, f: asked.append(n))
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
        return result.success, "; ".join(result.errors + result.notes) or "done"

    return carry_out


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
