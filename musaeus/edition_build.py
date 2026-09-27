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
                   one re-tagged re-tags its copy. Only a new master, or a
                   copy that is missing, is baked.
  a copy goes only with its master  A copy is removed when its master has
                   positively left: set aside (in review, quarantined...) or
                   gone from disk and from the catalogue. NOT merely because
                   this build did not select it -- after a catalogue reset
                   that was every copy (cloud review of #49).
  trust the file, not the record  A recorded copy is moved, re-tagged or
                   removed only when the file carries the marker naming its
                   master. A record whose file is something else is stale.
  never delete a stranger  A file in the edition folder with no record is
                   reported and left alone.
  interruptible    Each copy is baked to a temporary name, verified, tagged
                   (with a marker naming its master) and only then renamed
                   into place and recorded. A copy renamed but not recorded
                   when the build stopped is recognised by its marker next
                   time and adopted rather than baked again.
  damaged masters are not baked  A master that does not decode cleanly
                   would make a copy that does, hiding the damage.

Lossy masters are left out by default (Grey, 2026-09-27): baking AAC into
ALAC makes large files of lossy audio. --lossy alac includes them.
"""

from __future__ import annotations

import fcntl
import os
import shutil
import sqlite3
import time
from collections.abc import Callable, Iterable, Iterator
from concurrent.futures import Future, ThreadPoolExecutor, as_completed
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from . import edition_bake
from .config import LOSSLESS_CODECS
from .db import SET_ASIDE_STATUSES
from .edition_ledger import Copy, copies, forget, record

EDITION = "lossless"
TARGET_LUFS = float(edition_bake.TARGET_I)
TMP_SUFFIX = ".edition_tmp"

#: Worker-seconds of work per second of audio, scaled to 44.1 kHz -- the
#: decode check plus both loudnorm passes. Measured 2026-09-27 on 29 real
#: masters: 0.025 without the decode check, 0.077 with it on 192 kHz
#: masters (847 of the library's are). A flat per-track figure understated
#: the build by half; the estimate now follows length and sample rate.
WORK_PER_AUDIO_SECOND = 0.04
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
    decode_ok: int | None = None  # 1 checked clean, 0 checked damaged, None never checked
    seconds_44k: float = 240.0  # its length, scaled to 44.1 kHz: what a bake costs
    title: str = ""
    artist: str = ""
    album: str = ""

    @property
    def work_seconds(self) -> float:
        return self.seconds_44k * WORK_PER_AUDIO_SECOND

    @property
    def lossless(self) -> bool:
        return self.codec.lower() in LOSSLESS_CODECS

    def may_compress(self) -> bool | None:
        """True when its peaks force DYNAMIC mode; None when that cannot be told.

        Linear loudnorm applies one gain and is refused when that gain would
        push the true peak past -1 dBTP -- this can be judged from the stored
        loudness and peak. It is ALSO refused when the track's loudness range
        (LRA) exceeds the target's, and no LRA is stored (cloud review of
        #49: a loud, wide-range master went dynamic while this said False).
        So True is a certainty and nothing is ever promised linear.
        """
        if self.lufs is None or self.lufs_tp is None:
            return None
        gain = TARGET_LUFS - self.lufs
        if self.lufs_tp + gain > float(edition_bake.TARGET_TP):
            return True
        return None


@dataclass
class Plan:
    bake: list[tuple[Master, Path]] = field(default_factory=list)
    adopt: list[tuple[Master, Path]] = field(default_factory=list)
    move: list[tuple[Master, Copy, Path]] = field(default_factory=list)
    retag: list[tuple[Master, Copy]] = field(default_factory=list)
    remove: list[Copy] = field(default_factory=list)
    forget: list[Copy] = field(default_factory=list)
    up_to_date: int = 0
    kept_unselected: int = 0
    lossy_left_out: list[Master] = field(default_factory=list)
    same_audio: list[Master] = field(default_factory=list)
    outside_masters: list[str] = field(default_factory=list)
    blocked: list[tuple[Master, str]] = field(default_factory=list)
    unrecorded: list[Path] = field(default_factory=list)

    @property
    def bake_bytes(self) -> int:
        return sum(m.size_bytes for m, _ in self.bake)

    def hours(self, workers: int) -> float:
        return sum(m.work_seconds for m, _ in self.bake) / max(1, workers) / 3600

    def compress_count(self) -> int:
        """Bakes certain to be compressed, from their peaks. A lower bound."""
        return sum(m.may_compress() is True for m, _ in self.bake)


def _columns(conn: sqlite3.Connection) -> set[str]:
    return {r[1] for r in conn.execute("PRAGMA table_info(archive)")}


def load_rows(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """Every catalogue row, whatever its status: the removal rule needs to
    know which masters were set aside, not only which are live."""
    have = _columns(conn)
    decode = "decode_ok" if "decode_ok" in have else "NULL AS decode_ok"
    return conn.execute(
        f"""
        SELECT file_path, audio_hash, codec, lufs, lufs_tp, size_bytes, status, {decode},
               duration, sample_rate, title, artist, album
          FROM archive
         ORDER BY file_path
        """
    ).fetchall()


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
    live_hashes: set[str] = set()
    aside_hashes: set[str] = set()
    live_at: dict[str, str] = {}  # path -> the audio a live row says is there

    for r in load_rows(conn):
        h = r["audio_hash"] or ""
        if r["status"] == "CATALOGUED" and h:
            live_at[r["file_path"]] = h
        if r["status"] in SET_ASIDE_STATUSES:
            if h:
                aside_hashes.add(h)
            continue
        if r["status"] != "CATALOGUED":
            continue
        if h:
            live_hashes.add(h)
        path = Path(r["file_path"])
        if not path.is_relative_to(masters_root):
            plan.outside_masters.append(r["file_path"])
            continue
        mtime = _mtime_ns(path)
        codec = r["codec"] or (edition_bake.codec_of(path) if mtime is not None else "")
        m = Master(
            path=path,
            audio_hash=h,
            codec=codec,
            lufs=r["lufs"],
            lufs_tp=r["lufs_tp"],
            size_bytes=int(r["size_bytes"] or 0),
            mtime_ns=mtime,
            decode_ok=r["decode_ok"],
            seconds_44k=float(r["duration"] or 240.0) * (int(r["sample_rate"] or 44_100) / 44_100),
            title=r["title"] or "",
            artist=r["artist"] or "",
            album=r["album"] or "",
        )
        if not h:
            plan.blocked.append((m, "no audio fingerprint"))
        elif mtime is None:
            plan.blocked.append((m, "master missing"))
        elif m.decode_ok == 0:
            plan.blocked.append((m, "master fails to decode -- not baked"))
        elif not m.lossless and not include_lossy:
            plan.lossy_left_out.append(m)
        elif h in selected:
            plan.same_audio.append(m)
        else:
            selected[h] = (m, edition_root / path.relative_to(masters_root))

    marker_cache: dict[str, str | None] = {}

    def marker(p: Path) -> str | None:
        key = str(p)
        if key not in marker_cache:
            marker_cache[key] = edition_bake.read_marker(p)
        return marker_cache[key]

    def ours(p: Path, h: str) -> bool:
        return p.exists() and marker(p) == marker_for(h)

    # Which records can be trusted: the file they name carries their marker.
    # Read only where a decision depends on it -- a copy at its own path,
    # with its master unchanged, cannot be holding another master's audio
    # (two masters cannot share a path), and opening every copy's tags on
    # every plan is thousands of reads for nothing.
    trust_cache: dict[str, bool] = {}

    def trust(h: str) -> bool:
        if h not in trust_cache:
            trust_cache[h] = ours(Path(recorded[h].output_path), h)
        return trust_cache[h]

    for h, rec in recorded.items():
        if h in selected:
            continue
        # Gone means positively gone: set aside, or its file is not at its
        # path -- missing, or holding a recording the catalogue says is
        # another. Not in this build is not gone: after a catalogue reset
        # nothing is in the build (cloud review of #49).
        elsewhere = live_at.get(rec.master_path, h) != h
        gone = h not in live_hashes and (
            h in aside_hashes or elsewhere or not Path(rec.master_path).exists()
        )
        if gone:
            plan.remove.append(rec)
        else:
            plan.kept_unselected += 1

    # Paths that the removals and moves below empty before any bake lands.
    # Every baked-copy swap needs this: the original is renamed into the name
    # the baked copy left, so a NEW master's target still holds the old copy.
    freed = {c.output_path for c in plan.remove if trust(c.master_hash)}
    freed |= {
        recorded[h].output_path
        for h, (_, target) in selected.items()
        if h in recorded and recorded[h].output_path != str(target) and trust(h)
    }

    def taken(path: Path) -> bool:
        return path.exists() and str(path) not in freed

    for h, (m, target) in selected.items():
        c = recorded.get(h)
        if (
            c is not None
            and c.output_path == str(target)
            and c.master_mtime_ns == m.mtime_ns
            and target.exists()
        ):
            plan.up_to_date += 1
            continue
        if c is not None and trust(h):
            out = Path(c.output_path)
            if out != target:
                if taken(target) and not ours(target, h):
                    plan.blocked.append((m, f"its new place is taken: {target}"))
                else:
                    plan.move.append((m, c, target))
            else:
                plan.retag.append((m, c))
            continue
        if c is not None and Path(c.output_path).exists():
            # The file the record names is not this master's copy.
            plan.forget.append(c)
        if ours(target, h):
            plan.adopt.append((m, target))
        elif taken(target):
            plan.blocked.append((m, f"a file with no record is in the way: {target}"))
        else:
            plan.bake.append((m, target))

    known = {c.output_path for c in recorded.values()} | {str(t) for _, t in selected.values()}
    if edition_root.exists():
        for p in sorted(edition_root.rglob("*")):
            if p.is_file() and not p.name.endswith(TMP_SUFFIX) and str(p) not in known:
                plan.unrecorded.append(p)
    return plan


# ── Guards ─────────────────────────────────────────────────────────────────

#: musaeus subcommands that only read. Anything else -- a pipeline act,
#: organize, finalize, the console (which runs acts in-process) -- can move a
#: master under a build.
READ_ONLY_COMMANDS = frozenset(
    {"edition", "edition-build", "doctor", "version", "status", "runs", "report", "plan"}
)


def _musaeus_subcommand(argv: list[str]) -> str | None:
    """The subcommand of a musaeus invocation, "" for none, None if not musaeus."""
    for i, word in enumerate(argv):
        if word == "-m" and i + 1 < len(argv) and argv[i + 1] in ("musaeus", "musaeus.cli"):
            rest = argv[i + 2 :]
        elif Path(word).name == "musaeus" and i <= 1:
            rest = argv[i + 1 :]
        else:
            continue
        return next((w for w in rest if not w.startswith("-")), "")
    return None


def busy_musaeus(procs: Iterable[tuple[int, int, str, list[str]]], me: int) -> list[int]:
    """PIDs of other MUSAEUS work that can move masters under a build.

    *procs* is (pid, parent pid, process name, argv). Matched on the process
    NAME being python or musaeus, never a bare argv search, which matches the
    shell that is asking (CLAUDE.md, pgrep -f). This process and its
    ancestors -- the console that launched the build -- are not "other".
    """
    table = {pid: (ppid, name, argv) for pid, ppid, name, argv in procs}
    mine = set()
    p = me
    while p in table and p not in mine:
        mine.add(p)
        p = table[p][0]
    found = []
    for pid, (_, name, argv) in table.items():
        if pid in mine or not (name.startswith("python") or name == "musaeus"):
            continue
        sub = _musaeus_subcommand(argv)
        if sub is not None and sub not in READ_ONLY_COMMANDS:
            found.append(pid)
    return found


def _proc_table() -> Iterator[tuple[int, int, str, list[str]]]:
    for d in Path("/proc").glob("[0-9]*"):
        try:
            stat = (d / "stat").read_text()
            argv = [a.decode("utf-8", "replace") for a in (d / "cmdline").read_bytes().split(b"\0")]
        except OSError:
            continue
        name = stat[stat.index("(") + 1 : stat.rindex(")")]
        ppid = int(stat[stat.rindex(")") + 2 :].split()[1])
        yield int(d.name), ppid, name, [a for a in argv if a]


def pipeline_pids() -> list[int]:
    """Other MUSAEUS work running now that could move masters under a build."""
    return busy_musaeus(_proc_table(), os.getpid())


def describe_work(pids: list[int]) -> str:
    """What those PIDs are, in words: "the console (pid 12)", "musaeus run (pid 9)".

    A bare `musaeus` IS the console (cli.main: command or "console").
    """
    argv_of = {pid: argv for pid, _, _, argv in _proc_table()}
    parts = []
    for pid in pids:
        sub = _musaeus_subcommand(argv_of.get(pid, [])) or "console"
        parts.append(f"{'the console' if sub == 'console' else 'musaeus ' + sub} (pid {pid})")
    return ", ".join(parts)


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
    wanted: int = 0  # compressed masters newly put on the wanted list


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
    """Worker: decode-check, bake, verify, tag, under a temporary name. No database."""
    if m.decode_ok != 1:
        problem = edition_bake.decode_problem(m.path)
        if problem:
            raise edition_bake.BakeError(f"the master does not decode cleanly: {problem}")
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


def _move_all(plan: Plan, ledger: sqlite3.Connection, edition_root: Path, out: Outcome) -> None:
    """Two steps, so a chain or a swap of copies finishes in one build: every
    moving copy first steps aside to a temporary name, then each goes to its
    target. Step by step in path order, a swap never finished."""
    staged: list[tuple[Master, Copy, Path, Path]] = []
    for m, c, target in plan.move:
        old = Path(c.output_path)
        aside = old.with_name(f"{old.name}.{m.audio_hash[:12]}{TMP_SUFFIX}")
        try:
            old.rename(aside)
        except OSError as exc:
            out.failed.append((str(m.path), f"could not move its copy: {exc}"))
            continue
        staged.append((m, c, target, aside))
    for m, c, target, aside in staged:
        old = Path(c.output_path)
        try:
            if target.exists():
                # Never rename over a file: it is not this copy.
                raise OSError(f"its new place is taken: {target}")
            target.parent.mkdir(parents=True, exist_ok=True)
            aside.rename(target)
            edition_bake.copy_tags(m.path, target, marker_for(m.audio_hash))
            record(ledger, _copy(m, target, c.achieved_lufs, c.mode))
            out.moved += 1
        except Exception as exc:  # noqa: BLE001 -- one copy, not the build
            if aside.exists() and not old.exists():
                aside.rename(old)
            out.failed.append((str(m.path), f"{type(exc).__name__}: {exc}"))
        _prune_empty(old.parent, edition_root)


def execute(
    plan: Plan,
    ledger: sqlite3.Connection,
    edition_root: Path,
    *,
    workers: int = 2,
    limit: int | None = None,
    progress: Callable[[str], None] = print,
    wanted_csv: Path | None = None,
) -> Outcome:
    """Carry out *plan*. Removals and moves first, so space is freed before
    it is spent; then the bakes, recorded one by one as they land.

    *wanted_csv*: each master whose copy had to be compressed goes on that
    wanted list (Grey, 2026-09-27: "also add them to TuneMyMusic.csv").
    """
    out = Outcome()

    for stale in edition_root.rglob(f"*{TMP_SUFFIX}") if edition_root.exists() else []:
        stale.unlink(missing_ok=True)

    for c in plan.forget:
        forget(ledger, EDITION, c.master_hash)

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

    _move_all(plan, ledger, edition_root, out)

    for m, c in plan.retag:
        try:
            edition_bake.copy_tags(m.path, Path(c.output_path), marker_for(m.audio_hash))
            record(ledger, _copy(m, Path(c.output_path), c.achieved_lufs, c.mode))
            out.retagged += 1
        except Exception as exc:  # noqa: BLE001
            out.failed.append((str(m.path), f"{type(exc).__name__}: {exc}"))

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
        with IdleThrottle() as throttle:
            edition_bake.ACTIVE_THROTTLE = throttle
            for m, target in todo:
                futures[pool.submit(_bake_one, m, target)] = (m, target)
            done = 0
            for fut in as_completed(futures):
                m, target = futures[fut]
                done += 1
                try:
                    tmp, result = fut.result()
                    if target.exists():
                        tmp.unlink(missing_ok=True)
                        raise edition_bake.BakeError(f"something appeared at {target}")
                    tmp.rename(target)
                    record(ledger, _copy(m, target, result.achieved_lufs, result.mode))
                except Exception as exc:  # noqa: BLE001 -- one track, not the build
                    reason = (
                        str(exc)
                        if isinstance(exc, edition_bake.BakeError)
                        else (f"{type(exc).__name__}: {exc}")
                    )
                    out.failed.append((str(m.path), reason))
                    continue
                out.baked += 1
                if result.mode == "dynamic":
                    out.dynamic.append(str(target))
                    if wanted_csv is not None and _want(m, wanted_csv):
                        out.wanted += 1
                if done % 25 == 0 or done == len(todo):
                    rate = (time.monotonic() - started) / done
                    left = (len(todo) - done) * rate
                    progress(f"  {done:,}/{len(todo):,} baked  (~{left / 60:.0f} min left)")
    except KeyboardInterrupt:
        out.stopped = True
    finally:
        # Always: queued bakes must not run on after the lock and the
        # throttle are released (cloud review of #49).
        pool.shutdown(wait=True, cancel_futures=True)
        edition_bake.ACTIVE_THROTTLE = None
        if out.stopped:
            for stale in edition_root.rglob(f"*{TMP_SUFFIX}"):
                stale.unlink(missing_ok=True)
    return out


def _want(m: Master, wanted_csv: Path) -> bool:
    from .stages.canonicalize import want_track

    row = {"title": m.title, "artist": m.artist, "album": m.album, "file_path": str(m.path)}
    return want_track(wanted_csv, row, "compressed to reach -18 LUFS in the Lossless edition")


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
    hours = plan.hours(workers)
    lines = [
        f"  To bake       : {len(plan.bake):,} track(s), about {gb:.1f} GB, "
        f"about {hours:.0f} h with {workers} worker(s), more while the machine is in use",
        f"  Already there : {plan.up_to_date:,}",
    ]
    for label, n in (
        ("Adopt", len(plan.adopt)),
        ("Move/rename", len(plan.move)),
        ("Re-tag only", len(plan.retag)),
        ("Remove", len(plan.remove)),
        ("Stale records", len(plan.forget)),
    ):
        if n:
            lines.append(f"  {label:<14}: {n:,}")
    if plan.bake:
        lines.append(
            f"  Compressed    : at least {plan.compress_count():,} (their peaks); tracks with a "
            f"wide dynamic range are too -- the build reports the exact number"
        )
    if plan.lossy_left_out:
        lines.append(
            f"  Lossy masters : {len(plan.lossy_left_out):,} left out "
            f"(--lossy alac bakes them into ALAC)"
        )
    if plan.same_audio:
        lines.append(f"  Same audio    : {len(plan.same_audio):,} second copy(ies), baked once")
    if plan.kept_unselected:
        lines.append(
            f"  Kept          : {plan.kept_unselected:,} copy(ies) whose master is not in "
            f"this build but has not left the library"
        )
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
