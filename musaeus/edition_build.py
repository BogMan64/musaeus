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
import signal
import sqlite3
import threading
import time
from collections.abc import Callable, Iterable, Iterator, Mapping
from concurrent.futures import Future, ThreadPoolExecutor, as_completed
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from . import edition_bake, editions
from .config import AUDIO_EXTENSIONS, LOSSLESS_CODECS
from .db import SET_ASIDE_STATUSES
from .edition_ledger import (
    Copy,
    copies,
    forget,
    keep_measurement,
    measured_hashes,
    measurements_of,
    record,
)

EDITION = "lossless"
TARGET_LUFS = float(edition_bake.TARGET_I)
TMP_SUFFIX = ".edition_tmp"


def _mirror(masters_root: Path, edition_root: Path, master: Path) -> Path:
    """The Lossless edition: the master's own path under the edition root."""
    return edition_root / master.relative_to(masters_root)


def _artist_album(masters_root: Path, edition_root: Path, master: Path) -> Path:
    """The AAC editions: Artist/Album/Title.m4a (no genre level)."""
    return editions.artist_album_path(master, edition_root)


@dataclass(frozen=True)
class Kind:
    """What one edition is: its record name, loudness, layout and encode."""

    name: str
    target_i: str
    root_attr: str  # the MusicConfig attribute naming its folder
    place: Callable[[Path, Path, Path], Path]
    #: (master, temporary output, the record's kept measurements by recipe)
    bake: Callable[[Path, Path, Mapping[str, dict]], edition_bake.BakeResult]
    include_lossy: bool
    #: Put a master whose copy had to be compressed on the wanted list.
    #: Grey, 2026-09-27, for the Lossless edition only.
    list_compressed: bool = False
    #: The name people read: "Lossless", "Car", "iPhone".
    label: str = ""
    #: What its measurements' recipes start with (edition_bake).
    recipe_family: str = ""
    #: What a copy is made with (edition_bake.aac_settings), read at call
    #: time; a copy recorded with other settings is made again. None: the
    #: Lossless edition, which does not do this.
    settings: Callable[[], str] | None = None

    def root(self, config: object) -> Path:
        return Path(getattr(config, self.root_attr))


LOSSLESS_KIND = Kind(
    "lossless", edition_bake.TARGET_I, "alac_library", _mirror,
    # Looked up at call time, not bound at import: tests stand in for it.
    lambda src, tmp, known: edition_bake.bake(src, tmp, known=known),
    include_lossy=False, list_compressed=True, label="Lossless",
    recipe_family=edition_bake.LOSSLESS_RECIPE,
)  # fmt: skip
#: Grey, 2026-09-28: car and iPhone include the lossy masters (re-encoded
#: once); the car has the noise under every song.
CAR_KIND = Kind(
    "car", edition_bake.AAC_TARGET_I, "car_library", _artist_album,
    lambda src, tmp, known: edition_bake.bake_aac(src, tmp, noise=True, known=known),
    include_lossy=True, label="Car", recipe_family=edition_bake.AAC_RECIPE_FAMILY,
    settings=lambda: edition_bake.aac_settings(noise=True),
)  # fmt: skip
IPHONE_KIND = Kind(
    "iphone", edition_bake.AAC_TARGET_I, "iphone_library", _artist_album,
    lambda src, tmp, known: edition_bake.bake_aac(src, tmp, noise=False, known=known),
    include_lossy=True, label="iPhone", recipe_family=edition_bake.AAC_RECIPE_FAMILY,
    settings=lambda: edition_bake.aac_settings(noise=False),
)  # fmt: skip
KINDS = {k.name: k for k in (LOSSLESS_KIND, CAR_KIND, IPHONE_KIND)}

