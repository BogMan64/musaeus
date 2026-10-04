#!/usr/bin/env python3
"""Suggest and CHECK albums for the hand-fill spreadsheet. Writes NOTHING to the catalogue.

Grey, 2026-10-03: run iTunes (under the strict album-fill rules) on the songs
still without an album, put the answers in a separate column to review, and
check any album name against MusicBrainz.

1. Sheet 1, rows whose "Album (type here)" is empty: ask iTunes (exact artist
   and title, no single/EP/compilation/reissue/live album, earliest release),
   then ask MusicBrainz whether the song is on that official studio album.
   Only a CONFIRMED album is suggested; iTunes alone is not trusted (it picked
   "Built for Speed", dated 1974, for a 1981 Stray Cats song). Columns:
   "Suggested album (iTunes)" and "iTunes evidence".
2. Sheet 1, rows where Grey typed an album: "MusicBrainz check" says whether
   MusicBrainz confirms the song is on that album. A check, never a change.
3. Sheet "Check web-filled albums": the same check on "Album filled".

Grey's own column is never touched.

Apple publishes about 20 requests a minute, so ~800 rows take about 40 minutes.
The result is written to a temporary file and moved over the original only at
the end, so a workbook Grey has open is never half-written; progress survives a
restart (rows already answered are skipped).

    python3 scripts/suggest_albums_itunes.py ~/Desktop/MUSAEUS_albums_to_fill_2026-10-03.xlsx
"""

from __future__ import annotations

import re
import sys
import time
from pathlib import Path

from openpyxl import load_workbook
from openpyxl.styles import Font, PatternFill

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from musaeus.network_policy import NetworkPolicy, policy  # noqa: E402
from musaeus.stages import album_fill as af  # noqa: E402

SUGGEST, EVIDENCE, CHECK = "Suggested album (iTunes)", "iTunes evidence", "MusicBrainz check"


def main() -> int:
    # Execute-mode only: the gateway is local-only unless a caller opts in, and
    # this script's whole job is to ask iTunes. Put back when it ends.
    with policy(NetworkPolicy.ALLOWED):
        return _run()


def _run() -> int:
    src = Path(sys.argv[1]).expanduser()
    tmp = src.with_suffix(".partial.xlsx")
    wb = load_workbook(tmp if tmp.exists() else src)
    ws = wb.worksheets[0]
    head = [c.value for c in ws[1]]
    for name in (SUGGEST, EVIDENCE, CHECK):
        if name not in head:
            ws.cell(1, len(head) + 1, name)
            ws.cell(1, len(head) + 1).font = Font(bold=True, color="FFFFFF")
            ws.cell(1, len(head) + 1).fill = PatternFill("solid", fgColor="548235")
            head.append(name)
    col = {h: i + 1 for i, h in enumerate(head)}
    todo = [
        r for r in range(2, ws.max_row + 1)
        if not ws.cell(r, col["Album (type here)"]).value and not ws.cell(r, col[EVIDENCE]).value
    ]  # fmt: skip
    print(f"{len(todo):,} row(s) to ask iTunes about", flush=True)
    for n, r in enumerate(todo, 1):
        artist, title = ws.cell(r, col["Artist"]).value, ws.cell(r, col["Song"]).value
        if not (artist and title) or af._VERSION_QUALIFIER.search(str(title)):
            ws.cell(r, col[EVIDENCE], "not asked: a version title is never searched by text")
            continue
        try:
            time.sleep(af._ITUNES_RATE_S)
            album, why = af.choose_from_itunes(
                af.itunes_search(str(artist), str(title)), str(artist), str(title)
            )
        except af.Unavailable as exc:
            ws.cell(r, col[EVIDENCE], f"no answer ({str(exc)[:60]}); run again")
            continue
        if album:
            try:
                ok, mb = af.mb_confirm(str(artist), album, str(title))
            except af.Unavailable as exc:
                ok, mb = False, f"MusicBrainz busy ({str(exc)[:40]}); run again"
            why = f"{why}; {mb}"
            if ok:
                ws.cell(r, col[SUGGEST], album)
            else:
                why = f"iTunes said '{album}' but {why[0].lower() + why[1:]}"
        ws.cell(r, col[EVIDENCE], re.sub(r"\s+", " ", why))
        if n % 50 == 0:
            wb.save(tmp)
            print(f"  {n:,}/{len(todo):,}", flush=True)
    # Grey's typed albums, and the doubtful web-filled ones: a MusicBrainz check each.
    check_rows = [(ws, col, "Album (type here)")]
    if "Check web-filled albums" in wb.sheetnames:
        w2 = wb["Check web-filled albums"]
        h2 = [c.value for c in w2[1]]
        if CHECK not in h2:
            w2.cell(1, len(h2) + 1, CHECK)
            h2.append(CHECK)
        check_rows.append((w2, {h: i + 1 for i, h in enumerate(h2)}, "Album filled"))
    for sheet, cols, album_name in check_rows:
        for r in range(2, sheet.max_row + 1):
            album = sheet.cell(r, cols[album_name]).value
            if not album or sheet.cell(r, cols[CHECK]).value:
                continue
            try:
                ok, mb = af.mb_confirm(
                    str(sheet.cell(r, cols["Artist"]).value),
                    str(album),
                    str(sheet.cell(r, cols["Song"]).value),
                )
                sheet.cell(r, cols[CHECK], ("CONFIRMED: " if ok else "NOT CONFIRMED: ") + mb)
            except af.Unavailable as exc:
                sheet.cell(r, cols[CHECK], f"no answer ({str(exc)[:50]}); run again")
    for name, width in ((SUGGEST, 34), (EVIDENCE, 60), (CHECK, 60)):
        ws.column_dimensions[ws.cell(1, col[name]).column_letter].width = width
    wb.save(tmp)
    tmp.replace(src)
    got = sum(1 for r in range(2, ws.max_row + 1) if ws.cell(r, col[SUGGEST]).value)
    print(f"DONE: {got:,} suggestion(s) in {src.name}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
