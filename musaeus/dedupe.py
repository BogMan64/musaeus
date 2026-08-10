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

from .config import LOSSLESS_CODECS

logger = logging.getLogger(__name__)


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
    path  = row.get("file_path", "?")
    ext   = row.get("ext", "?") or "?"
    size  = _human_size(row.get("size_bytes"))
    br    = row.get("bitrate")
    br_s  = f"{br} kbps" if br else "?"
    lufs  = row.get("lufs")
    lufs_s = f"{lufs:.1f} LUFS" if lufs else "no LUFS"
    title  = row.get("title") or Path(path).stem
    artist = row.get("artist") or "?"
    album  = row.get("album") or "?"
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
               a.artist, a.album, a.title, a.ext,
               a.bitrate, a.size_bytes, a.duration, a.lufs, a.codec
          FROM duplicates d
          LEFT JOIN archive a USING (file_path)
         WHERE d.group_id = ?
        """,
        (group_id,),
    ).fetchall()
    members = [dict(r) for r in rows]
    members.sort(
        key=lambda m: (
            0 if (m.get("codec") or "").lower() in LOSSLESS_CODECS else 1,
            -(m.get("bitrate") or 0),
            -(m.get("size_bytes") or 0),
        )
    )
    return members


def _set_status(conn, group_id: str, file_path: str, status: str) -> None:
    conn.execute(
        "UPDATE duplicates SET status = ? WHERE group_id = ? AND file_path = ?",
        (status, group_id, file_path),
    )
    conn.commit()


def _auto_keep_best(conn, group_id: str, members: list[dict]) -> None:
    """Auto-select the best file as KEEP, rest as ARCHIVE. See
    _get_group_members() for the actual ordering rule (lossless-first,
    then bitrate/size)."""
    if not members:
        return
    keep = members[0]   # already sorted: lossless-first, then bitrate/size DESC
    for m in members:
        if m["file_path"] == keep["file_path"]:
            _set_status(conn, group_id, m["file_path"], "keep")
        else:
            _set_status(conn, group_id, m["file_path"], "archive")


# ── Interactive review ────────────────────────────────────────────────────────

def _read_key(prompt: str) -> str:
    try:
        sys.stdout.write(prompt)
        sys.stdout.flush()
        return sys.stdin.readline().strip().lower()
    except (EOFError, KeyboardInterrupt):
        return "q"


HELP = """
  k  — keep this file
  a  — archive/discard this file
  A  — auto: keep highest quality, archive rest
  s  — skip group (leave pending)
  q  — quit

When reviewing a group, enter the index number then k/a to act on that file.
Example: "1k" keeps member 1, "2a" archives member 2.
"""


def run_dedupe_console(conn, *, auto_mode: bool = False) -> None:
    """
    Launch the interactive dedupe review session.

    auto_mode=True: no user prompts — auto-resolve all pending groups by
    keeping the highest bitrate member and archiving the rest.
    """
    pending = _get_pending_groups(conn)

    if not pending:
        print("\n  ✓  No pending duplicate groups. All resolved.")
        return

    print(f"\n  Dedupe Review — {len(pending)} group(s) pending")
    if auto_mode:
        print("  AUTO MODE: keeping highest quality, archiving rest\n")
        for group_id in pending:
            members = _get_group_members(conn, group_id)
            _auto_keep_best(conn, group_id, members)
            keep = members[0] if members else {}
            print(f"  ✓ {group_id}  KEEP: {keep.get('file_path', '?')}")
        print(f"\n  Auto-resolved {len(pending)} group(s).")
        return

    print("  Type ? for help.\n")

    resolved = 0
    skipped  = 0

    for group_idx, group_id in enumerate(pending, 1):
        members = _get_group_members(conn, group_id)
        dup_type = members[0].get("duplicate_type", "?") if members else "?"
        conf     = members[0].get("confidence", 0) if members else 0

        print(f"\n{'─'*70}")
        print(f"  Group {group_idx}/{len(pending)}  [{group_id}]  {dup_type}  confidence={conf:.0%}")

        for i, m in enumerate(members, 1):
            print(_fmt_row(i, m))

        while True:
            cmd = _read_key("\n  Action ([#]k/[#]a/A/s/q/?) > ")

            if cmd == "?":
                print(HELP)
                continue

            if cmd == "q":
                print(f"\n  Quit. Resolved={resolved} Skipped={skipped} Remaining={len(pending)-group_idx}")
                return

            if cmd == "s":
                skipped += 1
                break

            if cmd == "a" and len(members) > 1:
                # Auto mode shortcut
                _auto_keep_best(conn, group_id, members)
                resolved += 1
                print(f"  → Auto: KEEP {members[0]['file_path']}")
                break

            # Parse "[index][action]" e.g. "1k", "2a"
            if len(cmd) >= 2:
                try:
                    idx = int(cmd[:-1]) - 1
                    action = cmd[-1]
                    if 0 <= idx < len(members) and action in ("k", "a"):
                        fp = members[idx]["file_path"]
                        st = "keep" if action == "k" else "archive"
                        _set_status(conn, group_id, fp, st)
                        icon = "✓ KEEP" if st == "keep" else "✗ ARCHIVE"
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
               SUM(CASE WHEN status='keep'    THEN 1 ELSE 0 END) AS keep_count,
               SUM(CASE WHEN status='archive' THEN 1 ELSE 0 END) AS archive_count,
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
