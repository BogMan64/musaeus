#!/usr/bin/env python3
"""Delete tracks a review marked for removal -- every copy, permanently.

Grey's rule, stated more than once: "delete means delete, not quarantine",
and "delete all copies". This is the tool that does it the same way twice,
because doing it by hand is how a deletion half-happens.

WHAT "PERMANENTLY" REQUIRES

  every tier      A track exists in ALAC-Archival (the master), ALAC_Library
                  (the -18 bake) and CAR_Library (the AAC edition). The row
                  knows two of those paths; the master is found by relative
                  path, the same rule editions.master_path_for uses. Missing
                  the master is how a deleted track returns on the next bake.

  denied_hashes   A deletion that does not reach the deny list is not
                  permanent: the next ingest of the same audio walks straight
                  back in. The list lives in the LEDGER (_db_backups/
                  hash_index.db), deliberately -- the catalogue can be
                  rebuilt, and a deny list wiped by a rebuild would let every
                  purged track back in.

  the row         Removed, with one DELETED_BY_REVIEW event per file so the
                  event log says what happened and to what.

SHARED AUDIO. An exact duplicate carries the same audio fingerprint as the
copy that was kept. Denying the duplicate's fingerprint would deny the kept
track as well, and it would be refused on the next rebuild. So a fingerprint
is denied only when no SURVIVING row carries it -- the same rule the file
guard below applies to paths. Found 2026-09-23 clearing a review queue that
had been deleted by hand: 252 of its 1,136 rows shared audio with tracks
still in the library.

ROWS WITH NO FILE. Every removed row leaves an event, including one whose
files were already gone. Events used to be written per deleted file, so such
a row -- once its audio is rightly not denied -- vanished from every record.

ORDER MATTERS. Files first, then the database, per file. A crash leaves a row
whose files are gone -- visible and re-runnable -- rather than a catalogue
that has forgotten tracks still on disk.

CONFIRMED FIRST. Every file is checked to be this track before anything
goes: an edition copy by its marker, the master by its audio (a re-encoded
master by its fingerprint). A row that cannot be confirmed is REFUSED --
nothing of it deleted or denied, and it counts as a survivor for the rest
of the batch. --force-id ID deletes it anyway, for a master that is
damaged or unreadable; its REFUSED line says what is at the path.

EXIT 1 when any row was refused: the others WERE done (partly done).
Rerunning is safe -- the done ids read as already gone.

    python3 scripts/delete_reviewed_tracks.py ids.txt --reason "..."
    python3 scripts/delete_reviewed_tracks.py ids.txt --reason "..." --execute
"""
from __future__ import annotations

import argparse
import sqlite3
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from musaeus.config import MusicConfig  # noqa: E402
from musaeus.db import deny_hash  # noqa: E402
from musaeus.editions import master_path_for  # noqa: E402

TIERS = ("ALAC-Archival", "ALAC_Library", "CAR_Library")

# Only the master mirrors the library copy's path (editions.master_path_for).
# The car tier is filed Artist/Album with no genre level, so a mirrored guess
# there never named the car copy -- it is found from car_export_path, the one
# record of where it actually is.


def copies(row: sqlite3.Row, libs: Path) -> list[Path]:
    """Every path this track could occupy, de-duplicated, order stable."""
    out: list[Path] = []
    seen: set[str] = set()

    def add(p: Path | None) -> None:
        if p and str(p) not in seen:
            out.append(p)
            seen.add(str(p))

    # set(row.keys()), not `in row`: sqlite3.Row's __contains__ tests VALUES,
    # so `"car_export_path" in row` asks whether some column holds that string
    # and is always False. ruff's SIM118 suggests exactly that rewrite here
    # and it would be a silent bug.
    cols = set(row.keys())
    fp = Path(row["file_path"])
    add(fp)
    car = row["car_export_path"] if "car_export_path" in cols else None
    add(Path(car) if car else None)
    master = master_path_for(fp, libs / "ALAC_Library", libs / "ALAC-Archival")
    if master.is_master:
        add(master.path)
    return out


