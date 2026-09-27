"""MUSAEUS — build the Lossless edition from the masters.

Every master in ALAC-Archival, baked to -18 LUFS ALAC, at the same relative
path under Libraries/ALAC_Library. `musaeus edition-build lossless`.

The rules it keeps, and why each one exists:

  rows untouched   A catalogue row points at its master for good (Grey,
                   2026-09-25). The retired build_alac_library.py pointed
                   rows at the copies it made, which is how ~1,170 baked
                   copies ended up posing as masters. This module never
                   writes to musaeus.db at all.
  a separate record  What is in the edition lives in edition_ledger
                   (db_history_dir/editions.db), keyed by the master's audio
                   hash -- musaeus.db is wiped between batches.
  copies follow masters  A master that moved or was renamed moves its copy;
                   one re-tagged re-tags its copy; one that left the library
                   takes its copy with it. Only a new master, or a copy that
                   is missing, is baked.
  never delete a stranger  A file in the edition folder with no record is
                   reported and left alone. Only recorded copies are removed.
  interruptible    Each copy is baked to a temporary name, verified, tagged
                   (with a marker naming its master) and only then renamed
                   into place and recorded. A copy renamed but not recorded
                   when the build stopped is recognised by its marker next
                   time and adopted rather than baked again.

Lossy masters (51 on 2026-09-27) are left out by default: baking AAC into
ALAC makes large files of lossy audio. --lossy alac includes them.
"""

from __future__ import annotations

import fcntl
import os
import shutil
import sqlite3
import time
from collections.abc import Callable, Iterator
from concurrent.futures import Future, ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from . import edition_bake
from .edition_ledger import Copy, copies, forget, record

EDITION = "lossless"
TARGET_LUFS = float(edition_bake.TARGET_I)
TMP_SUFFIX = ".edition_tmp"
LOSSLESS_CODECS = frozenset({"alac", "flac"})

#: Measured on real masters 2026-09-26: ~7.5 s of wall clock per track for
#: the two passes, per worker.
SECONDS_PER_TRACK = 7.5
#: Head-room kept free on the drive beyond the estimate.
_SPACE_MARGIN = 1.05


def marker_for(master_hash: str) -> str:
    return f"{EDITION} {edition_bake.TARGET_I} LUFS master={master_hash}"


@dataclass(frozen=True)
class Master:
    path: Path
    audio_hash: str
    codec: str
    lufs: float | None
    lufs_tp: float | None
    size_bytes: int
    mtime_ns: int | None

    @property
    def lossless(self) -> bool:
        return self.codec.lower() in LOSSLESS_CODECS

    def may_compress(self) -> bool | None:
        """Will linear loudnorm fall back to DYNAMIC (compression)?

        True/False when the stored loudness and true peak can say; None when
        the master needs lifting but its peak was never measured. Linear
        mode applies one gain; it is refused when that gain would push the
        true peak past -1 dBTP.
        """
        if self.lufs is None:
            return None
        gain = TARGET_LUFS - self.lufs
        if gain <= 0:
            return False
        if self.lufs_tp is None:
            return None
        return self.lufs_tp + gain > float(edition_bake.TARGET_TP)


@dataclass
class Plan:
    bake: list[tuple[Master, Path]] = field(default_factory=list)
    adopt: list[tuple[Master, Path]] = field(default_factory=list)
    move: list[tuple[Master, Copy, Path]] = field(default_factory=list)
    retag: list[tuple[Master, Copy]] = field(default_factory=list)
    remove: list[Copy] = field(default_factory=list)
    up_to_date: int = 0
    lossy_left_out: list[Master] = field(default_factory=list)
    same_audio: list[Master] = field(default_factory=list)
    outside_masters: list[str] = field(default_factory=list)
    blocked: list[tuple[Master, str]] = field(default_factory=list)
    unrecorded: list[Path] = field(default_factory=list)

    @property
    def bake_bytes(self) -> int:
        return sum(m.size_bytes for m, _ in self.bake)

    def compress_counts(self) -> tuple[int, int]:
        """(will be compressed, may be compressed -- peak never measured)."""
        verdicts = [m.may_compress() for m, _ in self.bake]
        return sum(v is True for v in verdicts), sum(v is None for v in verdicts)


