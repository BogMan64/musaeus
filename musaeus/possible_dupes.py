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
is moved or deleted. A pair is settled in `musaeus dedupe` or by hand.
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

_AND = re.compile(r"\s*&\s*")
#: A bracket that only names who played with whom: "(With Teddy Wilson And His
#: Orchestra)", "(feat. Cher)". Two songs sharing one scored as one song.
_CREDIT_BRACKET = re.compile(
    rf"[{OPEN}]\s*(?:with|feat\.?|featuring|ft\.?)\s[^{CLOSE}]*[{CLOSE}]", re.I
)


def _title_key(title: str) -> str:
    from .stages.neardupe import _normalise

    t = _CREDIT_BRACKET.sub(" ", title or "")
    return _normalise(_AND.sub(" and ", t), strip_qualifiers=True)


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
    from .stages.neardupe import _is_different_piece, _same_take

    conn.row_factory = sqlite3.Row
    cols = {r[1] for r in conn.execute("PRAGMA table_info(archive)")}
    mb = "mb_artist_name" if "mb_artist_name" in cols else "NULL AS mb_artist_name"
    rows = conn.execute(
        f"SELECT file_path, artist, title, album, duration, codec, bitrate, sample_rate, "
        f"audio_hash, {mb} FROM archive WHERE status = 'CATALOGUED' "
        "AND title IS NOT NULL AND trim(title) != '' AND artist IS NOT NULL"
    ).fetchall()

    # Pairs a person kept both of: decided, never offered again.
    kept: dict[str, list] = defaultdict(list)
    for g in conn.execute("SELECT group_id, status, audio_hash FROM duplicates"):
        kept[g["group_id"]].append(g)
    keep_both = {
        frozenset(r["audio_hash"] for r in rs)
        for rs in kept.values()
        if len(rs) > 1 and all(r["status"] == "keep" and r["audio_hash"] for r in rs)
    }

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
                if frozenset((a["audio_hash"], b["audio_hash"])) in keep_both:
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
                if _is_live(dict(a)) and _is_live(dict(b)) and not _same_take(dict(a), dict(b)):
                    continue
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
        row: dict[str, object] = {"pair": n, "why": p.why, "artist": p.a["artist"], "decide": ""}
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
    print("  Nothing was moved or deleted. Settle a pair in `musaeus dedupe` or by hand.")
    return 0