#: Worker-seconds of work per second of audio, scaled to 44.1 kHz -- the
#: decode check plus both loudnorm passes. Measured 2026-09-27 on 29 real
#: masters: 0.025 without the decode check, 0.077 with it on 192 kHz
#: masters (847 of the library's are). A flat per-track figure understated
#: the build by half; the estimate now follows length and sample rate.
WORK_PER_AUDIO_SECOND = 0.04
#: The car and iPhone encode, per second of audio at any rate (the rate is
#: brought down before loudnorm). Measured 2026-09-28 on 14 real masters,
#: 2 workers, Grey's i3-1315U: 0.18. Two ffmpegs at once run only 1.4x
#: faster than one on this 15 W chip, so the per-worker figure is high.
AAC_WORK_PER_AUDIO_SECOND = 0.18
#: The share of that which is the measure pass (6.3 of 11.2 s on a 44.1 kHz
#: master, 14.5 of 24.9 on a 192 kHz one, 2026-09-28): what a song whose
#: measurement is already in the record does not cost.
AAC_MEASURE_SHARE = 0.55
#: Head-room kept free on the drive beyond the estimate.
_SPACE_MARGIN = 1.05


def marker_for(master_hash: str, kind: Kind | None = None) -> str:
    """The marker naming *master_hash*'s copy in *kind* (the Lossless
    edition's form, unchanged, so its copies stay recognised)."""
    k = kind or LOSSLESS_KIND
    return f"{k.name} {k.target_i} LUFS master={master_hash}"


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
    seconds: float = 240.0  # its length
    title: str = ""
    artist: str = ""
    album: str = ""

    @property
    def work_seconds(self) -> float:
        return self.seconds_44k * WORK_PER_AUDIO_SECOND

    @property
    def lossless(self) -> bool:
        return self.codec.lower() in LOSSLESS_CODECS

    def may_compress(self, target_lufs: float = TARGET_LUFS) -> bool | None:
        """Will linear loudnorm fall back to DYNAMIC (compression)?

        Only the peaks can force it now: the range target is at ffmpeg's
        maximum (edition_bake.TARGET_LRA), so a wide range no longer does.
        Linear mode applies one gain and is refused when that gain would
        push the true peak past -1 dBTP. None when the peak was never
        measured.
        """
        if self.lufs is None or self.lufs_tp is None:
            return None
        gain = target_lufs - self.lufs
        return self.lufs_tp + gain > float(edition_bake.TARGET_TP)


