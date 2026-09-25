"""Moving a library copy without orphaning its master.

The convention every edition relies on: a catalogue row's file is the
ALAC_Library copy, and its master sits at the same relative path under
ALAC-Archival (editions.master_path_for). Any stage that moves a library copy
to another place IN the library must move the master too, or the copy is left
with nothing lossless behind it. The old library ended with 390 such rows.

OrganizeStage does this in its own rename path; classical_composer and
various_artists_fix refile library copies and call move_with_master().
Moves OUT of the library (quarantine, review) deliberately leave the master
where it is: a damaged or doubted copy is no reason to move a good master.
"""

from __future__ import annotations

import shutil
from pathlib import Path


def mirrored_master(path: Path, alac_library: Path, alac_archive: Path) -> Path | None:
    """The master behind a library copy, or None if there is none."""
    try:
        rel = Path(path).relative_to(alac_library)
    except ValueError:
        return None
    master = Path(alac_archive) / rel
    return master if master.is_file() else None


def move_with_master(conn, cfg, src: Path, dst: Path) -> None:
    """Move src to dst and, when src is a library copy with a master and dst
    stays in the library, move the master to dst's mirrored path.

    Raises OSError with nothing left half-moved: if the master cannot follow,
    the copy is put back. The master's bit-rot baseline follows it.
    """
    lib, arch = Path(cfg.alac_library), Path(cfg.alac_archive)
    master_from = mirrored_master(src, lib, arch)
    master_to = None
    if master_from is not None:
        try:
            master_to = arch / Path(dst).relative_to(lib)
        except ValueError:
            master_from = None  # leaving the library: the master stays
        else:
            if master_to.exists():
                raise OSError(f"a master already occupies {master_to}")
    Path(dst).parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(src), str(dst))
    if master_from is None:
        return
    try:
        master_to.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(master_from), str(master_to))
    except OSError:
        shutil.move(str(dst), str(src))
        raise
    if conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='archive_tier_hashes'"
    ).fetchone():
        conn.execute(
            "UPDATE OR REPLACE archive_tier_hashes SET path = ? WHERE path = ?",
            (str(master_to), str(master_from)),
        )
