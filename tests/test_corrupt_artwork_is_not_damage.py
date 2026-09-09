"""Broken cover art is not broken audio.

2026-09-08. scripts/decode_audit.py was three hours into the first
whole-library decode sweep and had reported three files as damaged:

    Andy Gibb      - (Love Is) Thicker Than Water
    Baltimora      - Tarzan Boy
    Chamillionaire - Ridin'

All three decode their full ALAC stream with exit 0. What they have in
common is a malformed embedded JPEG, and `ffmpeg -i file -f null -` decodes
EVERY stream in the container -- so the artwork's decoder wrote to stderr,
and `if proc.stderr.strip(): return False` counted that as damage.

Two things were wrong, and both are fixed:

  - the command asked ffmpeg to decode the artwork at all (now `-vn`)
  - the verdict treated any stderr as an audio fact (now classified)

`-vn` alone is not enough. The mjpeg header is parsed at demux time, before
stream selection applies, so two lines survive it -- plain `ffprobe` prints
them too. Both mechanisms are needed and both are tested here.

The stderr strings below are verbatim captures from those files, not
invented ones. That is deliberate: a synthetic fixture proves the code
handles what its author imagined, and the whole point of this incident is
that its author had not imagined artwork.

Why the classifier is a DENYLIST -- drop known image-decoder lines, keep
everything else -- is the load-bearing decision, and the last test here is
the one that pins it. An unrecognised image codec leaks through as a false
positive: it lands in the review CSV and a human rules on it. An allowlist
of known audio errors would fail the other way -- an unrecognised audio
error would be dropped, and a damaged master would be baked into an
edition. Fail towards the human.
"""

from __future__ import annotations

from musaeus.stages.corrupt import audio_relevant_stderr, ffmpeg_decode_check

# Verbatim, from `ffmpeg -v error -i "Andy Gibb - (Love Is) Thicker Than
# Water.m4a" -f null -` on 2026-09-08.
ARTWORK_ONLY = (
    "[mjpeg @ 0x55f3a41617c0] unable to decode APP fields: "
    "Invalid data found when processing input\n"
    "[mjpeg @ 0x55f3a41617c0] bits 86 is invalid\n"
    "[mjpeg @ 0x55f3a4163300] error count: 64\n"
    "[mjpeg @ 0x55f3a4163300] error y=23 x=32\n"
    "[mjpeg @ 0x55f3a4163300] invalid id 19\n"
    "Error while decoding stream #0:1: Invalid data found when processing input\n"
    "Too many packets buffered for output stream 0:1."
)

# Verbatim, from the CORRUPT_DETECTED event of 2026-09-03 for
# `The Irish Rovers - Wasn't That a Party.flac`. This one is real damage and
# was correctly deleted; it must stay caught.
REAL_AUDIO_DAMAGE = (
    "[flac @ 0x557509a42940] invalid residual\n"
    "[flac @ 0x557509a42940] decode_frame() failed\n"
    "Error while decoding stream #0:0: Invalid data found when processing input"
)


def test_artwork_errors_alone_are_not_damage() -> None:
    assert audio_relevant_stderr(ARTWORK_ONLY, audio_index=0) == ""


def test_real_audio_damage_survives_the_filter() -> None:
    kept = audio_relevant_stderr(REAL_AUDIO_DAMAGE, audio_index=0)
    assert "invalid residual" in kept
    assert "decode_frame() failed" in kept
    # The stream-attributed line names the AUDIO stream, so it is kept too.
    assert "stream #0:0" in kept


def test_damaged_audio_in_a_file_that_also_has_broken_artwork() -> None:
    """The case that would be lost by over-filtering."""
    both = ARTWORK_ONLY + "\n" + REAL_AUDIO_DAMAGE
    kept = audio_relevant_stderr(both, audio_index=0)
    assert "invalid residual" in kept
    assert "mjpeg" not in kept


def test_a_stream_error_naming_the_artwork_is_dropped() -> None:
    line = "Error while decoding stream #0:1: Invalid data found when processing input"
    assert audio_relevant_stderr(line, audio_index=0) == ""


def test_a_stream_error_naming_the_audio_is_kept() -> None:
    line = "Error while decoding stream #0:1: Invalid data found when processing input"
    # Same line, but here stream 1 IS the audio -- artwork-first containers exist.
    assert audio_relevant_stderr(line, audio_index=1) == line


def test_a_stream_error_is_kept_when_the_audio_stream_is_unknown() -> None:
    """ffprobe failing must not turn into a silent acquittal."""
    line = "Error while decoding stream #0:1: Invalid data found when processing input"
    assert audio_relevant_stderr(line, audio_index=None) == line


def test_the_decode_command_does_not_ask_for_the_artwork(monkeypatch) -> None:
    """-vn is what removes the muxer's 'Too many packets buffered' complaint.

    Asserted on the command rather than on an output, because the line it
    suppresses cannot be produced without a file that has broken artwork.
    """
    seen: list[list[str]] = []

    class _Proc:
        returncode = 0
        stderr = ""

    def fake_run(cmd, **kw):
        seen.append(cmd)
        return _Proc()

    monkeypatch.setattr("musaeus.stages.corrupt.subprocess.run", fake_run)
    ffmpeg_decode_check(__import__("pathlib").Path("/nonexistent.m4a"))
    assert seen, "ffmpeg was never invoked"
    assert "-vn" in seen[0]


def test_an_unrecognised_decoder_is_not_silently_forgiven() -> None:
    """The denylist must fail towards the human, not towards the encoder.

    If this test is ever changed to assert the opposite, read the module
    docstring first: an allowlist here means an unknown audio error becomes
    an acquittal, and the file it acquits gets baked.
    """
    line = "[some_future_codec @ 0xdeadbeef] something nobody has seen yet"
    assert audio_relevant_stderr(line, audio_index=0) == line