@dataclass
class Plan:
    bake: list[tuple[Master, Path]] = field(default_factory=list)
    adopt: list[tuple[Master, Path]] = field(default_factory=list)
    move: list[tuple[Master, Copy, Path]] = field(default_factory=list)
    retag: list[tuple[Master, Copy]] = field(default_factory=list)
    remove: list[Copy] = field(default_factory=list)
    forget: list[Copy] = field(default_factory=list)
    rebake: list[Copy] = field(default_factory=list)  # copies baked again, in place
    resettled: int = 0  # of those, the ones made with other settings
    up_to_date: int = 0
    kept_unselected: int = 0
    #: Those copies: their masters are on disk but not in this catalogue.
    kept: list[Copy] = field(default_factory=list)
    lossy_left_out: list[Master] = field(default_factory=list)
    over_budget: list[Master] = field(default_factory=list)
    #: The masters this build could make, budget aside: what a budget may be
    #: filled from (cloud review of #53).
    makeable: set[str] = field(default_factory=set)
    #: Audio hashes whose measurement the record already keeps for this kind.
    measured: set[str] = field(default_factory=set)
    #: Masters the second pass blocked (their place taken): not makeable, and
    #: never "over budget" either -- their copy stays where it is.
    cannot_place: dict[str, Copy | None] = field(default_factory=dict)
    same_audio: list[Master] = field(default_factory=list)
    outside_masters: list[str] = field(default_factory=list)
    blocked: list[tuple[Master, str]] = field(default_factory=list)
    #: Damaged copies in the way of a bake, made again over: path -> (size,
    #: mtime_ns) when planned, so a file changed since is never replaced.
    damaged: dict[str, tuple[int, int]] = field(default_factory=dict)
    #: Copies a stopped build left mid tag save (RETAGGING): made again.
    #: Copies baked again in place: path -> (size, mtime_ns) when planned.
    replace_sig: dict[str, tuple[int, int]] = field(default_factory=dict)
    unfinished: int = 0
    unrecorded: list[Path] = field(default_factory=list)
    kind_name: str = EDITION
    target_lufs: float = TARGET_LUFS

    @property
    def bake_bytes(self) -> int:
        """What the bakes will write. A lossless copy is about its master's
        size; an AAC copy is its length at the bitrate (editions'
        estimate, container overhead included) -- 256k is a fraction of
        the master, and counting the master overstated the car by ~8x."""
        if self.kind_name == EDITION:
            return sum(m.size_bytes for m, _ in self.bake)
        # Each edition by its own format: the iPhone was sized with the car's
        # (cloud review of #53), right only while both say 256k.
        spec = editions.EDITIONS[self.kind_name]
        return sum(
            editions.estimated_bytes(
                editions.Track(str(m.path), "", "", "", "", m.seconds, m.size_bytes), spec
            )
            for m, _ in self.bake
        )

    def hours(self, workers: int) -> float:
        if self.kind_name == EDITION:
            work = sum(m.work_seconds for m, _ in self.bake)
        else:
            # A song whose measurement is kept skips the measure pass.
            work = sum(
                m.seconds
                * AAC_WORK_PER_AUDIO_SECOND
                * (1 - AAC_MEASURE_SHARE if m.audio_hash in self.measured else 1)
                for m, _ in self.bake
            )
        return work / max(1, workers) / 3600

    def compress_counts(self) -> tuple[int, int]:
        """(will be compressed, may be -- peak never measured)."""
        verdicts = [m.may_compress(self.target_lufs) for m, _ in self.bake]
        return sum(v is True for v in verdicts), sum(v is None for v in verdicts)


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
    kind: Kind | None = None,
    include_lossy: bool | None = None,
    rebake_compressed: bool = False,
    allowed: set[str] | None = None,
) -> Plan:
    """Decide what the build does. Reads only; changes nothing.

    *allowed*: the master paths a size-budgeted edition (iPhone) selected. A
    live master outside it is left out, and its copy removed -- dropped for
    space is a reason, where "not in this build" alone is not.
    """
    kind = kind or LOSSLESS_KIND
    if include_lossy is None:
        include_lossy = kind.include_lossy
    plan = Plan(kind_name=kind.name, target_lufs=float(kind.target_i))
    plan.measured = measured_hashes(ledger, kind.recipe_family)
    recorded = copies(ledger, kind.name)
    # Which master holds each place so far, and whose copy the record puts
    # there: two masters meeting at one place, the one already built keeps it.
    place_holder: dict[str, Master] = {}
    copy_at = {c.output_path: h for h, c in recorded.items()}
    selected: dict[str, tuple[Master, Path]] = {}
    over_budget_hashes: set[str] = set()
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
            seconds=float(r["duration"] or 240.0),
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
        elif h in selected or h in over_budget_hashes:
            plan.same_audio.append(m)
        else:
            target = kind.place(masters_root, edition_root, path)
            holder = place_holder.get(str(target))
            if holder is not None:
                # Two masters, one place: the AAC layout drops the genre
                # folder, so the same Artist/Album/Title under two genres meet.
                # The place is the built one's, not whichever sorts first
                # (cloud review of #53: a newcomer blocked both).
                loser = holder if copy_at.get(str(target)) == h else m
                plan.blocked.append((loser, f"another master has the same place: {target}"))
                if loser is m:
                    continue
                selected.pop(holder.audio_hash, None)
                over_budget_hashes.discard(holder.audio_hash)
                plan.over_budget = [x for x in plan.over_budget if x is not holder]
                plan.makeable.discard(str(holder.path))
            place_holder[str(target)] = m
            plan.makeable.add(str(path))
            if allowed is not None and str(path) not in allowed:
                plan.over_budget.append(m)
                over_budget_hashes.add(h)
                continue
            selected[h] = (m, target)

    marker_cache: dict[str, str | None] = {}

    def marker(p: Path) -> str | None:
        key = str(p)
        if key not in marker_cache:
            marker_cache[key] = edition_bake.read_marker(p)
        return marker_cache[key]

    def ours(p: Path, h: str) -> bool:
        return p.exists() and marker(p) == marker_for(h, kind)

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

    # Only what the budget itself left out. A master blocked for another
    # reason (it does not decode, its place is taken) keeps its copy, as it
    # does without a budget (cloud review of #53).
    dropped_for_space = {m.audio_hash for m in plan.over_budget}
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
        if h in dropped_for_space:
            gone = True  # dropped from a budgeted edition for space
        if gone:
            plan.remove.append(rec)
        else:
            plan.kept_unselected += 1
            plan.kept.append(rec)

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

    settings_now = kind.settings() if kind.settings is not None else None
    for h, (m, target) in selected.items():
        c = recorded.get(h)
        if c is not None and c.mode == RETAGGING:
            # A build stopped while this copy's tags were saved in place. The
            # save may have moved the audio and not fixed the offsets into it:
            # such a copy still reads, marker and all, and plays broken
            # (review of #127). Made again; the new copy replaces it.
            plan.unfinished += 1
            if c.output_path == str(target) and target.exists():
                plan.rebake.append(c)
                plan.replace_sig[str(target)] = _signature(target)
                plan.bake.append((m, target))
                continue
            if Path(c.output_path).exists():
                plan.remove.append(c)  # the suspect copy goes; one is made at target
            else:
                plan.forget.append(c)
            c = None
        if (
            settings_now is not None
            and c is not None
            and c.settings != settings_now
            and c.output_path == str(target)
            and target.exists()
        ):
            # Made with other settings (Grey, 2026-09-29): made again, and the
            # verified new copy replaces it (execute), as a re-bake does.
            plan.rebake.append(c)
            plan.replace_sig[str(target)] = _signature(target)
            plan.resettled += 1
            plan.bake.append((m, target))
            continue
        if (
            rebake_compressed
            and c is not None
            and c.mode == "dynamic"
            and c.output_path == str(target)
            and target.exists()
        ):
            # Baked again under today's rules; the new copy replaces this one
            # only once it is verified (execute).
            plan.rebake.append(c)
            plan.replace_sig[str(target)] = _signature(target)
            plan.bake.append((m, target))
            continue
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
                    plan.makeable.discard(str(m.path))  # the budget must not go to it
                    plan.cannot_place[str(m.path)] = c
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
        elif taken(target) and edition_bake.is_damaged(target):
            # A copy whose retag, adopt or move was killed mid-save: it reads
            # as no MP4 at all, so its marker cannot say whose it is, and it
            # blocked every build after (review of #88, finding 4). Made again;
            # the verified new copy replaces it in one rename.
            plan.damaged[str(target)] = _signature(target)
            plan.bake.append((m, target))
        elif taken(target):
            plan.blocked.append((m, f"a file with no record is in the way: {target}"))
            plan.makeable.discard(str(m.path))  # the budget must not go to it
            plan.cannot_place[str(m.path)] = c
        else:
            plan.bake.append((m, target))

    known = {c.output_path for c in recorded.values()} | {str(t) for _, t in selected.values()}
    if edition_root.exists():
        for p in sorted(edition_root.rglob("*")):
            # Audio only, as the audit counts: the playlist stage may write
            # its index into the edition (CAR_Library/Playlists), and those
            # are not strays (cloud review of #53).
            if (
                p.is_file()
                and p.suffix.lower() in AUDIO_EXTENSIONS
                and not p.name.endswith(TMP_SUFFIX)
                and str(p) not in known
            ):
                plan.unrecorded.append(p)
    return plan


