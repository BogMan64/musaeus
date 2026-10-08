"""Findings 7 and 8 of the review of #86 (2026-10-07), in musaeus/keep_rule.py.

7. decide() -- the swap tool's judge -- skipped the shared-loudness step the
   resolver applies. A review copy with the SAME audio as a master measured at
   -18 LUFS, but unmeasured itself, "looked original" and won: the shape of the
   2026-09-26 incident, 119 masters swapped for identical copies of themselves.
8. The lossy list missed codecs (ac3, eac3, wmav1, musepack, amr...), so such a
   copy ranked as "other lossless" and beat a real AAC master, which canonicalize
   then re-encoded. Now only named lossless codecs rank as lossless.
"""

from __future__ import annotations

from musaeus.keep_rule import decide


def _copy(**kw):
    base = {"codec": "alac", "title": "Song", "album": "Album", "sample_rate": 44100,
            "duration": 200.0, "audio_hash": "H", "lufs": None}  # fmt: skip
    return {**base, **kw}


def test_an_unmeasured_copy_of_a_baked_master_does_not_win():
    master = _copy(lufs=-18.0)  # the master, measured: a baked copy
    review = _copy(lufs=None)  # the same audio, not measured yet

    verdict, step = decide(review, master)

    assert verdict != "review", f"an identical baked copy replaced the master ({step})"


def test_an_unmeasured_original_still_beats_a_baked_master():
    master = _copy(lufs=-18.0)
    review = _copy(lufs=None, audio_hash="OTHER")  # different audio: a real original

    assert decide(review, master)[0] == "review"


def test_a_lossy_codec_never_counts_as_lossless():
    for codec in ("ac3", "eac3", "wmav1", "musepack8", "amr_nb", "some_new_codec"):
        verdict, step = decide(_copy(codec=codec), _copy(codec="aac"))
        assert verdict != "review", f"{codec} beat an AAC master at {step!r}"


def test_named_lossless_codecs_still_beat_lossy():
    for codec in ("alac", "flac", "wavpack", "pcm_s16le"):
        assert decide(_copy(codec=codec), _copy(codec="aac"))[0] == "review"
