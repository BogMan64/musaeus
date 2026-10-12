"""Songs that may be one recording, for a person to decide (Grey, 2026-10-11).

Grey asked for fewer duplicates in the archive where MUSAEUS can tell, and a
Desktop report where it cannot. The duplicate checks in Act 2 decide what
they are sure of: the same audio (CrossDupe), the same fingerprint
(AcoustID), a near-identical title by the same act (NearDupe, then the
resolver's keep rule). What is left after a run is this report:

    musaeus possible-dupes          ~/Desktop/MUSAEUS_possible_duplicates_<date>.csv

A pair is listed when it is one act's, its titles are the same song once
"&"/"and", apostrophes, punctuation, case and version words (Remastered,
Live, Mono ...) are folded, or nearly so, and the two are about the same
length. "Shout (Part 1 & 2)" and "Shout (Part 1 And 2)" is the kind of pair.

Left out, because a check already decided them or they are not one song:
the same audio, pairs Grey kept both of, different classical movements,
and two live recordings of different nights.

Read-only: it opens the catalogue read-only and writes only the sheet. Nothing
is moved or deleted. A pair is settled by hand: removing the copy not wanted.
"""

from __future__ import annotations

import contextlib
import csv
import datetime
import os
import re
import sqlite3
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path

from .brackets import CLOSE, OPEN

#: Columns of the sheet. `decide` is for Grey's notes; nothing reads it back.
SHEET_FIELDS = [
    "pair",
    "why",
    "artist",
    "artist_b",
    "title_a",
    "album_a",
    "length_a",
    "quality_a",
    "file_a",
    "title_b",
    "album_b",
    "length_b",
    "quality_b",
    "file_b",
    "decide",
]

#: Close titles (after folding) at about the same length.
TITLE_SCORE = 85
SAME_LENGTH_S = 3.0
#: The same title after folding may be a longer edit of one recording.
SAME_TITLE_LENGTH_S = 10.0

#: A bracket that only names an edit of one recording: "(Single)", "(Album
#: Edit)", "(Video)", "(Original Mix)", "(Bonus Track)" -- 15 live pairs hid
#: behind them (pre-merge review of #191). NearDupe's own words come on top.
_EDIT_BRACKET = re.compile(
    rf"[{OPEN}]\s*(?:(?:19|20)\d{{2}}\s+)?(?:single|album|original|radio|extended|video|"
    rf"official|bonus|edit|mix|track|version|\s)+[{CLOSE}]",
    re.I,
)
_REDONE = re.compile(r"\bre-?recorded\b|\bre-?recording\b", re.I)


def _title_key(title: str) -> str:
    """NearDupe's own title key ("&" is "and"), with credit and edit brackets out."""
    from .stages.neardupe import _CREDIT_BRACKET, _normalise

    t = _CREDIT_BRACKET.sub(" ", title or "")
    t = _EDIT_BRACKET.sub(" ", t)
    return _normalise(t, strip_qualifiers=True)


def _quality(r: sqlite3.Row) -> str:
    codec = (r["codec"] or "").lower()
    rate = f"{round(r['sample_rate'] / 1000, 1):g} kHz" if r["sample_rate"] else ""
    kbps = f"{round(r['bitrate'] / 1000)} kbps" if r["bitrate"] else ""
    return " ".join(x for x in (codec, kbps, rate) if x)


def _length(seconds: float | None) -> str:
    if not seconds:
        return ""
    m, s = divmod(int(round(seconds)), 60)
    return f"{m}:{s:02d}"


@dataclass
class Pair:
    why: str
    a: sqlite3.Row
    b: sqlite3.Row


