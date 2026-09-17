"""The review CSV appears complete or not at all -- never half-written.

The menu this report is reached from warns "expect hours on a full library".
It used to stream rows straight into the destination, so a Ctrl-C, a SIGTERM
or a crash partway left a syntactically valid CSV -- header, then a prefix of
the rows -- indistinguishable from a finished one. It would be reviewed and
applied, and the rows that never got written would be invisible.

CLAUDE.md: "Existence is not completeness. A half-written file looks finished
and gets skipped for ever. Write to .part, verify, then rename."
"""

from __future__ import annotations

import csv
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fmradio.report import write  # noqa: E402


class _Boom(RuntimeError):
    pass


class _ExplodingProposals:
    """Iterates a few rows, then fails -- an interrupted run, exactly."""

    def __init__(self, good, at):
        self._good, self._at = good, at

    def __iter__(self):
        for i, p in enumerate(self._good):
            if i == self._at:
                raise _Boom("interrupted")
            yield p


def _proposal():
    from fmradio.model import Proposal
    return Proposal(artist="Blur", title="Song 2", verdicts=[], undecided="")


class TestACompletedReportIsReadable:
    def test_it_writes_the_destination(self, tmp_path):
        out = tmp_path / "r.csv"
        write([_proposal()], out)
        assert out.is_file()
        rows = list(csv.reader(out.open(encoding="utf-8")))
        assert len(rows) == 2, "header plus one row"

    def test_no_part_file_is_left_behind(self, tmp_path):
        out = tmp_path / "r.csv"
        write([_proposal()], out)
        assert not list(tmp_path.glob("*.part")), "the .part must be renamed away"


class TestAnInterruptedRunLeavesNothingToMistake:
    def test_the_destination_is_not_created(self, tmp_path):
        out = tmp_path / "r.csv"
        with pytest.raises(_Boom):
            write(_ExplodingProposals([_proposal()] * 5, at=3), out)
        assert not out.exists(), (
            "a partial CSV at the destination reads as a finished report"
        )

    def test_an_existing_good_report_is_not_destroyed(self, tmp_path):
        """The worse version of the same bug: opening the destination for
        writing truncates LAST week's good report before a single new row is
        written, so an interrupted re-run loses the file it was replacing."""
        out = tmp_path / "r.csv"
        write([_proposal()], out)
        before = out.read_bytes()
        with pytest.raises(_Boom):
            write(_ExplodingProposals([_proposal()] * 5, at=3), out)
        assert out.read_bytes() == before, "the previous report was clobbered"