def load_masters(conn: sqlite3.Connection) -> list[tuple[str, sqlite3.Row]]:
    rows = conn.execute(
        """
        SELECT file_path, audio_hash, codec, lufs, lufs_tp, size_bytes
          FROM archive
         WHERE status = 'CATALOGUED'
         ORDER BY file_path
        """
    ).fetchall()
    return [(r["file_path"], r) for r in rows]


def _mtime_ns(path: Path) -> int | None:
    try:
        return path.stat().st_mtime_ns
    except OSError:
        return None


def make_plan(
    conn: sqlite3.Connection,
    ledger: sqlite3.Connection,
    masters_root: Path,
    edition_root: Path,
    *,
    include_lossy: bool = False,
) -> Plan:
    """Decide what the build does. Reads only; changes nothing."""
    plan = Plan()
    recorded = copies(ledger, EDITION)
    selected: dict[str, tuple[Master, Path]] = {}

    for file_path, r in load_masters(conn):
        path = Path(file_path)
        if not path.is_relative_to(masters_root):
            plan.outside_masters.append(file_path)
            continue
        m = Master(
            path=path,
            audio_hash=r["audio_hash"] or "",
            codec=(r["codec"] or ""),
            lufs=r["lufs"],
            lufs_tp=r["lufs_tp"],
            size_bytes=int(r["size_bytes"] or 0),
            mtime_ns=_mtime_ns(path),
        )
        if not m.audio_hash or m.mtime_ns is None:
            plan.blocked.append(
                (m, "no audio fingerprint" if not m.audio_hash else "master missing")
            )
            continue
        if not m.lossless and not include_lossy:
            plan.lossy_left_out.append(m)
            continue
        if m.audio_hash in selected:
            plan.same_audio.append(m)
            continue
        selected[m.audio_hash] = (m, edition_root / path.relative_to(masters_root))

    wanted_outputs = {str(t) for _, t in selected.values()}
    # Paths the removals and moves below will empty before any bake lands.
    # Every baked-copy swap needs this: the original is renamed into the name
    # the baked copy left, so a NEW master's target still holds the old copy,
    # which is about to be removed.
    freed = {
        c.output_path
        for h, c in recorded.items()
        if h not in selected or c.output_path != str(selected[h][1])
    }

    def taken(path: Path) -> bool:
        return path.exists() and str(path) not in freed

    for h, (m, target) in selected.items():
        c = recorded.get(h)
        if c is None:
            if taken(target):
                if edition_bake.read_marker(target) == marker_for(h):
                    plan.adopt.append((m, target))
                else:
                    plan.blocked.append((m, f"a file with no record is in the way: {target}"))
            else:
                plan.bake.append((m, target))
            continue
        out = Path(c.output_path)
        if not out.exists():
            plan.bake.append((m, target))
        elif out != target:
            if taken(target):
                plan.blocked.append((m, f"its new place is taken: {target}"))
            else:
                plan.move.append((m, c, target))
        elif c.master_mtime_ns != m.mtime_ns:
            plan.retag.append((m, c))
        else:
            plan.up_to_date += 1

    plan.remove = [c for h, c in recorded.items() if h not in selected]

    known = {c.output_path for c in recorded.values()} | wanted_outputs
    if edition_root.exists():
        for p in sorted(edition_root.rglob("*")):
            if p.is_file() and not p.name.endswith(TMP_SUFFIX) and str(p) not in known:
                plan.unrecorded.append(p)
    return plan


# ── Guards ─────────────────────────────────────────────────────────────────


def pipeline_pids() -> list[int]:
    """PIDs of a running `musaeus run`: masters can move under it."""
    found = []
    for d in Path("/proc").glob("[0-9]*"):
        try:
            argv = (d / "cmdline").read_bytes().split(b"\0")
        except OSError:
            continue
        words = [a.decode("utf-8", "replace") for a in argv if a]
        if any(Path(w).name == "musaeus" or w.endswith("musaeus.cli") for w in words) and (
            "run" in words
        ):
            found.append(int(d.name))
    return found


