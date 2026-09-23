"""Write the proposal CSV. It proposes; it never decides.

Every review artefact in this project has the same shape: a decision column a
human fills in, one row per decision rather than per file, and the reasoning
beside the row so the reviewer is not asked to take anything on trust.

The DECISION column is FIRST and empty. Deliberately: a reviewer opening this
in a spreadsheet should land on the thing they are being asked for, not scroll
past twelve columns of evidence to find it. Same layout as
MUSAEUS_masterlaw_only_in_august's "MERGE FORWARD?" and ArtistsToReview's
"DECISION (KEEP / REMOVE / RENAME <name>)".
"""

from __future__ import annotations

import csv
import os
from pathlib import Path

from .model import Proposal

HEADER = [
    "DECISION (blank = accept pick, X = reject, or paste the version you want)",
    "artist",
    "song",
    "recommended_version",
    "confidence",
    "margin",
    "why_this_one",
    "runner_up",
    "why_not_that_one",
    "candidates_considered",
    "recording_mbid",
    "release_mbid",
    "listens",
    "first_release_date",
    "length",
]


def _fmt_len(seconds: float | None) -> str:
    if seconds is None:
        return ""
    return f"{int(seconds) // 60}:{int(seconds) % 60:02d}"


def write(proposals: list[Proposal], path: Path) -> dict[str, int]:
    """Write every proposal, decided or not, and return a summary.

    Undecided rows are included rather than dropped. A song this tool could not
    settle is exactly the song Grey needs to see -- omitting it would make the
    CSV look cleaner and be less useful, and the absence would be invisible.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    counts = {
        "rows": 0,
        "confident": 0,
        "undecided": 0,
        "no_candidates": 0,
        "only_one": 0,
        "no_date": 0,
    }

    # Write to .part and rename, never straight to the destination.
    #
    # `path.open("w")` truncates the destination the moment it is called and
    # then streams rows into it. This report is reached from a menu that warns
    # "expect hours on a full library", so a Ctrl-C, a SIGTERM or a crash
    # partway leaves a syntactically valid CSV -- header, then a prefix of the
    # rows -- that is indistinguishable from a finished one. It gets reviewed
    # and applied, and the rows that never got written are invisible.
    #
    # CLAUDE.md states the rule outright: "Existence is not completeness. A
    # half-written file looks finished and gets skipped for ever. Write to
    # .part, verify, then rename." rename() within a directory is atomic, so
    # the destination only ever exists complete.
    part = path.with_suffix(path.suffix + ".part")
    with part.open("w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(HEADER)
        for p in proposals:
            counts["rows"] += 1
            live = [v for v in p.verdicts if not v.excluded]
            best = live[0] if live else None
            second = live[1] if len(live) > 1 else None

            if best is None:
                counts["no_candidates"] += 1
                w.writerow(
                    ["", p.artist, p.title, "", "NO CANDIDATES", "",
                     p.undecided or "nothing to choose between", "", "", 0, "", "", "", "", ""]
                )
                continue

            # Four distinct states, because "not confident" hides three very
            # different situations and a reviewer needs to know which.
            if p.confident:
                counts["confident"] += 1
                confidence = "CONFIDENT"
            elif not p.chose:
                counts["only_one"] += 1
                confidence = "ONLY ONE CANDIDATE (no choice was made)"
            elif not p.judged_originality:
                counts["no_date"] += 1
                confidence = "POPULARITY ONLY (no release date -- run pass 2)"
            else:
                counts["undecided"] += 1
                confidence = "NEEDS A RULING"

            c = best.candidate
            w.writerow(
                [
                    "",
                    p.artist,
                    p.title,
                    c.title or "(untitled)",
                    confidence,
                    "" if p.margin is None else p.margin,
                    best.explanation,
                    second.candidate.title if second else "",
                    second.explanation if second else "",
                    len(p.verdicts),
                    c.recording_mbid,
                    c.release_mbid,
                    "" if c.listen_count is None else c.listen_count,
                    c.first_release_date,
                    _fmt_len(c.length_seconds),
                ]
            )
        fh.flush()
        os.fsync(fh.fileno())

    # Only now does the destination exist, and it exists complete.
    part.replace(path)
    return counts
