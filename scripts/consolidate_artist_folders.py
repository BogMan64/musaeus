#!/usr/bin/env python3
"""Merge an artist filed under two names into one, folders and catalogue together.

Grey, 2026-09-16: "we do not need to edit the music tracks just the artist
folder for searching." So this moves FILES and updates the CATALOGUE. It
never rewrites a library or master tag -- those files are left
byte-identical. (The car copy's album-artist tag is rewritten, because the
car edition files by it.)

WHY THIS IS NOT JUST `mv`

    archive.artist          what every lookup and every report reads
    archive.genre           the top folder; a merge can cross genres
    archive.file_path       absolute; a move orphans it
    archive.car_export_path absolute; same
    artist_canon.tsv        what stops the split reappearing on the next ingest

A rename that carries only some of those produces a catalogue pointing at
files that are not there -- the phantom-row failure this library has had to
repair twice in one day. All of them move together or the merge is not done.

EVERY COPY IS FOUND FROM THE CATALOGUE, NEVER FROM A FOLDER NAME

This script used to walk `Libraries/<tier>/<artist folder>`. The library
gained a genre level on 2026-09-18 (Genre/Artist/Album) and the walk found
nothing -- a dry run of "Simon" -> "Simon & Garfunkel" on 2026-09-24 reported
0 files in both ALAC tiers while 17 sat in Folk/Simon, and an --execute
would have relabelled all 17 rows and moved none of their files. The car tier
had already taught the same lesson (it files by album-artist, so the folder
guess missed it). So now:

    library copy   archive.file_path
    master copy    editions.master_path_for(file_path)  -- the mirrored path
    car copy       archive.car_export_path

and every destination comes from organize.library_relpath(), the rule
OrganizeStage files by, so the next organize pass leaves a merged file where
this put it.

A CLASH REFUSES THE WHOLE MERGE

If any destination is already taken, nothing moves and nothing is relabelled.
Moving the rest would leave the artist split across two names with the
catalogue claiming otherwise, and deleting the "duplicate" is a decision
about audio, not about names -- it belongs to a person or to the duplicate
resolver, not to a rename.

REFILING UNDER A NEW GENRE

    --genre Celtic      files the moved rows under that genre
    OLD == NEW          allowed only with --genre: refile an artist in place
                        (Delerium -> Celtic, after MasterLaw says so)

Without --genre a merge takes the genre the target artist is already filed
under, so a cross-genre merge (Crosby in Hip Hop -> Crosby, Stills, Nash &
Young in Folk Rock) lands in the target's folder. A target with no rows keeps
each row's own genre.

FOLDERS ARE SORT FORM, TAGS ARE NATURAL FORM

"The English Beat" files under "English Beat, The". The canonical name given
on the command line is the TAG form; the folder name is derived by
library_relpath(). Passing a folder name by hand is how the two conventions
drift apart.

    python3 scripts/consolidate_artist_folders.py "Simon" "Simon & Garfunkel"
    python3 scripts/consolidate_artist_folders.py "Simon" "Simon & Garfunkel" --execute
    python3 scripts/consolidate_artist_folders.py "Delerium" "Delerium" --genre Celtic
"""
from __future__ import annotations

import argparse
import collections
import sqlite3
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

import mutagen.mp4

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from musaeus.artist_form import sort_form  # noqa: E402
from musaeus.config import MusicConfig  # noqa: E402
from musaeus.editions import master_path_for  # noqa: E402
from musaeus.filing import load as filing_load  # noqa: E402
from musaeus.stages.organize import library_relpath  # noqa: E402

EXIT_CLASH = 3


@dataclass
class RowPlan:
    id: int
    moves: list[tuple[Path, Path]] = field(default_factory=list)  # (src, dst), every tier
    file_path: str | None = None  # new value, or None to leave
    car_export_path: str | None = None  # new value, or None to leave


@dataclass
class MergePlan:
    old: str
    new: str
    genre: str | None  # the genre written to moved rows; None = each keeps its own
    rows: list[RowPlan] = field(default_factory=list)
    relabel_only: int = 0  # rows with no library file (quarantine, review)
    clashes: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    dest_folders: collections.Counter = field(default_factory=collections.Counter)

    @property
    def moves(self) -> list[tuple[Path, Path]]:
        return [m for r in self.rows for m in r.moves]


def target_genre(conn: sqlite3.Connection, old: str, new: str, override: str | None) -> tuple[str | None, list[str]]:
    """The genre the moved rows file under, and anything worth saying about it.

    MasterLaw rules genre per ARTIST, so the target's own rows already say
    where it lives. More than one value there is a library problem (doctor
    counts it); the commonest wins and the dry run says so.
    """
    if override:
        return override, []
    if old == new:
        return None, []
    found = collections.Counter(
        g for (g,) in conn.execute(
            "SELECT genre FROM archive WHERE artist = ? AND status = 'CATALOGUED' AND COALESCE(genre,'') <> ''",
            (new,),
        )
    )
    if not found:
        return None, [f"{new!r} has no filed rows yet; each moved row keeps its own genre"]
    notes = []
    if len(found) > 1:
        notes.append(f"{new!r} is filed under {len(found)} genres {dict(found)}; using the commonest")
    return found.most_common(1)[0][0], notes


