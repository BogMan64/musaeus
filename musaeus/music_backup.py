"""The masters' monthly backup to NUC8TB, on its own (Grey, 2026-10-07).

The nightly backup (backup-tiers/nuc_backup.sh) covers /home and /etc only; the
music vault on FORGE2TB was backed up by hand, and those copies quietly went a
week stale. This makes a dated copy every month, the way the hand-made ones were
laid out, so the bit-rot repair (musaeus/bitrot_repair.py) finds it:

    <root>/2.-MUSAEUS_ALAC_Archive_YYYYMMDD/ALAC-Archival/...      the masters
    <root>/2.-MUSAEUS_ALAC_Archive_YYYYMMDD/vault_state/...        catalogue, ledger, MetaData

Unchanged songs are hard-linked to the previous copy (rsync --link-dest), so a
month costs only what changed. Linked copies share disk blocks: they protect
against a damaged or deleted master on FORGE2TB, not against NUC8TB itself
failing -- that is what the USB2 copy in the drawer is for.

A new copy is made under <name>.partial and checked before anything old is
removed: rsync, comparing checksums, must find no difference from the masters,
the counts must match, and the database copies must pass integrity_check. Only
then is it renamed to its dated name, and only then are copies beyond the newest
KEEP removed -- only folders named exactly 2.-MUSAEUS_ALAC_Archive_YYYYMMDD;
nothing else on the drive is ever touched.

A copy that fails its check keeps the .partial name, so it is never counted as
one of the KEEP and never used by the bit-rot repair. Written under the final
name, it once would have been both: the next month's rotation deleted the last
good copy to keep it (review of #87, finding 3).
"""

from __future__ import annotations

import re
import shutil
import sqlite3
import subprocess
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

from .edition_ledger import LEDGER_FILENAME

PREFIX = "2.-MUSAEUS_ALAC_Archive_"
_NAME_RE = re.compile(r"^2\.-MUSAEUS_ALAC_Archive_(\d{8})$")
KEEP = 2
PARTIAL = ".partial"


@dataclass
class Report:
    dest: Path
    problems: list[str] = field(default_factory=list)
    removed: list[Path] = field(default_factory=list)
    files: int = 0
    #: Copies left under .partial by earlier failed runs: never counted, never
    #: removed by rotation, reported so a person can look.
    unverified: list[Path] = field(default_factory=list)


def _databases(vault: Path) -> tuple[tuple[str, Path], ...]:
    """The SQLite files a copy must carry consistently, by name in vault_state/.

    hash_index.db is the deny list: copied raw it could be caught mid-write and
    was never checked (review of #87, finding 13)."""
    return (
        ("musaeus.db", vault / "musaeus.db"),
        (LEDGER_FILENAME, vault / "_db_backups" / LEDGER_FILENAME),
        ("hash_index.db", vault / "_db_backups" / "hash_index.db"),
    )


def dated_copies(root: Path) -> list[Path]:
    """This root's monthly copies, newest first (by the date in the name)."""
    found = [
        d for d in root.iterdir()
        if d.is_dir() and _NAME_RE.match(d.name) and (d / "ALAC-Archival").is_dir()
    ] if root.is_dir() else []  # fmt: skip
    return sorted(found, key=lambda d: d.name, reverse=True)


def to_remove(copies: list[Path], keep: int = KEEP) -> list[Path]:
    """The copies beyond the newest *keep*."""
    return copies[keep:]


def _rsync(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["rsync", "-a", *args], capture_output=True, text=True, check=False)


def make_copy(vault: Path, root: Path, today: str) -> tuple[Path, list[str]]:
    """Copy the masters and the vault's state into <root>/<PREFIX><today>.partial.

    A second run the same day takes the day's copy back under .partial first,
    so it is never counted while it is being changed.
    """
    final = root / f"{PREFIX}{today}"
    dest = root / f"{PREFIX}{today}{PARTIAL}"
    if final.is_dir() and not dest.exists():
        final.rename(dest)
    previous = next((c for c in dated_copies(root) if c != final), None)
    (dest / "vault_state").mkdir(parents=True, exist_ok=True)
    problems: list[str] = []
    link = [f"--link-dest={previous / 'ALAC-Archival'}"] if previous else []
    r = _rsync(*link, f"{vault / 'Libraries' / 'ALAC-Archival'}/", f"{dest / 'ALAC-Archival'}/")
    if r.returncode:
        problems.append(
            f"copying the masters failed (rsync {r.returncode}): {r.stderr.strip()[:200]}"
        )
    for name, src in _databases(vault):
        if src.is_file():
            s = sqlite3.connect(f"file:{src}?mode=ro", uri=True)
            t = sqlite3.connect(dest / "vault_state" / name)
            s.backup(t)
            t.close()
            s.close()
    r = _rsync(str(vault / "MetaData"), str(vault / "_db_backups"), f"{dest / 'vault_state'}/")
    if r.returncode:
        problems.append(f"copying MetaData/_db_backups failed (rsync {r.returncode})")
    return dest, problems


def verify_copy(vault: Path, dest: Path) -> tuple[int, list[str]]:
    """(files in the copy, problems). No problems means the copy is complete."""
    problems: list[str] = []
    src = vault / "Libraries" / "ALAC-Archival"
    # --checksum: size and modification time alone passed a damaged copy that
    # kept both (review of #87, finding 13). This reads both sides in full.
    r = _rsync(
        "--dry-run", "--checksum", "--itemize-changes", f"{src}/", f"{dest / 'ALAC-Archival'}/"
    )
    left = [line for line in r.stdout.splitlines() if line.strip()]
    if r.returncode or left:
        problems.append(
            f"the copy differs from the masters ({len(left)} item(s), rsync {r.returncode})"
        )
    n_src = sum(1 for p in src.rglob("*") if p.is_file())
    n_dst = sum(1 for p in (dest / "ALAC-Archival").rglob("*") if p.is_file())
    if n_src != n_dst:
        problems.append(f"{n_dst} files in the copy, {n_src} in the masters")
    for name, src_db in _databases(vault):
        if not src_db.is_file():
            continue
        try:
            conn = sqlite3.connect(f"file:{dest / 'vault_state' / name}?mode=ro", uri=True)
            ok = conn.execute("pragma integrity_check").fetchone()[0]
        except sqlite3.Error as exc:
            ok = str(exc)
        if ok != "ok":
            problems.append(f"{name} copy failed its integrity check: {ok}")
    return n_dst, problems


def run(vault: Path, root: Path, keep: int = KEEP, today: str | None = None) -> Report:
    """Make, check, and only then rotate. Never removes anything after a problem."""
    today = today or date.today().strftime("%Y%m%d")
    work, problems = make_copy(vault, root, today)
    files, more = verify_copy(vault, work)
    report = Report(work, problems + more, files=files)
    report.unverified = sorted(
        d for d in root.iterdir() if d.is_dir() and d.name.endswith(PARTIAL) and d != work
    )
    if report.problems:
        return report  # stays .partial: an old copy is the only good one; keep them all
    (work / "BACKUP_VERIFIED_AT.txt").write_text(f"{date.today().isoformat()}\n", encoding="utf-8")
    dest = root / f"{PREFIX}{today}"
    work.rename(dest)
    report.dest = dest
    for old in to_remove(dated_copies(root), keep):
        if old != dest and _NAME_RE.match(old.name):
            shutil.rmtree(old)
            report.removed.append(old)
    return report