@contextmanager
def build_lock(lock_dir: Path) -> Iterator[None]:
    """One build at a time. fcntl.flock: the kernel frees it if we die."""
    lock_dir.mkdir(parents=True, exist_ok=True)
    fh = open(lock_dir / f"edition-build-{EDITION}.lock", "w")  # noqa: SIM115
    try:
        try:
            fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise RuntimeError("another edition build is already running") from exc
        fh.write(str(os.getpid()))
        fh.flush()
        yield
    finally:
        fh.close()


# ── Execution ──────────────────────────────────────────────────────────────


@dataclass
class Outcome:
    baked: int = 0
    adopted: int = 0
    moved: int = 0
    retagged: int = 0
    removed: int = 0
    dynamic: list[str] = field(default_factory=list)
    failed: list[tuple[str, str]] = field(default_factory=list)
    stopped: bool = False


def _now() -> str:
    return datetime.now(tz=timezone.utc).isoformat(timespec="seconds")


def _copy(m: Master, output: Path, achieved: float | None, mode: str) -> Copy:
    return Copy(
        edition=EDITION,
        master_hash=m.audio_hash,
        master_path=str(m.path),
        master_mtime_ns=m.mtime_ns,
        output_path=str(output),
        built_at=_now(),
        achieved_lufs=achieved,
        mode=mode,
    )


def _prune_empty(start: Path, root: Path) -> None:
    d = start
    while d != root and d.is_relative_to(root):
        try:
            d.rmdir()
        except OSError:
            return
        d = d.parent


def _bake_one(m: Master, target: Path) -> tuple[Path, edition_bake.BakeResult]:
    """Worker: bake, verify, tag, all under a temporary name. No database."""
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(target.name + TMP_SUFFIX)
    tmp.unlink(missing_ok=True)
    try:
        result = edition_bake.bake(m.path, tmp)
        edition_bake.copy_tags(m.path, tmp, marker_for(m.audio_hash))
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    return tmp, result


def execute(
    plan: Plan,
    ledger: sqlite3.Connection,
    edition_root: Path,
    *,
    workers: int = 2,
    limit: int | None = None,
    progress: Callable[[str], None] = print,
) -> Outcome:
    """Carry out *plan*. Removals and moves first, so space is freed before
    it is spent; then the bakes, recorded one by one as they land."""
    out = Outcome()

    for stale in edition_root.rglob(f"*{TMP_SUFFIX}") if edition_root.exists() else []:
        stale.unlink(missing_ok=True)

    for c in plan.remove:
        p = Path(c.output_path)
        if p.exists():
            if edition_bake.read_marker(p) != marker_for(c.master_hash):
                out.failed.append((c.output_path, "recorded, but the file there is not the copy"))
                forget(ledger, EDITION, c.master_hash)
                continue
            p.unlink()
            _prune_empty(p.parent, edition_root)
        forget(ledger, EDITION, c.master_hash)
        out.removed += 1

    for m, c, target in plan.move:
        old = Path(c.output_path)
        if target.exists():
            # Never let a rename overwrite: the file there is not this copy.
            out.failed.append((str(m.path), f"its new place is taken: {target}"))
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        old.rename(target)
        edition_bake.copy_tags(m.path, target, marker_for(m.audio_hash))
        record(ledger, _copy(m, target, c.achieved_lufs, c.mode))
        _prune_empty(old.parent, edition_root)
        out.moved += 1

    for m, c in plan.retag:
        edition_bake.copy_tags(m.path, Path(c.output_path), marker_for(m.audio_hash))
        record(ledger, _copy(m, Path(c.output_path), c.achieved_lufs, c.mode))
        out.retagged += 1

    for m, target in plan.adopt:
        record(ledger, _copy(m, target, None, "adopted"))
        out.adopted += 1

    todo = plan.bake[:limit] if limit is not None else plan.bake
    if not todo:
        return out

    from .idle_throttle import IdleThrottle

    started = time.monotonic()
    pool = ThreadPoolExecutor(max_workers=max(1, workers))
    futures: dict[Future, tuple[Master, Path]] = {}
    try:
        with IdleThrottle():
            for m, target in todo:
                futures[pool.submit(_bake_one, m, target)] = (m, target)
            done = 0
            for fut in _as_completed(futures):
                m, target = futures[fut]
                done += 1
                try:
                    tmp, result = fut.result()
                except edition_bake.BakeError as exc:
                    out.failed.append((str(m.path), str(exc)))
                    continue
                except Exception as exc:  # noqa: BLE001 -- one track, not the build
                    out.failed.append((str(m.path), f"{type(exc).__name__}: {exc}"))
                    continue
                if target.exists():
                    tmp.unlink(missing_ok=True)
                    out.failed.append((str(m.path), f"something appeared at {target}"))
                    continue
                tmp.rename(target)
                record(ledger, _copy(m, target, result.achieved_lufs, result.mode))
                out.baked += 1
                if result.mode == "dynamic":
                    out.dynamic.append(str(target))
                if done % 25 == 0 or done == len(todo):
                    rate = (time.monotonic() - started) / done
                    left = (len(todo) - done) * rate
                    progress(f"  {done:,}/{len(todo):,} baked  (~{left / 60:.0f} min left)")
    except KeyboardInterrupt:
        out.stopped = True
        pool.shutdown(wait=True, cancel_futures=True)
        for stale in edition_root.rglob(f"*{TMP_SUFFIX}"):
            stale.unlink(missing_ok=True)
        return out
    pool.shutdown(wait=True)
    return out