def _size(path: Path) -> int:
    try:
        return path.stat().st_size
    except OSError:
        return 0


def selection(
    conn: sqlite3.Connection,
    ledger: sqlite3.Connection,
    masters_root: Path,
    edition_root: Path,
    kind: Kind,
    budget_bytes: int | None = None,
    *,
    genres: set[str] | None = None,
    artists: set[str] | None = None,
) -> editions.Selection:
    """What an edition holds, filled only from masters this build can make.

    The plan without a budget says which those are, so a blocked or
    duplicate row never takes the space. Copies kept for masters not in this
    catalogue (after the between-batches wipe) stay on the device and are
    charged first. The build, `musaeus edition` and the console all come
    here, so the preview and the build cannot disagree (cloud review of #53).
    """
    whole, sel = _whole_and_selection(
        conn, ledger, masters_root, edition_root, kind, budget_bytes, genres=genres, artists=artists
    )
    return sel


def _whole_and_selection(
    conn: sqlite3.Connection,
    ledger: sqlite3.Connection,
    masters_root: Path,
    edition_root: Path,
    kind: Kind,
    budget_bytes: int | None,
    *,
    genres: set[str] | None = None,
    artists: set[str] | None = None,
) -> tuple[Plan, editions.Selection]:
    whole = make_plan(conn, ledger, masters_root, edition_root, kind=kind)
    if budget_bytes is not None:
        # What stays on the device whatever the budget: copies kept for
        # masters not in this catalogue, and the copies of masters that
        # cannot be placed (second review of #53).
        staying = [*whole.kept, *(c for c in whole.cannot_place.values() if c)]
        budget_bytes = max(0, budget_bytes - sum(_size(Path(c.output_path)) for c in staying))
    return whole, editions.select_edition(
        conn,
        editions.EDITIONS[kind.name],
        genres=genres,
        artists=artists,
        budget_bytes=budget_bytes,
        makeable=whole.makeable,
    )