def find(conn: sqlite3.Connection) -> list[Pair]:
    """The pairs, most likely first within each act."""
    from rapidfuzz import fuzz  # type: ignore[import-untyped]

    from .artist_form import act_key, folder_artist
    from .stages.dupe_resolver import _is_live
    from .stages.neardupe import _SAME_TAKE_SECONDS, _credit, _is_different_piece, _same_take

    conn.row_factory = sqlite3.Row
    cols = {r[1] for r in conn.execute("PRAGMA table_info(archive)")}
    mb = "mb_artist_name" if "mb_artist_name" in cols else "NULL AS mb_artist_name"
    rows = conn.execute(
        f"SELECT file_path, artist, title, album, duration, codec, bitrate, sample_rate, "
        f"audio_hash, {mb} FROM archive WHERE status = 'CATALOGUED' "
        "AND title IS NOT NULL AND trim(title) != '' AND artist IS NOT NULL"
    ).fetchall()

    # Pairs a person kept both of: decided, never offered again. By path OR by
    # recording: a fingerprint can change after the decision (2 of 55 live
    # keep-both groups), and a group can hold more than two (pre-merge review
    # of #191). And AcoustID's pairs already waiting for Grey are said so.
    kept_paths: dict[str, set[str]] = defaultdict(set)
    kept_hashes: dict[str, set[str]] = defaultdict(set)
    waiting: dict[str, set[str]] = defaultdict(set)
    for g in conn.execute(
        "SELECT group_id, file_path, status, audio_hash, duplicate_type FROM duplicates"
    ):
        if g["status"] == "keep":
            kept_paths[g["group_id"]].add(g["file_path"])
            if g["audio_hash"]:
                kept_hashes[g["group_id"]].add(g["audio_hash"])
        elif g["status"] == "review" and g["duplicate_type"] == "ACOUSTIC":
            waiting[g["group_id"]].add(g["file_path"])

    def kept_both(a: sqlite3.Row, b: sqlite3.Row) -> bool:
        ab = {a["file_path"], b["file_path"]}
        if any(ab <= paths for paths in kept_paths.values()):
            return True
        hashes = {a["audio_hash"], b["audio_hash"]}
        return None not in hashes and any(hashes <= hs for hs in kept_hashes.values())

    def acoustid_waiting(a: sqlite3.Row, b: sqlite3.Row) -> bool:
        return any({a["file_path"], b["file_path"]} <= paths for paths in waiting.values())

    acts: dict[str, list[sqlite3.Row]] = defaultdict(list)
    for r in rows:
        acts[act_key(folder_artist(r["artist"], r["mb_artist_name"]))].append(r)

    pairs: list[Pair] = []
    for members in acts.values():
        keyed = [(r, _title_key(r["title"])) for r in members]
        for i, (a, ka) in enumerate(keyed):
            for b, kb in keyed[i + 1 :]:
                if a["audio_hash"] and a["audio_hash"] == b["audio_hash"]:
                    continue  # the same audio: CrossDupe's
                if kept_both(a, b):
                    continue
                da, db = a["duration"], b["duration"]
                if not da or not db or not ka or not kb:
                    continue
                gap = abs(da - db)
                if ka == kb:
                    if gap > SAME_TITLE_LENGTH_S:
                        continue
                    why = "same title"
                else:
                    if gap > SAME_LENGTH_S:
                        continue
                    score = fuzz.ratio(ka, kb)
                    if score < TITLE_SCORE:
                        continue
                    why = f"titles {score:.0f}% alike"
                if _is_different_piece(a["title"], b["title"]):
                    continue
                live_a, live_b = _is_live(dict(a)), _is_live(dict(b))
                if live_a and live_b and not _same_take(dict(a), dict(b)):
                    continue
                # A live take against a studio one, a re-recording, two
                # versions with different guests: not one recording (pre-merge
                # review of #191).
                if live_a != live_b and gap > _SAME_TAKE_SECONDS:
                    continue
                if bool(_REDONE.search(a["title"])) != bool(_REDONE.search(b["title"])):
                    continue
                ca, cb = _credit(a["title"]), _credit(b["title"])
                if ca and cb and ca != cb:
                    continue
                if acoustid_waiting(a, b):
                    why = f"AcoustID: same recording, waiting for you; {why}"
                pairs.append(Pair(f"{why}, {gap:.1f} s apart", a, b))
    pairs.sort(key=lambda p: ((p.a["artist"] or "").lower(), (p.a["title"] or "").lower()))
    return pairs


def _relative(path: str, roots: list[Path]) -> str:
    p = Path(path)
    for root in roots:
        with contextlib.suppress(ValueError):
            return str(p.relative_to(root))
    return path


def write_sheet(pairs: list[Pair], out: Path, roots: list[Path]) -> None:
    """Write the sheet beside *out*, then rename it into place."""
    rows = []
    for n, p in enumerate(pairs, 1):
        row: dict[str, object] = {
            "pair": n,
            "why": p.why,
            "artist": p.a["artist"],
            "artist_b": p.b["artist"] if p.b["artist"] != p.a["artist"] else "",
            "decide": "",
        }
        for side, r in (("a", p.a), ("b", p.b)):
            row[f"title_{side}"] = r["title"]
            row[f"album_{side}"] = r["album"] or ""
            row[f"length_{side}"] = _length(r["duration"])
            row[f"quality_{side}"] = _quality(r)
            row[f"file_{side}"] = _relative(r["file_path"], roots)
        rows.append(row)
    part = out.with_name(out.name + ".part")
    try:
        with open(part, "w", encoding="utf-8-sig", newline="") as fh:
            w = csv.DictWriter(fh, fieldnames=SHEET_FIELDS)
            w.writeheader()
            w.writerows(rows)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(part, out)
    except BaseException:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(part)
        raise


def default_sheet() -> Path:
    return Path.home() / "Desktop" / f"MUSAEUS_possible_duplicates_{datetime.date.today()}.csv"


def cmd_possible_dupes(*, out: str | None, dry_run: bool) -> int:
    from .config import get_config

    cfg = get_config()
    sheet = Path(out).expanduser() if out else default_sheet()
    if sheet.is_dir():
        print(f"ERROR: {sheet} is a folder -- give a file name for the sheet.")
        return 1
    if sheet.exists() and not dry_run:
        print(f"ERROR: {sheet} already exists -- it may hold your notes.")
        print("  Move it, or pass --out for a new sheet.")
        return 1
    conn = sqlite3.connect(f"file:{cfg.db_path}?mode=ro", uri=True)
    try:
        pairs = find(conn)
    finally:
        conn.close()
    print(f"\nMusaeus possible-dupes{' [DRY RUN]' if dry_run else ''}")
    print(f"  pairs that may be one recording: {len(pairs):,}")
    if dry_run:
        for p in pairs[:10]:
            print(f"    {p.a['artist']}: {p.a['title']!r} / {p.b['title']!r}  ({p.why})")
        return 0
    sheet.parent.mkdir(parents=True, exist_ok=True)
    write_sheet(pairs, sheet, [cfg.alac_archive, cfg.alac_library, cfg.vault_root])
    print(f"  Sheet: {sheet}")
    print("  Nothing was moved or deleted. These pairs are not waiting in `musaeus dedupe`:")
    print("  settle one by removing the copy you do not want. The `decide` column is for")
    print("  your notes; MUSAEUS does not read it, so a pair kept as two stays on the list.")
    return 0