def _not_this_recording(p: Path, row: sqlite3.Row, h0: str | None) -> str:
    """Why the file at *p* cannot be confirmed as the recording *row* names,
    or "" when it is. Anything that cannot be read counts as not confirmed:
    a deletion that cannot confirm its target does not happen (--force-id)."""
    from musaeus.hasher import audio_hash

    if not h0:
        return "the row has no audio hash to confirm it by"
    cols = set(row.keys())
    try:
        if audio_hash(p, strict=True) == h0:
            return ""
    except Exception as exc:  # noqa: BLE001 - unreadable is "not confirmed"
        return f"its audio could not be read ({type(exc).__name__}: {exc})"
    if (row["canon_action"] if "canon_action" in cols else None) != "TRANSCODED":
        return "its audio is not this track's (audio hash differs)"
    stored = row["chromaprint"] if "chromaprint" in cols else None
    if not stored:
        return "a re-encoded master, and no stored fingerprint to confirm it by"
    try:
        from musaeus.stages.acousticid import _fpcalc

        if _fpcalc(str(p))[1] == stored:
            return ""
    except Exception as exc:  # noqa: BLE001
        return f"a re-encoded master whose fingerprint could not be taken ({type(exc).__name__}: {exc})"
    return "a re-encoded master whose fingerprint is not this track's"


def _what_is_there(p: Path) -> str:
    """What the file's own tags say it is, for a person deciding --force-id."""
    try:
        from mutagen import File

        f = File(p, easy=True)
        tags = f.tags if f is not None else None
        if tags:
            artist = (tags.get("artist") or ["?"])[0]
            title = (tags.get("title") or ["?"])[0]
            return f"  [its tags say: {artist} - {title}]"
    except Exception:  # noqa: BLE001
        pass
    return "  [its tags could not be read]"


