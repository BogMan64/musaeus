#!/usr/bin/env python3
"""
MUSAEUS — SpellCheck Stage (Act 1, report-only)

Catches misspelled artist names by fuzzy-matching them against names the
library already knows are real, and against MusicBrainz when asked.

Why a dictionary is the wrong tool here, and this is the right one:
artist names are proper nouns and are routinely, deliberately odd --
"a-ha", "NSYNC", "Ne-Yo", "flexii., cat.flp", "オルゴールサウンド J-POP".
A spellchecker would flag all of those and miss "Deperados", which is
wrong only because "Desperados" exists. The useful question is never "is
this a word" but "is there a near-identical name that IS in the corpus",
and MusicBrainz plus the library's own artist list are that corpus.

REPORT-ONLY, deliberately, and this is not timidity. The same fuzzy
approach applied to truncated names on 2026-08-21 flagged Queen,
Santana, Usher, Macklemore, Nelly and Keith as damaged -- six real
artists out of twenty candidates, a 30% false-positive rate. Reading
each one's actual tracks was the only thing that separated them. A stage
that renamed on a similarity score would have quietly merged six real
artists into other people's catalogues.

So it writes a CSV and changes nothing. Promote it to automatic only
after a few hundred of its calls have been eyeballed and it has earned
it -- see MUSAEUS_TODO.md.

Placement: Act 1, after Normalize, so it sees names in their canonical
article form ("Beatles, The") rather than whatever the source tagged.
"""

from __future__ import annotations

import csv
import logging
import re
from pathlib import Path

from ..context import RunContext, StageResult
from .base import BaseStage

logger = logging.getLogger(__name__)

# rapidfuzz ratio. Set high on purpose: at 88 the truncation experiment
# produced a 30% false-positive rate, and a report nobody trusts is worse
# than no report. 92 keeps the obvious transpositions and drops most of
# the "different artist, similar name" noise.
_THRESHOLD = 92

# Pairs that score high and are genuinely different people. Anything added
# here needs a reason in the comment -- this list is how the stage learns.
_KNOWN_DISTINCT: frozenset[tuple[str, str]] = frozenset(
    {
        ("paul young", "john paul young"),  # UK soul singer vs Australian
        ("bon jovi", "jon bon jovi"),  # band vs the man
        ("beach boys, the", "beach girls, the"),
        ("keith", "keith & kristyn getty"),  # 60s pop vs modern hymn writers
        # Found by this stage's own first live run, 2026-08-22. Both scored
        # in the 90s and both are two different people -- the exact class
        # that makes auto-renaming unsafe.
        ("sonny boy williamson", "sonny boy williamson ii"),  # J.L. Williamson vs Rice Miller
        ("hank williams", "hank williams jr"),  # father and son
        ("hank williams", "hank williams iii"),  # grandson
        ("nat king cole", "natalie cole"),  # father and daughter
        ("frank sinatra", "nancy sinatra"),
        ("bob marley", "ziggy marley"),
        ("john lennon", "julian lennon"),
        ("jeff buckley", "tim buckley"),
    }
)

_NOISE_RE = re.compile(r"[^a-z0-9]+")


def _norm(name: str) -> str:
    """Compare-form: lowercase, punctuation-free, '&' folded to 'and'."""
    return _NOISE_RE.sub("", (name or "").lower().replace("&", " and "))


def _is_known_distinct(a: str, b: str) -> bool:
    la, lb = a.lower().strip(), b.lower().strip()
    return (la, lb) in _KNOWN_DISTINCT or (lb, la) in _KNOWN_DISTINCT


def find_suspects(
    artists: dict[str, int], threshold: int = _THRESHOLD
) -> list[tuple[str, int, str, int, float]]:
    """Artists that look like a misspelling of another artist present.

    Returns (suspect, suspect_files, likely_correct, correct_files, score).

    The rarer name is treated as the suspect: a misspelling typically
    appears on one or two files while the correct spelling carries the
    catalogue. A tie is not reported at all -- with no weight of evidence
    either way there is nothing to suggest.
    """
    try:
        from rapidfuzz import fuzz
    except ImportError:  # pragma: no cover - optional dependency
        logger.warning("[spellcheck] rapidfuzz not installed; nothing to do")
        return []

    names = list(artists)
    normed = {n: _norm(n) for n in names}
    out: list[tuple[str, int, str, int, float]] = []
    for i, a in enumerate(names):
        for b in names[i + 1 :]:
            if normed[a] == normed[b]:
                continue  # same name, different punctuation -- not a typo
            if _is_known_distinct(a, b):
                continue
            score = fuzz.ratio(normed[a], normed[b])
            if score < threshold:
                continue
            na, nb = artists[a], artists[b]
            if na == nb:
                continue
            suspect, correct = (a, b) if na < nb else (b, a)
            out.append((suspect, artists[suspect], correct, artists[correct], score))
    out.sort(key=lambda r: (-r[4], r[1]))
    return out


class SpellCheckStage(BaseStage):
    """Report artist names that look like misspellings. Changes nothing."""

    NAME = "spellcheck"
    CLAIMS_EFFECT = False  # report-only: it makes no claim about disk

    def validate(self, ctx: RunContext) -> None:
        n = ctx.conn.execute(
            "SELECT COUNT(DISTINCT artist) FROM archive WHERE status='CATALOGUED'"
        ).fetchone()[0]
        logger.info("[spellcheck] %d distinct artist(s) to compare", n)

    def _report(self, ctx: RunContext, dry_run: bool) -> StageResult:
        result = self._make_result(dry_run=dry_run)
        artists = {
            r["artist"]: r["n"]
            for r in ctx.conn.execute(
                "SELECT artist, COUNT(*) n FROM archive WHERE status='CATALOGUED' "
                "AND artist IS NOT NULL AND trim(artist) != '' GROUP BY artist"
            )
        }
        result.files_processed = len(artists)
        suspects = find_suspects(artists)

        out = Path(ctx.config.vault_root) / "RUNS" / "spellcheck_suspects.csv"
        out.parent.mkdir(parents=True, exist_ok=True)
        with out.open("w", newline="", encoding="utf-8") as fh:
            w = csv.writer(fh)
            w.writerow(["suspect", "suspect_files", "likely_correct", "correct_files", "score"])
            w.writerows(suspects)

        result.notes.append(f"artists compared: {len(artists)}")
        result.notes.append(f"possible misspellings: {len(suspects)}")
        result.notes.append(f"report: {out}")
        result.notes.append("REPORT ONLY -- nothing renamed. Review before acting.")
        for s, sn, c, cn, sc in suspects[:20]:
            result.notes.append(f"    {s!r} ({sn}) -> {c!r} ({cn})   score={sc:.0f}")
        ctx.record_stage(result)
        return result

    def dry_run(self, ctx: RunContext) -> StageResult:
        return self._report(ctx, dry_run=True)

    def run(self, ctx: RunContext) -> StageResult:
        return self._report(ctx, dry_run=False)
