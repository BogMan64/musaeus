#!/usr/bin/env python3
"""
MUSAEUS — Dedupe Review Console

Interactive terminal UI for resolving duplicate groups detected by the Scholar stage.

Controls:
  k  — keep this file (mark as KEEP)
  a  — archive/discard this file (mark as ARCHIVE)
  s  — skip group (leave pending)
  q  — quit and save progress
  ?  — show this help

Each group shows all members with their metadata so you can pick the keeper.
Decisions are written to the duplicates table immediately (no undo in-session,
but the event log records everything).
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path

logger = logging.getLogger(__name__)

#: A person's decisions, told apart from the resolver's own bookkeeping
#: ('keep', 'archive' = already moved). Written as plain 'keep'/'archive' they
#: were read as the resolver's: an archived copy was never moved, and a group a
#: person decided whole was skipped (review of #86, finding 9). The resolver
#: carries these out -- the kept copy is the keeper, archived ones move.
KEEP_USER = "keep_user"
ARCHIVE_USER = "archive_user"


# ── Formatting helpers ────────────────────────────────────────────────────────


def _human_size(n: int | None) -> str:
    if n is None:
        return "?"
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.0f} {unit}"
        n /= 1024  # type: ignore[assignment]
    return f"{n:.1f} TB"


def _fmt_row(idx: int, row: dict) -> str:
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
    status = row.get("dup_status", "pending")

    lines = [
        f"  [{idx}] {path}",
        f"       {artist} — {album}",
        f"       {title}",
        f"       {ext.upper()}  {br_s}  {size}  {lufs_s}  [{status}]",
    ]
    return "\n".join(lines)


# ── DB helpers ────────────────────────────────────────────────────────────────


def _get_pending_groups(conn) -> list[str]:
    """Return group_ids with at least one 'pending' member, ordered."""
    rows = conn.execute(
        """
        SELECT DISTINCT group_id
          FROM duplicates
         WHERE status = 'pending'
         ORDER BY group_id
        """
    ).fetchall()
    return [r[0] for r in rows]


def _get_group_members(conn, group_id: str) -> list[dict]:
    """
    Return archive info for every member of a duplicate group, ordered
    so the best keeper candidate is first: real lossless codec beats
    lossy UNCONDITIONALLY (a bitrate/size comparison across different
    codecs isn't a fair quality comparison), then bitrate/size as a
    tiebreak among files that are equally lossless or equally lossy.
    """
    rows = conn.execute(
        """
        SELECT d.file_path,
               d.duplicate_type,
               d.confidence,
               d.status AS dup_status,
               a.id AS current_row, a.status AS current_status, a.finalized_at,
               a.artist, a.album, a.title, a.ext,
               a.bitrate, a.size_bytes, a.duration, a.lufs, a.codec,
               a.sample_rate, a.audio_hash
          FROM duplicates d
          LEFT JOIN archive a USING (file_path)
         WHERE d.group_id = ?
        """,
        (group_id,),
    ).fetchall()
    members = [dict(r) for r in rows]
    # Grey's keep rule, ranked exactly as the resolver ranks: this console's
    # own order (lossless first, then bitrate and size) kept a bigger live
    # copy over the studio one (review of #86, finding 9).
    from .stages.dupe_resolver import _rank, _share_loudness

    _share_loudness(members)
    _rank(members)
    return members


def _set_status(conn, group_id: str, file_path: str, status: str) -> None:
    conn.execute(
        "UPDATE duplicates SET status = ? WHERE group_id = ? AND file_path = ?",
        (status, group_id, file_path),
    )
    conn.commit()


#: What auto says. Auto writes nothing: the resolver obeys a person's keep and
#: archive, and auto wrote them from this console's own ranking, which lacked
#: what the resolver ranks by -- it could keep a new arrival and move the filed
#: master (review of #123). The resolver applies the keep rule itself to every
#: group nobody decided (Grey, 2026-10-09: "leave it to the resolver").
LEFT_TO_KEEP_RULE = "left to the keep rule: the resolver decides it at the next Act 2"


# ── Interactive review ────────────────────────────────────────────────────────


def _read_key(prompt: str) -> str:
    try:
        sys.stdout.write(prompt)
        sys.stdout.flush()
        # Not lowercased: "A" (auto) and "a" (archive) are different keys,
        # and lowercasing made "a" resolve the whole group (review of #86, 9).
        return sys.stdin.readline().strip()
    except (EOFError, KeyboardInterrupt):
        return "q"


HELP = """
  k  — keep this file
  a  — archive/discard this file
  A  — auto: leave this group to the keep rule (the resolver decides it)
  s  — skip group (leave pending)
  q  — quit

When reviewing a group, enter the index number then k/a to act on that file.
Example: "1k" keeps member 1, "2a" archives member 2.
"""


def run_dedupe_console(conn, *, auto_mode: bool = False) -> None:
    """
    Launch the interactive dedupe review session.

    auto_mode=True: no user prompts, and nothing written: every pending group
    is left to the keep rule, which the resolver applies at the next Act 2.
    """
    pending = _get_pending_groups(conn)

    if not pending:
        print("\n  ✓  No pending duplicate groups. All resolved.")
        return

    print(f"\n  Dedupe Review — {len(pending)} group(s) pending")
    if auto_mode:
        print(f"  AUTO: {len(pending)} group(s) {LEFT_TO_KEEP_RULE}. Nothing changed here.")
        return

    print("  Type ? for help.\n")

    resolved = 0
    skipped = 0

    for group_idx, group_id in enumerate(pending, 1):
        members = _get_group_members(conn, group_id)
        dup_type = members[0].get("duplicate_type", "?") if members else "?"
        conf = members[0].get("confidence", 0) if members else 0

        print(f"\n{'─' * 70}")
        print(
            f"  Group {group_idx}/{len(pending)}  [{group_id}]  {dup_type}  confidence={conf:.0%}"
        )

        for i, m in enumerate(members, 1):
            print(_fmt_row(i, m))

        while True:
            cmd = _read_key("\n  Action ([#]k/[#]a/A/s/q/?) > ")

            if cmd.lower() in ("?", "q", "s"):
                cmd = cmd.lower()

            if cmd == "?":
                print(HELP)
                continue

            if cmd == "q":
                print(
                    f"\n  Quit. Resolved={resolved} Skipped={skipped} Remaining={len(pending) - group_idx}"
                )
                return

            if cmd == "s":
                skipped += 1
                break

            if cmd in ("a", "k"):
                print("  Which file? Put its number first, e.g. 2a or 1k.")
                continue

            if cmd == "A":
                skipped += 1
                print(f"  → Auto: {LEFT_TO_KEEP_RULE}")
                break

            # Parse "[index][action]" e.g. "1k", "2a"
            if len(cmd) >= 2:
                try:
                    idx = int(cmd[:-1]) - 1
                    action = cmd[-1].lower()
                    if 0 <= idx < len(members) and action in ("k", "a"):
                        fp = members[idx]["file_path"]
                        st = KEEP_USER if action == "k" else ARCHIVE_USER
                        _set_status(conn, group_id, fp, st)
                        icon = "✓ KEEP" if st == KEEP_USER else "✗ ARCHIVE"
                        print(f"  → {icon}: {fp}")
                        # Refresh members
                        members = _get_group_members(conn, group_id)
                        # Check if all resolved
                        if all(m["dup_status"] != "pending" for m in members):
                            resolved += 1
                            break
                        continue
                except (ValueError, IndexError):
                    pass

            print("  ? — unknown command. Type ? for help.")

    print(f"\n  Session complete. Resolved={resolved} Skipped={skipped}")


# ── Report ────────────────────────────────────────────────────────────────────


def print_dedupe_report(conn) -> None:
    """Print a summary of all duplicate groups and their resolution status."""
    rows = conn.execute(
        """
        SELECT group_id,
               COUNT(*) as total,
               SUM(CASE WHEN status IN ('keep', 'keep_user') THEN 1 ELSE 0 END) AS keep_count,
               SUM(CASE WHEN status IN ('archive', 'archive_user') THEN 1 ELSE 0 END) AS archive_count,
               SUM(CASE WHEN status='pending' THEN 1 ELSE 0 END) AS pending_count,
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