def budgeted(
    conn: sqlite3.Connection,
    ledger: sqlite3.Connection,
    masters_root: Path,
    edition_root: Path,
    kind: Kind,
    budget_bytes: int,
    *,
    rebake_compressed: bool = False,
) -> tuple[Plan, editions.Selection]:
    """The plan for a budgeted edition, and the selection that fills it."""
    whole, sel = _whole_and_selection(conn, ledger, masters_root, edition_root, kind, budget_bytes)
    # The masters that cannot be placed are let through the budget: blocked
    # again below, their copies kept -- not dropped "for space".
    allowed = {str(t.file_path) for t in sel.included} | set(whole.cannot_place)
    plan = make_plan(
        conn, ledger, masters_root, edition_root,
        kind=kind, allowed=allowed, rebake_compressed=rebake_compressed,
    )  # fmt: skip
    return plan, sel


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
def build_lock(lock_dir: Path, name: str = EDITION) -> Iterator[None]:
    """One build of an edition at a time. fcntl.flock: the kernel frees it if we die."""
    lock_dir.mkdir(parents=True, exist_ok=True)
    fh = open(lock_dir / f"edition-build-{name}.lock", "w")  # noqa: SIM115
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


def _copy(
    m: Master,
    output: Path,
    achieved: float | None,
    mode: str,
    kind: Kind,
    settings: str | None = None,
    mtime_ns: int | None = None,
) -> Copy:
    """The record of a copy. *settings*: what it was made with -- today's by
    default; a move or a retag keeps the copy's own. *mtime_ns*: the master's
    mtime to record, its own by default."""
    if settings is None:
        settings = kind.settings() if kind.settings is not None else ""
    return Copy(
        edition=kind.name,
        master_hash=m.audio_hash,
        master_path=str(m.path),
        master_mtime_ns=m.mtime_ns if mtime_ns is None else mtime_ns,
        output_path=str(output),
        built_at=_now(),
        achieved_lufs=achieved,
        mode=mode,
        settings=settings,
    )


#: The record of a copy whose tags are being saved in place (retag, adopt,
#: move), committed before the save; the save's own record replaces it. A
#: record still saying so means a build stopped mid-save (review of #127).
RETAGGING = "retagging"


def _tag_in_place(
    ledger: sqlite3.Connection,
    m: Master,
    path: Path,
    kind: Kind,
    achieved: float | None,
    mode: str,
    settings: str | None,
) -> None:
    """Give the copy at *path* the master's tags, in place, journalled.

    The save is in place: copying every file to retag it would copy whole
    editions after a mass master retag. Instead the record says RETAGGING
    until the save is done, so a copy a kill left half saved is made again.
    """
    record(ledger, _copy(m, path, achieved, RETAGGING, kind, settings, mtime_ns=0))
    edition_bake.copy_tags(m.path, path, marker_for(m.audio_hash, kind))
    record(ledger, _copy(m, path, achieved, mode, kind, settings))


