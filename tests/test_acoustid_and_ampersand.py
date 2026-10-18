"""AcoustID's title check reads "&" as "and" (Grey, 2026-10-12)."""

from musaeus.stages.acousticid import _pick_matching_recording


def test_a_title_spelled_with_ampersand_agrees_with_the_word_and():
    recordings = [{"id": "r1", "title": "Jack and Diane", "artists": [{"name": "John Mellencamp"}]}]
    assert _pick_matching_recording(recordings, "John Mellencamp", "Jack & Diane") == "r1"