def plan_merge(cfg, conn: sqlite3.Connection, old: str, new: str, genre: str | None = None) -> MergePlan:
    """Everything an --execute would do, computed without touching anything."""
    lib, arch = Path(cfg.alac_library), Path(cfg.alac_archive)
    car_root = Path(cfg.vault_root) / "Libraries" / "CAR_Library"
    g, notes = target_genre(conn, old, new, genre)
    plan = MergePlan(old, new, g, notes=notes)

    claimed: dict[Path, int] = {}  # dst -> row id, so two rows cannot land on one path
    car_moved: dict[Path, Path] = {}  # a car file two rows share moves once
    for row in conn.execute("SELECT * FROM archive WHERE artist = ? ORDER BY id", (old,)).fetchall():
        rp = RowPlan(row["id"])
        fp = Path(row["file_path"]) if row["file_path"] else None
        try:
            rel_now = fp.relative_to(lib) if fp else None
        except ValueError:
            rel_now = None
        if rel_now is None:
            plan.relabel_only += 1
            plan.rows.append(rp)
            continue

        rel = library_relpath(new, row["mb_artist_name"], g or row["genre"], row["album"], row["title"], fp.suffix)
        plan.dest_folders[str(Path(*rel.parts[:2]))] += 1
        if rel != rel_now:
            rp.moves.append((fp, lib / rel))
            rp.file_path = str(lib / rel)
            master = master_path_for(fp, lib, arch)
            if master.is_master and master.path != fp:
                rp.moves.append((master.path, arch / rel))

        car = Path(row["car_export_path"]) if row["car_export_path"] else None
        if car is not None and car.is_file():
            stem = car.name
            renamed = f"{sort_form(new)} - {stem.split(' - ', 1)[1]}" if " - " in stem else stem
            car_dst = car_root / sort_form(new) / car.parent.name / renamed
            if car_dst != car:
                if car not in car_moved:
                    car_moved[car] = car_dst
                    rp.moves.append((car, car_dst))
                rp.car_export_path = str(car_dst)

        for src, dst in rp.moves:
            if not src.is_file():
                plan.clashes.append(f"id={row['id']}: source missing: {src}")
            elif dst.exists():
                plan.clashes.append(f"id={row['id']}: already taken: {dst}")
            elif dst in claimed:
                plan.clashes.append(f"id={row['id']}: same destination as id={claimed[dst]}: {dst}")
            else:
                claimed[dst] = row["id"]
        plan.rows.append(rp)
    return plan


def _remove_empty_parents(start: Path, stop_at: set[Path]) -> int:
    """rmdir upward while empty. Never rmtree: a folder holding anything the
    catalogue does not know about (art, a .part, a stray) is left for a
    person, not deleted for them."""
    removed = 0
    d = start
    while d not in stop_at and d != d.parent:
        try:
            d.rmdir()
        except OSError:
            break
        removed += 1
        d = d.parent
    return removed


def _retag_car(path: Path, album_artist: str) -> None:
    tags = mutagen.mp4.MP4(path)
    if tags.tags is None:
        tags.add_tags()
    tags.tags["aART"] = [album_artist]
    tags.save()


def execute_plan(cfg, conn: sqlite3.Connection, plan: MergePlan) -> dict[str, int]:
    """Apply a clash-free plan one row at a time.

    Each row's files move, then its catalogue row changes, then it commits --
    so an interruption leaves at most one row half-done, and that row's moves
    are put back before the error propagates.
    """
    if plan.clashes:
        raise RuntimeError("refusing to execute a plan with clashes")
    lib, arch = Path(cfg.alac_library), Path(cfg.alac_archive)
    car_root = Path(cfg.vault_root) / "Libraries" / "CAR_Library"
    run_id = f"consolidate_{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}"
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    note = f"{plan.old} -> {plan.new}" + (f" [genre {plan.genre}]" if plan.genre else "")
    counts = collections.Counter()
    emptied: set[Path] = set()

    for rp in plan.rows:
        done: list[tuple[Path, Path]] = []
        try:
            for src, dst in rp.moves:
                dst.parent.mkdir(parents=True, exist_ok=True)
                src.rename(dst)
                done.append((src, dst))
                emptied.add(src.parent)
                if dst.is_relative_to(car_root):
                    _retag_car(dst, plan.new)
        except Exception:
            for src, dst in reversed(done):
                dst.rename(src)
            raise

        sets, args = ["artist = ?"], [plan.new]
        if plan.genre and rp.file_path is not None:
            sets.append("genre = ?")
            args.append(plan.genre)
        if rp.file_path is not None:
            sets.append("file_path = ?")
            args.append(rp.file_path)
        conn.execute(f"UPDATE archive SET {', '.join(sets)} WHERE id = ?", (*args, rp.id))
        counts["rows"] += 1
        for src, dst in rp.moves:
            if dst.is_relative_to(car_root):
                # Every row naming this car file follows it, not only this one.
                counts["car_paths"] += conn.execute(
                    "UPDATE archive SET car_export_path = ? WHERE car_export_path = ?", (str(dst), str(src))
                ).rowcount
            conn.execute(
                "INSERT INTO events (run_id, ts, event_type, file_path, old_value, new_value, stage, note) "
                "VALUES (?,?,?,?,?,?,?,?)",
                (run_id, now, "ARTIST_CONSOLIDATED", str(dst), str(src), str(dst), "consolidate", note),
            )
            counts["files"] += 1
        conn.commit()

    for d in emptied:
        counts["dirs_removed"] += _remove_empty_parents(d, {lib, arch, car_root})
    return dict(counts)


