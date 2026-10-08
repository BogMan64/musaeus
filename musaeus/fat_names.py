"""Names a FAT32 (or exFAT) stick cannot hold as they are on the vault's ext4.

Grey, 2026-10-03: the car stick is FAT32, and every library name must be
safe on it. FAT32 refuses \\ : * ? " < > | in a name, drops a trailing dot or
space, and ignores letter case. ext4 does none of that, so a library can look
fine here and still lose a song on the stick: two files whose paths differ
only by case land on ONE name there, and the second copy overwrites the first.

Forbidden characters are already removed wherever a library path is built
(organize.sanitize_path_component). Letter case is not, and on 2026-10-03 the
masters held 26 folder pairs differing only by case ("The Dark Side Of The
Moon" / "The Dark Side of the Moon"). On a stick those merge into one folder
harmlessly. Two FILES on one name do not.

One rule, used by the USB transfer (which refuses to copy) and by doctor
(which reports). str.casefold() is stricter than FAT's own upcase table, so
both can only over-report a clash, never miss one.
"""

from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path

#: Refused in a FAT32 or exFAT name. "/" is the separator, so never in a part.
FORBIDDEN = frozenset('\\:*?"<>|')


def _bad_part(part: str) -> bool:
    return part.endswith((".", " ")) or any(ch in FORBIDDEN or ord(ch) < 32 for ch in part)


def names_unsafe_on_fat(
    files: Iterable[Path], source_root: Path
) -> tuple[list[Path], list[list[Path]]]:
    """Pure (no I/O): the files a FAT32 stick cannot hold as named.

    Returns (bad, clashes):
      bad      a file whose path has a character FAT refuses, a control
               character, or a part ending in a dot or space
      clashes  groups of files whose paths differ only by letter case

    Folders differing only by case are not a clash here: nothing is lost
    unless two FILES then share a name, which the clash test catches.
    """
    bad: list[Path] = []
    folded: dict[str, list[Path]] = {}
    for f in files:
        rel = f.relative_to(source_root)
        if any(_bad_part(part) for part in rel.parts):
            bad.append(f)
        folded.setdefault(str(rel).casefold(), []).append(f)
    return bad, [sorted(group) for group in folded.values() if len(group) > 1]


def folders_differing_only_by_case(dirs: Iterable[Path], source_root: Path) -> list[list[Path]]:
    """Pure: groups of folders whose paths differ only by letter case.

    Harmless on a stick (they merge), but each is one album or artist shown
    as two in a folder-browsed library, and the place a file clash comes from.
    """
    folded: dict[str, list[Path]] = {}
    for d in dirs:
        folded.setdefault(str(d.relative_to(source_root)).casefold(), []).append(d)
    return [sorted(group) for group in folded.values() if len(group) > 1]
