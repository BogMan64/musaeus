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