def remove_emptied_folders(
    folders: set[Path], libs: Path, editions: tuple[Path, ...] = ()
) -> int:
    """rmdir each folder a deletion emptied, and its parents while they are
    empty, stopping at the tier roots and every edition's own folder. Never
    rmtree: art, a .part or a stray the catalogue does not know keeps its
    folder for a person to look at. (Before this, 224 removals on 2026-09-23
    left ~3,600 empty folders; and iPHONE_Library, not a tier here, went
    with its last copy -- cloud review of #53.)"""
    roots = {libs, *(libs / t for t in TIERS), *editions}
    removed = 0
    for d in sorted(folders, key=lambda p: len(p.parts), reverse=True):
        while d not in roots and d != d.parent and any(r in d.parents for r in roots):
            try:
                d.rmdir()
            except OSError:
                break
            removed += 1
            d = d.parent
    return removed


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("ids", type=Path, help="file of archive ids, one per line")
    ap.add_argument("--reason", required=True, help="recorded on every event and deny entry")
    ap.add_argument("--execute", action="store_true", help="actually delete (default: dry run)")
    ap.add_argument(
        "--force-id", type=int, action="append", default=[], metavar="ID",
        help="delete this id's master even though it cannot be confirmed as this track "
             "(damaged, unreadable, re-encoded by a newer ffmpeg). Read its REFUSED line "
             "first: it says what is at the path. Only files outside the edition folders "
             "are forced; a copy inside one is still left unless its marker is this "
             "track's. Repeatable.",
    )  # fmt: skip
    args = ap.parse_args()
    if args.execute:  # the masters lock (review of #87, findings 9 and 10)
        from musaeus.masters_lock import hold_for_process

        hold_for_process(exclusive=True, what="scripts/delete_reviewed_tracks.py")

    ids = [int(x) for x in args.ids.read_text().split() if x.strip().isdigit()]
    # Once each: pass 2 works from the rows pass 1 read, so an id listed twice
    # would be deleted twice.
    ids = list(dict.fromkeys(ids))
    if not ids:
        print(f"no ids in {args.ids}")
        return 1

    cfg = MusicConfig.from_env()
    libs = Path(cfg.vault_root) / "Libraries"
    ledger = Path(cfg.vault_root) / "_db_backups" / "hash_index.db"

    conn = sqlite3.connect(cfg.db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout = 60000")
    led = sqlite3.connect(ledger) if ledger.exists() else None
    if led is None:
        print(f"WARNING: no ledger at {ledger} -- nothing will be denied, so a "
              f"re-ingest of this audio would be accepted again.")

    # Which paths do SURVIVING rows still point at?
    #
    # Two archive rows can share one published file: a track and its "My
    # playlist X" duplicate both carry the same car_export_path, because the
    # car edition is keyed on (artist, title) and they are the same recording.
    # Deleting the duplicate then removes the file the KEPT row names, and the
    # kept row silently becomes a phantom. Measured on this very run before
    # the guard existed: 2 of 82 deletions did exactly that to Paul
    # McCartney's "With a Little Luck" and Rage Against the Machine's "Killing
    # in the Name".
    #
    # A deletion is allowed to remove the track it was asked to remove. It is
    # not allowed to take a different track's file with it.
    doomed = set(ids)
    keep_paths: set[str] = set()
    for col in ("file_path", "car_export_path"):
        try:
            for rid, val in conn.execute(f"SELECT id, {col} FROM archive WHERE COALESCE({col},'') <> ''"):
                if rid not in doomed and val:
                    keep_paths.add(str(Path(val)))
        except sqlite3.Error:
            continue

    # Which AUDIO do surviving rows still carry? Same idea as keep_paths, one
    # level down: a deletion may not deny the fingerprint of a track it is
    # not deleting.
    keep_hashes: set[str] = set()
    try:
        for rid, h in conn.execute("SELECT id, audio_hash FROM archive WHERE COALESCE(audio_hash,'') <> ''"):
            if rid not in doomed:
                keep_hashes.add(h)
    except sqlite3.Error:
        pass

    # The Lossless edition's -18 LUFS copies. Since 2026-09-25 a row points
    # at its master; the copy in ALAC_Library is known only to the edition
    # ledger, by the master's audio hash. Grey, 2026-09-27: it goes with the
    # master, now -- not at the next edition build.
    # Through edition_ledger, the one definition of where the record lives
    # and what it holds (cloud review of #49: a second copy of the path would
    # silently find nothing the day db_history_dir moves, as it did once).
    from musaeus.edition_bake import read_marker
    from musaeus.edition_build import KINDS, marker_for
    from musaeus.edition_ledger import copies as edition_copies
    from musaeus.edition_ledger import forget as forget_copy
    from musaeus.edition_ledger import ledger_path, open_ledger

    edition = open_ledger(ledger_path(cfg)) if ledger_path(cfg).exists() else None
    # Every edition -- Lossless, car, iPhone (Grey, 2026-09-28: all copies).
    edition_copy: dict[str, dict[str, str]] = {}
    if edition is not None:
        edition_copy = {
            name: {h: c.output_path for h, c in edition_copies(edition, name).items()}
            for name in KINDS
        }

    run_id = f"delete_reviewed_{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}_{uuid.uuid4().hex[:6]}"
    n_files = n_rows = n_denied = n_absent = n_shared = n_kept_audio = n_refused = 0
    edition_roots = {name: kind.root(cfg) for name, kind in KINDS.items()}
    emptied: set[Path] = set()
    forced = set(args.force_id)
    for i in sorted(forced - set(ids)):
        print(f"WARNING: --force-id {i} is not in {args.ids}; it does nothing")

    # PASS 1, before anything goes: what is at each path must be THIS track
    # (Codex audit 2026-10-16, F2 and F8). A path is only where the row last
    # saw its file; another recording can be there now.
    #   an edition copy -- in any edition folder, including the old car path
    #     the row itself names -- carries a marker naming its master's audio.
    #     Not this track's marker: left (F8: it was reported LEFT and then
    #     deleted anyway).
    #   anything else is the master: its audio must be this row's. Not this
    #     row's, or not checkable: the whole row is REFUSED -- nothing
    #     deleted, nothing denied, the row kept (F2) -- unless the person
    #     named it with --force-id.
    #   a path a surviving row names is kept whatever it holds, so it is not
    #     checked: a decode for nothing, and a refusal of a row none of whose
    #     files would go (review of #197, finding 3).
    # A re-encoded (TRANSCODED) master keeps its SOURCE's audio hash
    # (stages/finalize.py), so its own audio never matches the row's; its
    # stored fingerprint does, exactly -- measured on both live TRANSCODED
    # masters, 2026-10-17.
    rows: dict[int, sqlite3.Row] = {}
    confirmed: dict[int, list[Path]] = {}
    refused: dict[int, str] = {}
    said: dict[int, list[str]] = {}
    left: dict[int, set[str]] = {}  # paths pass 1 already reported LEFT
    for i in ids:
        row = conn.execute("SELECT * FROM archive WHERE id=?", (i,)).fetchone()
        if row is None:
            continue
        rows[i], said[i], left[i] = row, [], set()
        h0 = row["audio_hash"] if "audio_hash" in set(row.keys()) else None
        checked: list[Path] = []
        for p in copies(row, libs):
            if not p.is_file() or str(p) in keep_paths:
                checked.append(p)
                continue
            kind = next((n for n, r in edition_roots.items() if r in p.parents), None)
            if kind is not None:
                if not h0 or read_marker(p) != marker_for(h0, KINDS[kind]):
                    said[i].append(f"     LEFT (the file there is not this track's copy): {p}")
                    left[i].add(str(p))
                    continue
                checked.append(p)
                continue
            why = _not_this_recording(p, row, h0)
            if why and i in forced:
                said[i].append(f"     FORCED -- {why}: {p}{_what_is_there(p)}")
            elif why:
                refused[i] = f"{why}: {p}{_what_is_there(p)}"
                break
            checked.append(p)
        if i not in refused:
            confirmed[i] = checked

    # A refused row stays in the library, so it is a survivor like any other:
    # another row in this batch may not take its files, its audio (the deny
    # list), its edition copies or its bit-rot baseline (review of #197,
    # finding 1).
    for i in refused:
        row = rows[i]
        cols = set(row.keys())
        for col in ("file_path", "car_export_path"):
            if col in cols and row[col]:
                keep_paths.add(str(Path(row[col])))
        if "audio_hash" in cols and row["audio_hash"]:
            keep_hashes.add(row["audio_hash"])

    # PASS 2: the deletions.
    for i in ids:
        row = rows.get(i)
        if row is None:
            n_absent += 1
            continue
        print(f"\n  id={i}  {row['artist']} - {row['title']}")
        for line in said[i]:
            print(line)
        if i in refused:
            print(f"     REFUSED -- {refused[i]}; nothing of this row is deleted")
            n_refused += 1
            if args.execute:
                conn.execute(
                    "INSERT INTO events (run_id, ts, event_type, file_path, old_value, "
                    "new_value, note) VALUES (?,?,?,?,?,?,?)",
                    (run_id, datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
                     "DELETE_REFUSED", row["file_path"], "", "",
                     f"{args.reason} [refused: {refused[i]}]"))
            continue
        files_for_row = 0
        paths = confirmed[i]
        h0 = row["audio_hash"] if "audio_hash" in set(row.keys()) else None
        ecs = [(name, held[h0]) for name, held in edition_copy.items() if h0 and h0 in held]
        # A copy reported KEPT or LEFT here stays, even when the row's own
        # car_export_path also names it (review of #197, finding 2: F8's
        # shape on the KEPT branch).
        held_back: set[str] = set()
        for name, ec in ecs:
            if h0 in keep_hashes:
                # A copy is keyed by audio: another row with this audio is
                # still in the library, so this copy is its copy too. (Already
                # LEFT in pass 1: another master's copy, said once.)
                if str(Path(ec)) not in left[i]:
                    print(f"     KEPT (a surviving row has the same audio): {ec}")
                held_back.add(str(Path(ec)))
            elif Path(ec).is_file() and read_marker(Path(ec)) != marker_for(h0, KINDS[name]):
                # The build never deletes a file whose marker is not the
                # copy's, and neither does this (second review of #49). The
                # record is stale; it goes below with the others.
                if str(Path(ec)) not in left[i]:
                    print(f"     LEFT (the file there is not this track's copy): {ec}")
                held_back.add(str(Path(ec)))
            elif Path(ec) not in paths:
                paths.append(Path(ec))
        paths = [p for p in paths if str(p) not in held_back]
        for p in paths:
            if not p.is_file():
                continue
            if str(p) in keep_paths:
                print(f"     KEPT (another row still points at it): {p}")
                n_shared += 1
                continue
            print(f"     {'DELETE ' if args.execute else 'would delete '}{p}")
            if args.execute:
                p.unlink()
                emptied.add(p.parent)
                conn.execute(
                    "INSERT INTO events (run_id, ts, event_type, file_path, old_value, "
                    "new_value, note) VALUES (?,?,?,?,?,?,?)",
                    (run_id, datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
                     "DELETED_BY_REVIEW", str(p), str(p), "", args.reason))
            n_files += 1
            files_for_row += 1
        # The copy's record goes AFTER its file, like the row: files first,
        # then the records (see ORDER MATTERS above).
        if h0 not in keep_hashes and args.execute and edition is not None:
            for name, _ in ecs:
                forget_copy(edition, name, h0)
        h = row["audio_hash"] if "audio_hash" in set(row.keys()) else None
        audio_kept = bool(h) and h in keep_hashes
        if audio_kept:
            print(f"     NOT DENIED (another row still carries this audio): {h[:12]}")
            n_kept_audio += 1
        elif h and led is not None:
            if args.execute:
                deny_hash(led, h, args.reason, row["file_path"])
            n_denied += 1
        if args.execute:
            if files_for_row == 0:
                note = args.reason + " [row only: no file was on disk"
                note += "; audio not denied, another row still carries it]" if audio_kept else "]"
                conn.execute(
                    "INSERT INTO events (run_id, ts, event_type, file_path, old_value, "
                    "new_value, note) VALUES (?,?,?,?,?,?,?)",
                    (run_id, datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
                     "DELETED_BY_REVIEW", row["file_path"], row["file_path"], "", note))
            # The master's bit-rot baseline goes with it, or the monthly check
            # lists every deleted song as "missing from disk" (2026-10-07). By
            # path, or by its audio when it moved since the baseline -- never by
            # audio another row still carries: that baseline is the survivor's.
            try:
                if h and not audio_kept:
                    conn.execute(
                        "DELETE FROM archive_tier_hashes WHERE path = ? OR audio_hash = ?",
                        (row["file_path"], h),
                    )
                else:
                    conn.execute("DELETE FROM archive_tier_hashes WHERE path = ?", (row["file_path"],))
            except sqlite3.OperationalError:
                pass  # a vault with no bit-rot baseline yet
            conn.execute("DELETE FROM archive WHERE id=?", (i,))
        n_rows += 1

    n_dirs = 0
    if args.execute:
        conn.commit()
        if led is not None:
            led.commit()
        if edition is not None:
            edition.commit()
        n_dirs = remove_emptied_folders(
            emptied, libs, tuple(kind.root(cfg) for kind in KINDS.values())
        )
    conn.close()
    if led is not None:
        led.close()
    if edition is not None:
        edition.close()

    print(f"\n{'DELETED' if args.execute else 'DRY RUN'}: {n_files} file(s), "
          f"{n_rows} row(s), {n_denied} hash(es) denied"
          + (f", {n_dirs} emptied folder(s) removed" if n_dirs else "")
          + (f", {n_absent} id(s) already gone" if n_absent else "")
          + (f", {n_shared} file(s) kept because a surviving row needs them" if n_shared else "")
          + (f", {n_kept_audio} hash(es) NOT denied because a surviving row carries the same audio"
             if n_kept_audio else "")
          + (f", {n_refused} row(s) REFUSED (see above)" if n_refused else ""))
    return 1 if n_refused else 0


if __name__ == "__main__":
    sys.exit(main())