def _signature(p: Path) -> tuple[int, int]:
    st = p.stat()
    return st.st_size, st.st_mtime_ns


def _prune_empty(start: Path, root: Path) -> None:
    d = start
    while d != root and d.is_relative_to(root):
        try:
            d.rmdir()
        except OSError:
            return
        d = d.parent


def _known_measurements(ledger: sqlite3.Connection, m: Master) -> dict[str, dict]:
    """The kept measurements of *m*, restored from the master when the ledger
    lacks them.

    Each master keeps its measurements in its own tag (#73), so a lost or new
    ledger need not measure everything again -- but nothing called
    restore_ledger, so a build did exactly that (review of #88, finding 12).
    """
    have = measurements_of(ledger, m.audio_hash)
    if have:
        return have
    from . import master_measurements

    try:
        if master_measurements.restore_ledger(ledger, m.audio_hash, m.path):
            return measurements_of(ledger, m.audio_hash)
    except Exception:  # noqa: BLE001, S110 -- unreadable tag: the bake measures it, as before
        pass
    return have


def _bake_one(
    m: Master, target: Path, kind: Kind, known: Mapping[str, dict]
) -> tuple[Path, edition_bake.BakeResult]:
    """Worker: decode-check, bake, verify, tag, under a temporary name. No database."""
    if m.decode_ok != 1:
        problem = edition_bake.decode_problem(m.path)
        if problem:
            raise edition_bake.BakeError(f"the master does not decode cleanly: {problem}")
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(target.name + TMP_SUFFIX)
    tmp.unlink(missing_ok=True)
    try:
        try:
            result = kind.bake(m.path, tmp, known)
        except edition_bake.BakeError as exc:
            # Only when the failure came on a KEPT measurement, and never
            # while the build is stopping (second review of #53).
            if not getattr(exc, "reused", False) or edition_bake.STOPPING.is_set():
                raise
            # A kept measurement is reused for ever, so a wrong one would fail
            # every build: once more on a fresh one, which the build then
            # keeps in its place (cloud review of #53).
            tmp.unlink(missing_ok=True)
            result = kind.bake(m.path, tmp, {})
        edition_bake.copy_tags(m.path, tmp, marker_for(m.audio_hash, kind))
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    return tmp, result


def _move_all(
    plan: Plan, ledger: sqlite3.Connection, edition_root: Path, out: Outcome, kind: Kind
) -> None:
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
            _tag_in_place(ledger, m, target, kind, c.achieved_lufs, c.mode, c.settings)
            out.moved += 1
        except Exception as exc:  # noqa: BLE001 -- one copy, not the build
            if aside.exists() and not old.exists():
                aside.rename(old)
            out.failed.append((str(m.path), f"{type(exc).__name__}: {exc}"))
        _prune_empty(old.parent, edition_root)


def _put_back_stepped_aside(stale: Path, ledger: sqlite3.Connection, kind: Kind) -> bool:
    """Put a finished copy a killed move left stepped aside back in its place.

    The move phase renames each moving copy to <name>.<hash12><TMP_SUFFIX>
    before it moves any to its target. Ctrl-C between the two left finished
    copies under that name, and they were deleted with the half-made encodes:
    hours of baking again, and a stick sync in between dropped those songs
    (review of #88, finding 11). Back only when the ledger still records that
    copy at the old place, the place is free, and the file's marker says it is
    that copy. True when put back.
    """
    stem = stale.name[: -len(TMP_SUFFIX)]
    name, dot, short = stem.rpartition(".")
    if not dot or len(short) != 12:
        return False
    old = stale.with_name(name)
    if old.exists():
        return False
    for master_hash, c in copies(ledger, kind.name).items():
        if (
            c.output_path == str(old)
            and master_hash.startswith(short)
            and edition_bake.read_marker(stale) == marker_for(master_hash, kind)
        ):
            stale.rename(old)
            return True
    return False


