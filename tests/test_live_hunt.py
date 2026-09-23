"""live_hunt reuses MUSAEUS's live rule; it must never grow its own.

Grey: "I rarely like a live version over a studio." The tool produces a
hunting list -- which live recordings have no studio counterpart here.

The rule for "is this live" already exists as `_LIVE_MARKERS` in
musaeus/stages/dupe_resolver.py, where it makes live copies lose to studio
copies when picking a duplicate keeper. Two answers to that question is the
failure these tests exist to prevent.

Why it is read with `ast` rather than imported
----------------------------------------------
Re-tested on 2026-09-15 when the tool moved into the repository, because
"it can just import it now" is the obvious assumption. It is wrong:

    from musaeus.stages.dupe_resolver import _LIVE_MARKERS
        -> 277 ms, 78 musaeus modules, and musaeus/config.py runs
           _load_env() at import time, so the import MUTATES os.environ.

A read-only reporting tool has no business doing that, so the ast read
survived the move. What the move DID fix: the source path was hardcoded to
/mnt/FORGE2TB/Projects/MUSAEUS-sandbox, a directory that does not exist, so
the tool exited with its own "point --musaeus-src at a checkout" message
every single run. Inside the repo the path is knowable from __file__.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts" / "live_hunt"))

import live_hunt  # noqa: E402


class TestItBorrowsTheRuleRatherThanCopyingIt:
    def test_the_default_source_path_exists(self):
        """The bug the move fixed. A path that does not resolve makes the
        tool unrunnable, and it fails the same way whether MUSAEUS moved or
        the constant was simply wrong."""
        assert live_hunt.MUSAEUS_SRC.is_file(), live_hunt.MUSAEUS_SRC

    def test_the_markers_match_musaeus_exactly(self):
        """The whole point: one rule, one home."""
        from musaeus.stages.dupe_resolver import _LIVE_MARKERS

        assert live_hunt.load_musaeus_live_markers(live_hunt.MUSAEUS_SRC) == tuple(_LIVE_MARKERS)

    def test_a_missing_source_raises_rather_than_falling_back(self, tmp_path):
        """A silent private copy is how one rule becomes two that disagree."""
        with pytest.raises(SystemExit):
            live_hunt.load_musaeus_live_markers(tmp_path / "nope.py")

    def test_a_renamed_marker_tuple_raises(self, tmp_path):
        src = tmp_path / "dupe_resolver.py"
        src.write_text("_SOMETHING_ELSE = ('live',)\n", encoding="utf-8")
        with pytest.raises(SystemExit):
            live_hunt.load_musaeus_live_markers(src)

    def test_a_non_literal_tuple_raises(self, tmp_path):
        """ast.literal_eval cannot evaluate a computed value, and guessing
        would be worse than stopping."""
        src = tmp_path / "dupe_resolver.py"
        src.write_text("_LIVE_MARKERS = tuple(x for x in ('live',))\n", encoding="utf-8")
        with pytest.raises((SystemExit, ValueError)):
            live_hunt.load_musaeus_live_markers(src)

    def test_no_private_marker_list_is_defined_in_this_file(self):
        """Guards the regression directly: someone 'fixing' a failure by
        pasting the markers in would satisfy every other test here."""
        src = Path(live_hunt.__file__).read_text(encoding="utf-8")
        assert "_LIVE_MARKERS = (" not in src, (
            "live_hunt must not define its own marker list -- read MUSAEUS's"
        )


class TestTheRebuiltRegex:
    def test_it_matches_musaeus_for_the_same_input(self):
        from musaeus.stages.dupe_resolver import _LIVE_RE

        mine = live_hunt.build_live_re(live_hunt.load_musaeus_live_markers(live_hunt.MUSAEUS_SRC))
        for t in (
            "Layla (Live at MTV Unplugged)",
            "Hotel California - live",
            "Rumours",
            "Live and Let Die",
            "Baba O'Riley in concert",
        ):
            assert bool(mine.search(t)) == bool(_LIVE_RE.search(t)), t

    def test_it_is_word_bounded(self):
        """'live' inside 'Oliver' or 'delivery' is not a live recording."""
        rx = live_hunt.build_live_re(("live", "concert"))
        assert not rx.search("Oliver's Army")
        assert not rx.search("Special Delivery")
        assert rx.search("Live at Leeds")
