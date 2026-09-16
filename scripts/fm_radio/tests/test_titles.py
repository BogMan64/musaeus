"""Title matching, borrowed from MUSAEUS when it is importable.

These tests pass either way -- the point of the fallback is that the tool works
without MUSAEUS. What is asserted is the CONTRACT both implementations must
meet, plus the thing that actually went wrong: a fallback nobody could see.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fmradio import titles  # noqa: E402
from fmradio.titles import IMPLEMENTATION, _builtin_base_title, base_title, provenance  # noqa: E402


class TestContract:
    """True of whichever implementation is in force."""

    def test_version_qualifiers_do_not_split_a_song(self):
        want = base_title("Here I Am")
        assert base_title("Here I Am (2009 Remaster)") == want
        assert base_title("Here I Am - Live") == want

    def test_case_and_punctuation_folded(self):
        assert base_title("Rock 'n' Roll Train") == base_title("ROCK N ROLL TRAIN")

    def test_empty_and_none_are_safe(self):
        assert base_title("") == ""
        assert base_title(None) == ""  # type: ignore[arg-type]

    def test_two_different_songs_stay_different(self):
        assert base_title("Killer") != base_title("Killers")


class TestTheImprovementIsReal:
    """Why borrowing is worth the coupling."""

    def test_the_fallback_destroys_a_meaningful_bracket(self):
        """Air Supply, in the library: the bracket IS most of the title.
        Truncating at it collides with any other song called "Here I Am"."""
        crude = _builtin_base_title("Here I Am (Just When I Thought I Was Over You)")
        assert crude == "here i am"

    def test_musaeus_keeps_it(self):
        """Only meaningful when MUSAEUS is present; skipped otherwise rather
        than asserted away, so this file does not lie on a bare machine."""
        if IMPLEMENTATION != "musaeus":
            return
        kept = base_title("Here I Am (Just When I Thought I Was Over You)")
        assert kept != base_title("Here I Am")
        assert "thought" in kept


class TestTheFallbackIsVisible:
    def test_provenance_names_the_implementation_in_use(self):
        """THE BUG THIS GUARDS. The first attempt imported `base_title` from
        `musaeus.neardupe`. No such function exists, and neardupe lives at
        musaeus.stages.neardupe -- so a plain try/except ImportError would have
        used the crude version on every run, forever, while the README claimed
        an upgrade. The run header must state which rule is in force."""
        text = provenance()
        assert IMPLEMENTATION in ("musaeus", "builtin")
        if IMPLEMENTATION == "musaeus":
            assert "MUSAEUS" in text and "neardupe" in text
        else:
            assert "fallback" in text
            assert titles.IMPORT_ERROR, "a fallback must record WHY it fell back"
            assert titles.IMPORT_ERROR in text

    def test_the_import_target_actually_exists_here(self):
        """Not a contract test -- a canary. If MUSAEUS is on this machine and
        the import failed anyway, the private name has moved and the borrowing
        is silently dead."""
        import importlib.util

        if importlib.util.find_spec("musaeus") is None:
            return
        assert IMPLEMENTATION == "musaeus", (
            "MUSAEUS is importable but neardupe._normalise was not: "
            f"{titles.IMPORT_ERROR}"
        )