def _write_canon_entry(canon: Path, old: str, new: str) -> int:
    """Add old -> new, and re-point anything that would now chain through it.

    resolve_exact does NOT follow a chain. So if some existing row already
    says `X -> old`, adding `old -> new` leaves X landing on `old` and
    stopping there -- half-way, at a name nothing else uses. doctor's
    "authorities agree" check calls that a FAIL, and it is right to.

    It happened the first time this script ran. Merging ELO into Electric
    Light Orchestra turned a pre-existing "Jeff Lynne's ELO" -> "ELO" into a
    chain, and doctor went red on the next run. Nothing was mis-filed --
    both names had zero tracks -- but the ruling file was inconsistent and
    the next artist to arrive under that name would have landed wrong.

    Returns how many existing entries had to be re-pointed.
    """
    text = canon.read_text(encoding="utf-8")
    lines = text.splitlines()

    repointed = 0
    for i, ln in enumerate(lines):
        parts = ln.split("\t", 1)
        if len(parts) == 2 and parts[1].strip() == old:
            lines[i] = f"{parts[0]}\t{new}"
            repointed += 1

    # And never write a row whose canonical is itself a key -- the same
    # chain, created in the other direction.
    keys = {ln.split("\t", 1)[0].strip().lower()
            for ln in lines if "\t" in ln and not ln.startswith("#")}
    if new.lower() in keys:
        dest = next(ln.split("\t", 1)[1].strip() for ln in lines
                    if "\t" in ln and ln.split("\t", 1)[0].strip().lower() == new.lower())
        print(f"  note: {new!r} is itself a canon key pointing at {dest!r}; "
              f"writing {old!r} -> {dest!r} instead")
        new = dest

    # Whole-key comparison, not `f"{old}\t" in text`. That substring test
    # reports a match when `old` is merely the TAIL of another key:
    # "Jeff Lynne's ELO\tELO" contains "ELO\t", so adding ELO was silently
    # skipped and the merge left no canon entry at all.
    if old.lower() not in keys:
        lines.append(f"{old}\t{new}")
        print(f"  artist_canon.tsv: added {old!r} -> {new!r}")

    canon.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return repointed


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("old", help="the artist name to retire (tag form)")
    ap.add_argument("new", help="the artist name to keep (tag form)")
    ap.add_argument("--genre", help="file the moved rows under this genre")
    ap.add_argument("--execute", action="store_true")
    args = ap.parse_args()
    if args.old == args.new and not args.genre:
        ap.error("OLD and NEW are the same name; that is only a refile, which needs --genre")

    cfg = MusicConfig.from_env()
    conn = sqlite3.connect(cfg.db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout = 60000")

    plan = plan_merge(cfg, conn, args.old, args.new, args.genre)
    n_lib = sum(1 for r in plan.rows if r.file_path is not None)
    print(f"  {args.old!r} -> {args.new!r}" + (f"  [genre: {plan.genre}]" if plan.genre else ""))
    print(f"  rows: {len(plan.rows)}  ({n_lib} to move, {plan.relabel_only} outside the library, relabel only)")
    for folder, n in plan.dest_folders.most_common():
        print(f"  into {folder}/  ({n})")
    for n in plan.notes:
        print(f"  note: {n}")
    filing = filing_load(Path(cfg.meta_dir))
    if args.old in filing:
        print(f"  note: artist_filing.tsv files {args.old!r} under {filing[args.old]!r}; that line is now stale")

    if plan.clashes:
        print(f"\nREFUSED: {len(plan.clashes)} clash(es), nothing moved, nothing relabelled:")
        for c in plan.clashes[:20]:
            print(f"   {c}")
        return EXIT_CLASH

    if not args.execute:
        print(f"\nDRY RUN: {len(plan.moves)} file(s), {len(plan.rows)} row(s) would change")
        return 0

    counts = execute_plan(cfg, conn, plan)
    conn.close()
    if args.old != args.new:
        canon = Path(cfg.meta_dir) / "artist_canon.tsv"
        if canon.is_file():
            n_chain = _write_canon_entry(canon, args.old, args.new)
            if n_chain:
                print(f"  artist_canon.tsv: re-pointed {n_chain} entry(ies) that would "
                      f"otherwise chain through {args.old!r}")
    print(f"\nDONE: {counts.get('files', 0)} file(s), {counts.get('rows', 0)} row(s), "
          f"{counts.get('car_paths', 0)} car_export_path, {counts.get('dirs_removed', 0)} empty folder(s) removed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
