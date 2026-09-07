"""CorruptStage's relative fragment rule, ported from doctor 2026-09-07.

doctor REPORTS fragments; CorruptStage is what STOPS them at ingest, and it
was using an absolute floor. Measured against the 22 fragments doctor found
on the live library: CorruptStage caught 13 and missed 9 --

    over the 45s floor : Def Leppard 48s (beside a 4:27 copy), Eagles 47s,
                         Player 56s, Olivia Newton-John 57s, Mr. Mister 59s
    keyword exemption  : Elvis "Can't Help Falling in Love (Epic intro)"
                         at 21s beside a 2:57 copy; Eagles "(Reprise II)"

Raising MIN_DURATION_SEC is not the fix: a 120s floor flags 397 COMPLETE
recordings on this library -- "Hit the Road Jack" (2:00), "All Shook Up"
(1:58), "It's Not Unusual" (2:00).
"""

from __future__ import annotations

import pytest

from musaeus.stages.corrupt import (
    FRAGMENT_MAX_SEC,
    FRAGMENT_SIBLING_MIN_SEC,
    NEAR_ZERO_SEC,
    check_file,
)


@pytest.fixture
def track(tmp_path):
    """A file big enough that check 1 (size vs duration) never fires."""
    def _make(name: str, kib: int = 4096):
        p = tmp_path / name
        p.write_bytes(b"\0" * (kib * 1024))
        return p
    return _make


class TestNearZero:
    def test_a_one_second_file_is_caught(self, track):
        """ORPHEUS's floor was `< 1.0`, which let a file of exactly 1s
        through. Two real ones sat in the library because of it."""
        suspect, reason = check_file(track("x.m4a"), "alac", 1.0)
        assert suspect and "not a recording" in reason

    def test_no_keyword_can_excuse_it(self, track):
        """An "intro" of one second is still not an intro."""
        suspect, _ = check_file(track("Band - Song (intro).m4a"), "alac", 1.0)
        assert suspect

    def test_a_four_second_file_is_not_near_zero(self, track):
        """Four seconds is still caught -- by the pre-existing absolute
        rule, which is right. What matters is that it is NOT reported as
        "not a recording": the boundary belongs where NEAR_ZERO_SEC puts it.

        (An earlier version of this test asserted `not suspect` and failed
        for an unrelated reason -- the absolute <45s rule -- which would
        have made it look like the new check was broken.)"""
        suspect, reason = check_file(track("x.m4a"), "alac", 4.0)
        assert suspect
        assert "not a recording" not in reason
        assert "suspiciously short" in reason


class TestRelativeToASibling:
    def test_a_clip_beside_a_full_copy_is_caught(self, track):
        """The Def Leppard case: 48s is over the absolute floor, and a 4:27
        copy of the same recording makes it obviously a clip."""
        suspect, reason = check_file(track("x.m4a"), "alac", 48.0, 267.0)
        assert suspect and "another copy" in reason

    def test_a_keyword_does_not_excuse_it(self, track):
        """The Elvis case. The absolute rule exempts anything whose title
        says "intro"; a 2:57 copy of the same recording is evidence the
        title cannot override."""
        suspect, _ = check_file(
            track("Elvis Presley - Can't Help Falling in Love (Epic intro).m4a"),
            "alac", 21.0, 177.0)
        assert suspect

    def test_no_sibling_means_no_opinion(self, track):
        """A 48s track with nothing to compare against is left alone --
        this is what stops the rule behaving like a raised floor."""
        suspect, _ = check_file(track("x.m4a"), "alac", 48.0, 0.0)
        assert not suspect

    def test_a_short_sibling_is_not_evidence(self, track):
        """Simon & Garfunkel's "Bookends Theme" is 33s and 83s. Both real;
        neither is long enough to accuse the other, so the RELATIVE rule
        must stay silent. The absolute <45s rule still fires on a 33s file
        and always did -- that is a separate, older judgement, and this test
        is careful to check the reason rather than the boolean."""
        suspect, reason = check_file(track("bookends.m4a"), "alac", 33.0, 83.0)
        assert "another copy" not in reason

    def test_a_long_clip_with_a_short_sibling_is_left_alone(self, track):
        """Above the absolute floor, so only the relative rule could fire --
        and an 83s sibling is not long enough to justify it."""
        suspect, reason = check_file(track("reprise.m4a"), "alac", 50.0, 83.0)
        assert not suspect, reason

    def test_a_complete_two_minute_song_is_never_flagged(self, track):
        """The 397-false-positive case, pinned. Even with a longer copy
        present, a 2-minute recording is over the ceiling."""
        suspect, _ = check_file(track("hit the road jack.m4a"), "alac", 120.0, 300.0)
        assert not suspect

    def test_the_bounds_stay_in_the_ratio_they_assume(self):
        """The module asserts this at import; state it here too so a change
        to either constant fails a named test rather than an import."""
        assert FRAGMENT_MAX_SEC * 2 <= FRAGMENT_SIBLING_MIN_SEC
        assert NEAR_ZERO_SEC < FRAGMENT_MAX_SEC


class TestLongestSibling:
    """The row must not be its own sibling.

    Found by mutation testing: making _longest_sibling return the map value
    unconditionally broke nothing in the suite, because every other test
    passes the sibling length in directly. A 300s file would then report a
    300s "sibling" -- harmless here, but the same slip on a 20s clip whose
    only copy is itself would invent evidence that does not exist.
    """

    def _map(self, artist, title, seconds):
        from musaeus.doctor import song_key
        return {song_key(artist, title): seconds}

    def test_the_only_copy_has_no_sibling(self):
        from musaeus.stages.corrupt import _longest_sibling
        row = {"artist": "A", "title": "T", "duration": 300.0}
        assert _longest_sibling(row, self._map("A", "T", 300.0)) == 0.0

    def test_a_longer_copy_is_a_sibling(self):
        from musaeus.stages.corrupt import _longest_sibling
        row = {"artist": "A", "title": "T", "duration": 20.0}
        assert _longest_sibling(row, self._map("A", "T", 300.0)) == 300.0

    def test_an_unknown_recording_has_no_sibling(self):
        from musaeus.stages.corrupt import _longest_sibling
        row = {"artist": "A", "title": "T", "duration": 20.0}
        assert _longest_sibling(row, {}) == 0.0

    def test_a_row_with_no_duration_is_safe(self):
        """duration can be NULL; `or 0.0` must hold or this raises."""
        from musaeus.stages.corrupt import _longest_sibling
        row = {"artist": "A", "title": "T", "duration": None}
        assert _longest_sibling(row, self._map("A", "T", 300.0)) == 300.0