def _as_completed(futures: dict[Future, tuple[Master, Path]]) -> Iterator[Future]:
    from concurrent.futures import as_completed

    yield from as_completed(futures)


def free_bytes(path: Path) -> int:
    probe = path
    while not probe.exists():
        probe = probe.parent
    return shutil.disk_usage(probe).free


def space_needed(plan: Plan) -> int:
    return int(plan.bake_bytes * _SPACE_MARGIN)


def plan_lines(plan: Plan, *, workers: int, free: int | None = None) -> list[str]:
    """The plan in plain words, for the dry run and the console."""
    gb = plan.bake_bytes / 1_000_000_000
    hours = len(plan.bake) * SECONDS_PER_TRACK / max(1, workers) / 3600
    will, may = plan.compress_counts()
    lines = [
        f"  To bake       : {len(plan.bake):,} track(s), about {gb:.1f} GB, "
        f"about {hours:.1f} h with {workers} worker(s)",
        f"  Already there : {plan.up_to_date:,}",
    ]
    for label, n in (
        ("Adopt", len(plan.adopt)),
        ("Move/rename", len(plan.move)),
        ("Re-tag only", len(plan.retag)),
        ("Remove", len(plan.remove)),
    ):
        if n:
            lines.append(f"  {label:<14}: {n:,}")
    if will or may:
        lines.append(
            f"  Compressed    : {will:,} will be, {may:,} may be -- quieter than "
            f"{edition_bake.TARGET_I} LUFS, and lifting them would clip without it"
        )
    if plan.lossy_left_out:
        lines.append(
            f"  Lossy masters : {len(plan.lossy_left_out):,} left out "
            f"(--lossy alac bakes them into ALAC)"
        )
    if plan.same_audio:
        lines.append(f"  Same audio    : {len(plan.same_audio):,} second copy(ies), baked once")
    if plan.blocked:
        lines.append(f"  Not possible  : {len(plan.blocked):,} -- listed in the log")
    if plan.unrecorded:
        lines.append(
            f"  Unknown files : {len(plan.unrecorded):,} in the edition folder with no "
            f"record -- left alone, listed in the log"
        )
    if plan.outside_masters:
        lines.append(
            f"  Not in masters: {len(plan.outside_masters):,} catalogued row(s) outside "
            f"ALAC-Archival -- skipped"
        )
    if free is not None:
        lines.append(f"  Free space    : {free / 1_000_000_000:,.0f} GB")
    return lines