def execute(
    plan: Plan,
    ledger: sqlite3.Connection,
    edition_root: Path,
    *,
    workers: int = 2,
    limit: int | None = None,
    progress: Callable[[str], None] = print,
    wanted_csv: Path | None = None,
    kind: Kind | None = None,
) -> Outcome:
    """Carry out *plan*. Removals and moves first, so space is freed before
    it is spent; then the bakes, recorded one by one as they land.

    *wanted_csv*: each master whose copy had to be compressed goes on that
    wanted list (Grey, 2026-09-27: "also add them to TuneMyMusic.csv").
    """
    kind = kind or LOSSLESS_KIND
    out = Outcome()

    for stale in edition_root.rglob(f"*{TMP_SUFFIX}") if edition_root.exists() else []:
        if not _put_back_stepped_aside(stale, ledger, kind):
            stale.unlink(missing_ok=True)

    for c in plan.forget:
        forget(ledger, kind.name, c.master_hash)

    for c in plan.remove:
        p = Path(c.output_path)
        if p.exists():
            if edition_bake.read_marker(p) != marker_for(c.master_hash, kind):
                out.failed.append((c.output_path, "recorded, but the file there is not the copy"))
                forget(ledger, kind.name, c.master_hash)
                continue
            p.unlink()
            _prune_empty(p.parent, edition_root)
        forget(ledger, kind.name, c.master_hash)
        out.removed += 1

    _move_all(plan, ledger, edition_root, out, kind)

    for m, c in plan.retag:
        try:
            _tag_in_place(ledger, m, Path(c.output_path), kind, c.achieved_lufs, c.mode, c.settings)
            out.retagged += 1
        except Exception as exc:  # noqa: BLE001
            out.failed.append((str(m.path), f"{type(exc).__name__}: {exc}"))

    for m, target in plan.adopt:
        # Tagged again: the record takes the master's mtime of today, so a
        # retag between the stopped build and this one would never reach the
        # copy otherwise (second review of #49).
        try:
            # Its make is unknown (the marker names the master, not the
            # settings): recorded as such, so a copy from before a settings
            # change is made again (second review of #53).
            _tag_in_place(ledger, m, target, kind, None, "adopted", "")
            out.adopted += 1
        except Exception as exc:  # noqa: BLE001
            out.failed.append((str(m.path), f"{type(exc).__name__}: {exc}"))

    todo = plan.bake[:limit] if limit is not None else plan.bake
    if not todo:
        return out
    # Copies being baked again: the verified new copy replaces the old one
    # (its own marked file) in one rename -- never a moment without a copy.
    replaceable = {c.output_path for c in plan.rebake}

    def may_replace(target: Path, h: str) -> bool:
        key = str(target)
        try:
            if key in replaceable:
                # This copy, at the place its record names: marked as this
                # master's, or damaged and untouched since the plan (reviews
                # of #88, finding 4, and of #127).
                if edition_bake.read_marker(target) == marker_for(h, kind):
                    return True
                unchanged = plan.replace_sig.get(key) == _signature(target)
                return unchanged and edition_bake.is_damaged(target)
            if key in plan.damaged:
                # Damaged when planned, and untouched since.
                return _signature(target) == plan.damaged[key] and edition_bake.is_damaged(target)
        except FileNotFoundError:
            return True  # gone since the check: the place is free
        return False

    from .idle_throttle import IdleThrottle

    started = time.monotonic()
    pool = ThreadPoolExecutor(max_workers=max(1, workers))
    # A shutdown (SIGTERM) or a closed terminal (SIGHUP) stops the build as
    # Ctrl-C does, cleaning up after itself: they left half-made copies in
    # the edition, which the transfers copied (cloud review of #53).
    restore = _stop_on_signals()
    futures: dict[Future, tuple[Master, Path]] = {}
    try:
        with IdleThrottle() as throttle:
            edition_bake.ACTIVE_THROTTLE = throttle
            # Kept measurements are read here, in this thread: the workers
            # never touch the record.
            known = {m.audio_hash: _known_measurements(ledger, m) for m, _ in todo}
            for m, target in todo:
                fut = pool.submit(_bake_one, m, target, kind, known[m.audio_hash])
                futures[fut] = (m, target)
            done = 0
            for fut in as_completed(futures):
                m, target = futures[fut]
                done += 1
                tmp: Path | None = None
                try:
                    tmp, result = fut.result()
                    if target.exists() and not may_replace(target, m.audio_hash):
                        tmp.unlink(missing_ok=True)
                        raise edition_bake.BakeError(f"something appeared at {target}")
                    os.replace(tmp, target)
                    record(ledger, _copy(m, target, result.achieved_lufs, result.mode, kind))
                    if (
                        result.recipe
                        and result.measured is not None
                        and known[m.audio_hash].get(result.recipe) != result.measured
                    ):
                        keep_measurement(ledger, m.audio_hash, result.recipe, result.measured)
                except Exception as exc:  # noqa: BLE001 -- one track, not the build
                    if tmp is not None:
                        tmp.unlink(missing_ok=True)  # never left in the edition
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
                    if kind.list_compressed and wanted_csv is not None and _want(m, wanted_csv):
                        out.wanted += 1
                if done % 25 == 0 or done == len(todo):
                    rate = (time.monotonic() - started) / done
                    left = (len(todo) - done) * rate
                    progress(f"  {done:,}/{len(todo):,} baked  (~{left / 60:.0f} min left)")
    except KeyboardInterrupt:
        out.stopped = True
    finally:
        restore()
        if out.stopped:
            # The queue first, then the running encodes: killed first, a
            # worker could start the next song in between (second review).
            pool.shutdown(wait=False, cancel_futures=True)
            edition_bake.stop_children()
        # Always: queued bakes must not run on after the lock and the
        # throttle are released (cloud review of #49).
        pool.shutdown(wait=True, cancel_futures=True)
        edition_bake.STOPPING.clear()
        edition_bake.ACTIVE_THROTTLE = None
        if out.stopped:
            for stale in edition_root.rglob(f"*{TMP_SUFFIX}"):
                stale.unlink(missing_ok=True)
    return out


