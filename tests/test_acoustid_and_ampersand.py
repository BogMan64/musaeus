"""AcoustID's title check reads "&" as "and" (Grey, 2026-10-12)."""

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
