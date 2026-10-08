"""Repair a rotted master from a backup copy, on its own (Grey, 2026-10-06).

Grey: "If it did find corruption, can it automatically check the USB2 or NUC8TB
for a better one and copy it over on its own, with a notification at the end of
the run saying what I did?"

A master is repaired only when it really rotted: its audio (PCM identity) no
longer matches its baseline, and MUSAEUS did not replace it on purpose. A master
MUSAEUS replaced -- the swap tool putting a better copy in place -- either has
audio the catalogue knows, or was filed after its baseline was taken; it is
never "repaired" back.

The good copy is the first backup copy (newest first) holding the same relative
path whose audio matches the baseline -- a backup that rotted too is never used.
The damaged file is set aside, not deleted, in REVIEW/BITROT_DAMAGED/<date>/.
The restored file gets the damaged file's tags when they can still be read (a
backup can be older than this week's tags), and its byte hash becomes the new
baseline.

The master's path is never empty: the backup copy is made and verified beside
it first, the damaged file is kept by a hard link, and one atomic rename puts
the good copy in place (review of #87, finding 7).
"""

from __future__ import annotations

import os
import shutil
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from .hasher import audio_hash

#: Where the backup copies live; overridable with MUSAEUS_BACKUP_ROOTS (":"-separated).
DEFAULT_BACKUP_ROOTS = ("/mnt/NUC8TB_BACKUP", "/media/grey/USB2")


@dataclass
class Repair:
    path: str
    repaired: bool
    detail: str  # the backup copy used, or why none could be


def backup_roots() -> list[Path]:
    raw = os.environ.get("MUSAEUS_BACKUP_ROOTS")
    return [Path(p) for p in (raw.split(":") if raw else DEFAULT_BACKUP_ROOTS) if p]


def backup_copies(roots: list[Path]) -> list[Path]:
    """Every backed-up ALAC-Archival folder on a mounted root, newest first.

    Dated copies (…MUSAEUS_ALAC_Archive_YYYYMMDD/ALAC-Archival) by their date; a
    plain ALAC-Archival folder at a root (USB2's first copy) after them.
    """
    dated: list[tuple[str, Path]] = []
    plain: list[Path] = []
    for root in roots:
        if not root.is_dir():
            continue  # unplugged: normal
        for d in root.glob("*MUSAEUS_ALAC_Archive_*/ALAC-Archival"):
            if d.is_dir():
                dated.append((d.parent.name.rsplit("_", 1)[-1], d))
        if (root / "ALAC-Archival").is_dir():
            plain.append(root / "ALAC-Archival")
    return [d for _, d in sorted(dated, reverse=True)] + plain


def good_copy(rel: Path, want_audio: str, copies: list[Path]) -> Path | None:
    """The first copy of *rel* whose audio is still *want_audio*."""
    for copy_root in copies:
        candidate = copy_root / rel
        if not candidate.is_file():
            continue
        try:
            if audio_hash(candidate, strict=True) == want_audio:
                return candidate
        except Exception:  # noqa: BLE001 -- an unreadable backup copy is just not a good one
            continue
    return None


def is_rot(
    conn, current_audio: str | None, *, path: str | None = None, baselined_at: str | None = None
) -> bool:
    """Rot unless MUSAEUS replaced the file on purpose.

    On purpose means either of two things. The file's current audio is a
    catalogued song's (the swap tool's better copy). Or a catalogued song was
    filed at this path after the baseline was taken: the catalogue's
    audio_hash is the audio as it ARRIVED, which a transcoded master never
    matches, so the first test alone reverted a deliberate replacement of one
    (review of #87, finding 4). Rot changes the bytes, never finalized_at.
    """
    if current_audio:
        known = conn.execute(
            "SELECT 1 FROM archive WHERE audio_hash = ? AND status = 'CATALOGUED' LIMIT 1",
            (current_audio,),
        ).fetchone()
        if known is not None:
            return False
    if path and baselined_at:
        refiled = conn.execute(
            "SELECT 1 FROM archive WHERE file_path = ? AND status = 'CATALOGUED' "
            "AND julianday(finalized_at) > julianday(?) LIMIT 1",
            (path, baselined_at),
        ).fetchone()
        if refiled is not None:
            return False
    return True


def _copy_tags(src: Path, dst: Path) -> bool:
    from mutagen.mp4 import MP4

    try:
        tags = MP4(src).tags
    except Exception:  # noqa: BLE001 -- unreadable: the backup's own tags stay
        return False
    if not tags:
        return False
    restored = MP4(dst)
    if restored.tags is None:
        restored.add_tags()
    assert restored.tags is not None
    restored.tags.clear()
    restored.tags.update(dict(tags))
    restored.save()
    return True


def _keep_aside(path: Path, aside: Path) -> Path:
    """Keep the damaged file at *aside* without moving it off *path*.

    A hard link first: it reads none of the data, so a file too damaged to read
    can still be kept. A copy if the link is refused (another filesystem).
    """
    aside.parent.mkdir(parents=True, exist_ok=True)
    n = 1
    while aside.exists():  # a second repair of the same song the same day
        aside = aside.with_name(f"{aside.stem}.{n}{aside.suffix}")
        n += 1
    try:
        os.link(path, aside)
    except OSError:
        shutil.copy2(path, aside)
    return aside


def repair(path: Path, archive_root: Path, baseline_audio: str, copies: list[Path],
           aside_root: Path) -> Repair:  # fmt: skip
    """Put a good backup copy in place of the rotted master at *path*.

    Order is the contract: copy and verify beside the master, keep the damaged
    file, then one atomic rename. A kill at any point leaves a master at *path*.
    """
    rel = path.relative_to(archive_root)
    source = good_copy(rel, baseline_audio, copies)
    if source is None:
        where = ", ".join(str(c) for c in copies) or "no backup drive mounted"
        return Repair(str(path), False, f"no backup copy with the original audio ({where})")
    day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    tmp = path.with_name(path.name + ".restoring")
    try:
        shutil.copy2(source, tmp)
        tags = _copy_tags(path, tmp)  # the damaged file's tags, while it is still in place
        if audio_hash(tmp, strict=True) != baseline_audio:  # never put a wrong file in place
            tmp.unlink(missing_ok=True)
            return Repair(
                str(path), False, f"the copy from {source} did not verify; master left as it was"
            )
        _keep_aside(path, aside_root / "BITROT_DAMAGED" / day / rel)
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)  # gone already after a successful rename
    return Repair(str(path), True, f"{source}" + ("" if tags else " (its own, older tags)"))