def _stop_on_signals() -> Callable[[], None]:
    """Turn SIGTERM and SIGHUP into KeyboardInterrupt; return the undo.

    Only the main thread may set handlers; elsewhere this does nothing.
    """
    if threading.current_thread() is not threading.main_thread():
        return lambda: None

    def stop(signum, frame):
        raise KeyboardInterrupt

    old = {s: signal.signal(s, stop) for s in (signal.SIGTERM, signal.SIGHUP)}

    def restore() -> None:
        for s, handler in old.items():
            signal.signal(s, handler)

    return restore


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
        ("Remake damaged", len(plan.damaged)),
        ("Stopped retag", plan.unfinished),
        ("Remove", len(plan.remove)),
        ("Stale records", len(plan.forget)),
    ):
        if n:
            lines.append(f"  {label:<14}: {n:,}")
    will, may = plan.compress_counts()
    if plan.kind_name != EDITION and plan.bake:
        why = (
            "welcome in the car (Grey, 2026-09-28)"
            if plan.kind_name == "car"
            else "ffmpeg's normal range rule, as for the car"
        )
        lines.append(
            f"  Compressed    : at least {will:,} (their peaks); wide-range tracks too -- {why}"
        )
    elif will or may:
        lines.append(
            f"  Compressed    : {will:,} will be, {may:,} may be -- lifting them to "
            f"{plan.target_lufs:.1f} LUFS would push their peaks too high"
        )
    if plan.over_budget:
        lines.append(f"  Over budget   : {len(plan.over_budget):,} track(s) left out for space")
    if plan.resettled:
        lines.append(f"  Re-make       : {plan.resettled:,} copy(ies) made with other settings")
    if len(plan.rebake) > plan.resettled:
        lines.append(
            f"  Re-bake       : {len(plan.rebake) - plan.resettled:,} compressed copy(ies), "
            "with the range rule"
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
