"""AcoustID's title check reads "&" as "and" (Grey, 2026-10-12)."""

import pytest

from musaeus.stages.acousticid import _pick_matching_recording


def test_a_title_spelled_with_ampersand_agrees_with_the_word_and():
    recordings = [{"id": "r1", "title": "Jack and Diane", "artists": [{"name": "John Mellencamp"}]}]
    assert _pick_matching_recording(recordings, "John Mellencamp", "Jack & Diane") == "r1"


def test_a_title_that_is_only_an_ampersand_matches_nothing():
    # Review of #209: "&" alone became "and" and picked "Band On The Run".
    recordings = [{"id": "r1", "title": "Band On The Run", "artists": [{"name": "Wings"}]}]
    assert _pick_matching_recording(recordings, "Wings", "&") is None


def test_and_spelled_out_does_not_make_a_part_title_agree():
    # Codex review of #209: "Jack & Diane" read as "jackanddiane" contained a
    # recording called "And".
    recordings = [{"id": "r9", "title": "And", "artists": [{"name": "John Mellencamp"}]}]
    assert _pick_matching_recording(recordings, "John Mellencamp", "Jack & Diane") is None


def test_ampersand_still_agrees_with_and_as_part_of_a_longer_title():
    recordings = [{"id": "r1", "title": "Jack and Diane", "artists": [{"name": "John Mellencamp"}]}]
    want = "Jack & Diane (Remastered)"
    assert _pick_matching_recording(recordings, "John Mellencamp", want) == "r1"


def test_one_title_in_either_spelling_agrees():
    # Codex review of #209: "AT&T" and "ATandT" were refused.
    recordings = [{"id": "r1", "title": "ATandT", "artists": [{"name": "X"}]}]
    assert _pick_matching_recording(recordings, "X", "AT&T") == "r1"


def test_a_recording_called_and_is_not_part_of_and_spelled_out():
    # Codex review of #209: "Jack and Diane" still picked a recording "And".
    recordings = [{"id": "r9", "title": "And", "artists": [{"name": "John Mellencamp"}]}]
    assert _pick_matching_recording(recordings, "John Mellencamp", "Jack and Diane") is None


def test_an_artist_tag_that_lost_its_ampersand_is_the_same_act():
    # Codex review of #209: "Hall & Oates" read as "hallandoates" no longer
    # contained "halloates", so a true duplicate was rejected as a cover.
    from musaeus.stages.acousticid import _spellings

    mine, theirs = _spellings("Hall & Oates"), _spellings("Hall Oates")
    assert any(m in t or t in m for m in mine for t in theirs)


def test_a_song_called_and_still_matches_its_own_versions():
    # Codex review of #209: a file titled "And" never matched "And (Live)",
    # and the stage then stamped it checked.
    recordings = [{"id": "r1", "title": "And (Live)", "artists": [{"name": "X"}]}]
    assert _pick_matching_recording(recordings, "X", "And") == "r1"


@pytest.mark.parametrize(
    "want, recorded",
    [("And", "And - 2011 Remaster"), ("AT&T", "ATandT (Live)")],
)
def test_a_title_agrees_with_its_versions_in_either_spelling(want, recorded):
    # Codex review of #209.
    recordings = [{"id": "r1", "title": recorded, "artists": [{"name": "X"}]}]
    assert _pick_matching_recording(recordings, "X", want) == "r1"


@pytest.mark.parametrize(
    "want, recorded",
    [
        ("Song - Part 2", "Song - Part 1"),
        ("Shout - Part 2", "Shout - Part 1"),
        # Codex review of #216: the version rule swallowed "Part 1" before
        # "Live", and "songpart1" is a run of letters inside "songpart10".
        ("Song - Part 2 Live", "Song - Part 1 Live"),
        ("Song - Part 10", "Song - Part 1"),
        # Round two: a part is not the whole either.
        ("Song - Part 2", "Song"),
        # Round 3: a spelled-out part is a part too.
        ("Song - Part One", "Song"),
    ],
)
def test_another_part_of_a_song_is_not_its_version(want, recorded):
    # Codex review of #209 (after merge): the base title dropped any " - "
    # ending, so "Song - Part 1" was taken for "Song - Part 2" -- a wrong
    # recording id, and a pair that could be staged as one song twice.
    recordings = [{"id": "r1", "title": recorded, "artists": [{"name": "X"}]}]
    assert _pick_matching_recording(recordings, "X", want) is None


def test_a_version_ending_after_a_dash_still_agrees():
    recordings = [{"id": "r1", "title": "Slow Ride - 2016 Remaster", "artists": [{"name": "X"}]}]
    assert _pick_matching_recording(recordings, "X", "Slow Ride") == "r1"


def test_the_same_part_still_agrees_with_its_versions():
    recordings = [{"id": "r1", "title": "Song - Part 1 (Live)", "artists": [{"name": "X"}]}]
    assert _pick_matching_recording(recordings, "X", "Song - Part 1") == "r1"


@pytest.mark.parametrize(
    "want, recorded",
    [("Song - Part I", "Song - Part 1"), ("Song - Pt. 1", "Song - Part 1"),
     ("Song - Part Two", "Song - Part 2")],
)  # fmt: skip
def test_one_part_in_any_spelling_agrees(want, recorded):
    # Codex review of #216, round 3: a refused match is stamped checked and
    # never retried, so "Part I" must find "Part 1".
    recordings = [{"id": "r1", "title": recorded, "artists": [{"name": "X"}]}]
    assert _pick_matching_recording(recordings, "X", want) == "r1"


def test_one_part_in_another_spelling_is_picked_among_its_pressings():
    # Review of #216: the exact-title pick skipped the part spelling, so
    # "Pt. 2" beside a remaster of "Part 2" was a guess, refused for good.
    recordings = [
        {"id": "r1", "title": "Song, Part 2", "artists": [{"name": "X"}]},
        {"id": "r2", "title": "Song, Part 2 - 2011 Remaster", "artists": [{"name": "X"}]},
    ]
    assert _pick_matching_recording(recordings, "X", "Song, Pt. 2") == "r1"
