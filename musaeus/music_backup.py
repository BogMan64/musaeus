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

A second run on the same day builds its own .partial beside the day's verified
copy, and the verified copy is set aside only once the new one has passed its
check (Codex audit 2026-10-16, F7: it used to be taken back under .partial
first, so a failed retry left the day with no counted copy at all). The
.partial is the run's own, so rsync --delete inside it is safe -- without it, a
master renamed since the last attempt left its old name behind, the counts never
agreed again and every retry that day failed.
"""

from __future__ import annotations

import fcntl
import os
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
#: The day's previous verified copy, for the moment between setting it aside
#: and naming the new one. Never counted; put back or removed by the next run.
SUPERSEDED = ".superseded"
#: A new copy with fewer files than this share of the newest dated copy is
#: not trusted to replace or rotate anything without --allow-shrink: masters
#: that were moved or emptied would otherwise "verify" against themselves
#: (review of #200, finding 1 -- on 2026-10-13 every master WAS moved away).
SHRINK_FLOOR = 0.9


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


def _recover(root: Path) -> None:
    """Finish any swap a crash cut short (see _replace), whatever its day --
    a run on another day would otherwise leave it for ever (review of #200,
    finding 3)."""
    for aside in sorted(root.glob(f"{PREFIX}*{SUPERSEDED}")):
        final = aside.with_name(aside.name[: -len(SUPERSEDED)])
        if not (aside.is_dir() and _NAME_RE.match(final.name)):
            continue
        if final.is_dir():
            shutil.rmtree(aside)  # the new copy was named: the old one was on its way out
        else:
            aside.rename(final)  # the new one never was: the verified old one is the copy


def _replace(work: Path, final: Path) -> None:
    """Name the verified *work* copy *final*, setting aside the day's earlier
    copy only now. A directory cannot be renamed over another, so for a moment
    the old copy is under SUPERSEDED; _recover finishes the job if a crash
    lands there."""
    aside = final.with_name(final.name + SUPERSEDED)
    if final.is_dir():
        final.rename(aside)
    work.rename(final)
    if aside.is_dir():
        shutil.rmtree(aside)


def make_copy(vault: Path, root: Path, today: str) -> tuple[Path, list[str]]:
    """Copy the masters and the vault's state into <root>/<PREFIX><today>.partial.

    The day's verified copy, if there is one, is left alone and linked against
    (F7); the .partial is this run's own, so files that have left the masters
    are deleted from it.
    """
    _recover(root)
    dest = root / f"{PREFIX}{today}{PARTIAL}"
    previous = next(iter(dated_copies(root)), None)
    (dest / "vault_state").mkdir(parents=True, exist_ok=True)
    problems: list[str] = []
    link = [f"--link-dest={previous / 'ALAC-Archival'}"] if previous else []
    r = _rsync(
        "--delete",
        *link,
        f"{vault / 'Libraries' / 'ALAC-Archival'}/",
        f"{dest / 'ALAC-Archival'}/",
    )
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
    r = _rsync(
        "--delete", str(vault / "MetaData"), str(vault / "_db_backups"), f"{dest / 'vault_state'}/"
    )
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


def _files(copy: Path) -> int:
    return sum(1 for p in (copy / "ALAC-Archival").rglob("*") if p.is_file())


def run(
    vault: Path,
    root: Path,
    keep: int = KEEP,
    today: str | None = None,
    allow_shrink: bool = False,
) -> Report:
    """Make, check, and only then rotate. Never removes anything after a problem.

    One run at a time on a root: two would build in the same .partial and
    race in _replace (review of #200, finding 4). The lock is on the root
    folder itself, so no file is added to the drive."""
    fd = os.open(root, os.O_RDONLY)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return Report(root, ["another backup run is using this drive"])
        return _run(vault, root, keep, today, allow_shrink)
    finally:
        os.close(fd)


def _run(vault: Path, root: Path, keep: int, today: str | None, allow_shrink: bool) -> Report:
    today = today or date.today().strftime("%Y%m%d")
    _recover(root)  # before counting: a copy set aside is still the last copy
    # The LARGEST copy kept, not the newest: compared only with the newest,
    # masters drained a little each month (100 -> 91 -> 82) passed every run
    # and rotated the last full copy away (re-review of #200). A shrink that
    # was meant needs --allow-shrink until the big copy has rotated out.
    newest = max(dated_copies(root), key=_files, default=None)
    had = _files(newest) if newest else 0
    work, problems = make_copy(vault, root, today)
    files, more = verify_copy(vault, work)
    if files == 0:
        more.append("the masters folder holds no files: not a backup")
    elif had and files < had * SHRINK_FLOOR and not allow_shrink:
        more.append(
            f"{files} files against {had} in {newest.name if newest else 'the last copy'}: "
            f"the masters shrank by more than {1 - SHRINK_FLOOR:.0%} -- moved or emptied? "
            f"If it was meant, run again with --allow-shrink"
        )
    report = Report(work, problems + more, files=files)
    report.unverified = sorted(
        d
        for d in root.iterdir()
        if d.is_dir() and d != work and d.name.endswith((PARTIAL, SUPERSEDED))
    )
    if report.problems:
        return report  # stays .partial: an old copy is the only good one; keep them all
    (work / "BACKUP_VERIFIED_AT.txt").write_text(f"{date.today().isoformat()}\n", encoding="utf-8")
    dest = root / f"{PREFIX}{today}"
    _replace(work, dest)
    report.dest = dest
    for old in to_remove(dated_copies(root), keep):
        if old != dest and _NAME_RE.match(old.name):
            shutil.rmtree(old)
            report.removed.append(old)
    return report
