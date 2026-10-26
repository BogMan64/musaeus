"""Of several recordings of one song, the take this file's length is (batch 6, 2026-10-25)."""

from musaeus.stages import acousticid as ac


def _rec(rid, duration=None):
    rec = {"id": rid, "title": "Feelin' Satisfied", "artists": [{"name": "Boston"}]}
    return {**rec, "duration": duration} if duration else rec


def test_the_recording_closest_in_length_is_this_file(monkeypatch):
    # Boston's studio "Feelin' Satisfied" (251.8 s) took a live bootleg with no
    # length, listed first of six recordings of the song.
    recordings = [_rec("boot"), _rec("long", 256.0), _rec("dlb", 252.026)]
    monkeypatch.setattr(
        ac, "_acousticid_query", lambda *a: [{"score": 0.98, "recordings": recordings}]
    )
    assert ac._acousticid_lookup("FP", 251.8, "k", "Boston", "Feelin' Satisfied")[0] == "dlb"


def test_with_no_length_known_the_order_stands():
    assert (
        ac._pick_matching_recording([_rec("a"), _rec("b")], "Boston", "Feelin' Satisfied", 251.8)
        == "a"
    )
