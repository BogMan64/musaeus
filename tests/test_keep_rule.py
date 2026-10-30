"""Grey's keep rule, 2026-10-03, reordered 2026-10-22: studio over live above
all, then lossless (ALAC and FLAC equal) over lossy, original over remaster over a new mix,
then sample rate, then length (2 s or more); a tie keeps the library copy."""

from __future__ import annotations

import pytest

from musaeus.keep_rule import decide

BASE = {"codec": "alac", "title": "Song", "album": "Album", "sample_rate": 44100, "duration": 200.0}


def c(**kw):
    return {**BASE, **kw}


# A live copy has a length of its own: a copy at a live copy's length is that live
# take (batch 7, 2026-10-25; test_one_live_take_is_live_on_every_copy below).
@pytest.mark.parametrize(
    ("review", "library", "want"),
    [
        (c(codec="alac"), c(codec="flac"), ("tie", "")),
        (c(codec="flac", sample_rate=192000), c(), ("review", "quality")),
        # Linda Ronstadt, "Somewhere Out There": the 3:58 FLAC arrival lost to the
        # 3:07 ALAC library copy on format before length was asked (2026-10-22)
        (c(codec="flac", duration=238.2), c(duration=186.5), ("review", "length")),
        (
            c(codec="aac"),
            c(codec="flac", title="Song (Live)", duration=230.0),
            ("review", "studio/live"),
        ),
        (c(codec="aac"), c(codec="flac"), ("library", "format")),
        (
            c(title="Song (Live)", sample_rate=192000, duration=230.0),
            c(),
            ("library", "studio/live"),
        ),
        (c(), c(album="Unplugged", duration=230.0), ("review", "studio/live")),
        (c(title="Song (Remastered)", sample_rate=96000), c(), ("library", "original/remaster")),
        (c(), c(title="Song (2016 Remix)"), ("review", "original/remaster")),
        # Rolling Stones "Tumbling Dice": a new mix is further from the original
        # than a remaster of it (Grey, 2026-10-27: a remaster beats a new mix)
        (
            c(title="Tumbling Dice (2009 Mix)", duration=226.2),
            c(title="Tumbling Dice (2005 Digital Remaster)", duration=223.7),
            ("library", "original/remaster"),
        ),
        (
            c(title="Song (Remastered)"),
            c(title="Song (2016 Remix)"),
            ("review", "original/remaster"),
        ),
        # Diana Krall "The Best Thing For You": a 5.1 mix sent to stereo is a new
        # mix, so the original stereo copy wins over its 192 kHz (Grey, 2026-10-28)
        (
            c(title="The Best Thing For You (5.1 Mix)", sample_rate=192000),
            c(title="The Best Thing For You"),
            ("library", "original/remaster"),
        ),
        (c(title="Song (Surround Mix)", sample_rate=96000), c(), ("library", "original/remaster")),
        # Grey, 2026-10-30: single, radio, 7'' and film versions -- "album wins"
        (c(title="Song (Single Mix)"), c(), ("library", "original/remaster")),
        (c(title="Song (1965 Stereo Mix)"), c(), ("tie", "")),
        (c(title="Song (Original 1973 Stereo Mix)"), c(), ("tie", "")),
        (c(title="Song - Original Re-Mix (Stereo)"), c(), ("tie", "")),
        # "Original" in the same label, however nested (Codex review of #258, round 2) ...
        (c(title="Song (Original (Re-Mix))"), c(), ("tie", "")),
        # the whole label, after the marker too (round 3); "remix" is still a reissue word
        (c(title="Song (2019 Remix Original)"), c(title="Song (Remastered)"), ("tie", "")),
        (c(title="Song - 2019 Remix, Original"), c(title="Song (Remastered)"), ("tie", "")),
        # ... and only there: a song NAMED "Original Sin" can be remixed
        (
            c(title="Original Sin (2019 Remix)"),
            c(title="Original Sin"),
            ("library", "original/remaster"),
        ),
        # "Original" in the title never hides "Remixed" in the album (Codex review of #258)
        (
            c(title="Original (Remastered)"),
            c(title="Original", album="Remixed"),
            ("review", "original/remaster"),
        ),
        # Bach "Air", 192 kHz both: the piano arrangement is 39 s longer, and Grey
        # keeps the original scoring (2026-10-26: "A. Original")
        (
            c(title="BWV 1068 - II. Air On A G String (Arr. For Piano)", duration=330.8),
            c(title="BWV 1068 - II. Air", duration=292.4),
            ("library", "original/arrangement"),
        ),
        (c(title="Largo (Transcribed For Guitar)"), c(), ("library", "original/arrangement")),
        # Codex review of #256: a song NAMED "The Arrangement" is not an arrangement
        (
            c(title="The Arrangement (Arr. For Piano)", duration=330.0),
            c(title="The Arrangement"),
            ("library", "original/arrangement"),
        ),
        (
            c(title="The Arrangement (Remastered)", sample_rate=96000),
            c(title="The Arrangement"),
            ("library", "original/remaster"),
        ),
        # Round 3: an arrangement and a remaster are two steps, not one flag
        (
            c(title="Air (Arr. For Piano) (Remastered)", sample_rate=96000),
            c(title="Air (Arr. For Piano)"),
            ("library", "original/remaster"),
        ),
        (
            c(title="Air (Arr. For Piano)", sample_rate=96000),
            c(title="Air (Remastered)"),
            ("library", "original/arrangement"),
        ),
        # Round 4: a bracket inside the label hid it
        (
            c(title="Air (Arr. For Piano [2024])", duration=330.0),
            c(title="Air"),
            ("library", "original/arrangement"),
        ),
        # Round 5: the label's word came after the inner bracket
        (
            c(title="Air (For Piano [2024] Arr.)", duration=330.0),
            c(title="Air"),
            ("library", "original/arrangement"),
        ),
        # ... and a word after a CLOSED bracket is outside it
        (
            c(title="Air (2024) The Arrangement", sample_rate=96000),
            c(title="The Arrangement"),
            ("review", "quality"),
        ),
        # Round 2: an unclosed title bracket ran on into the album's name
        (
            c(title="Song (Arr. For Piano)", duration=330.0),
            c(title="Song (Original Mix", album="The Arrangement"),
            ("library", "original/arrangement"),
        ),
        (c(sample_rate=192000), c(), ("review", "quality")),
        (c(duration=202.0), c(), ("review", "length")),
        (c(duration=201.9), c(), ("tie", "")),
        (c(duration=197.5), c(), ("library", "length")),
        (c(), c(), ("tie", "")),
    ],
)
def test_the_order_grey_ruled(review, library, want):
    assert decide(review, library) == want


def test_one_live_take_is_live_on_every_copy():
    # Bruce Hornsby "The End Of The Innocence": the 192 kHz "(Live)" original and its
    # "(Remastered 2003)" copy, 438.7 s both; only the original says "Live" (batch 7).
    live = c(title="Song (Live)", sample_rate=192000, duration=438.7)
    remaster = c(title="Song (Remastered 2003)", duration=438.7)
    assert decide(live, remaster) == ("review", "original/remaster")


def test_bitrate_is_not_quality():
    """Two ALAC copies of one recording differ in bitrate by noise; a swap on
    that remakes every edition copy for nothing (AC/DC "Big Gun", 2026-10-03)."""
    assert decide(c(bitrate=1_100_000), c(bitrate=900_000)) == ("tie", "")


@pytest.mark.parametrize(
    "label",
    ["Jam", "Instrumental Version", "Acoustic", "Demo", "Alternate Take", "Outtake", "Rehearsal"],
)
def test_a_version_label_ranks_below_the_plain_original(label):
    """Grey, 2026-10-30: "Oh! Darling (Jam)" and "Hoogie Boogie (Instrumental
    Version)" were picked over the album and vocal versions; they rank lower."""
    plain = {"codec": "alac", "title": "Oh! Darling", "album": "Abbey Road",
             "sample_rate": 44100, "duration": 207.0}  # fmt: skip
    version = {**plain, "title": f"Oh! Darling ({label})", "duration": 319.0, "sample_rate": 96000}

    assert decide(version, plain) == ("library", "original/arrangement")


@pytest.mark.parametrize("title", ["Mystery Train (Master Take)", "Blue Moon (The Sun Sessions)"])
def test_a_master_take_or_the_sun_sessions_is_not_a_lower_version(title):
    plain = {"codec": "alac", "title": title.split(" (")[0], "album": "A",
             "sample_rate": 44100, "duration": 150.0}  # fmt: skip
    labelled = {**plain, "title": title, "sample_rate": 96000}

    assert decide(labelled, plain)[1] == "quality"


@pytest.mark.parametrize("credit", ["feat. Pearl Jam", "with The Jam", "ft. Acoustic Alchemy"])
def test_an_artist_credit_is_not_a_lower_version(credit):
    """CodeRabbit, review of #266: "(feat. Pearl Jam)" matched "jam"."""
    plain = {
        "codec": "alac",
        "title": "Song",
        "album": "A",
        "sample_rate": 44100,
        "duration": 200.0,
    }
    credited = {**plain, "title": f"Song ({credit})", "sample_rate": 96000}

    assert decide(credited, plain)[1] == "quality"


def test_a_copy_with_the_audio_of_a_labelled_demo_is_the_demo_too():
    """CodeRabbit, review of #266: an unlabelled copy of a demo's audio beat the
    original on quality; identical audio shares the version label."""
    from musaeus.stages.dupe_resolver import _rank

    original = {"codec": "alac", "title": "Song", "album": "A", "sample_rate": 44100,
                "duration": 200.0, "audio_hash": "orig", "finalized_at": "x", "id": 1}  # fmt: skip
    demo = {**original, "title": "Song (Demo)", "audio_hash": "demo", "id": 2}
    bare = {**original, "audio_hash": "demo", "sample_rate": 96000, "finalized_at": None, "id": 3}
    members = [bare, demo, original]

    _rank(members)

    assert members[0] is original


def test_a_label_after_a_credit_still_counts():
    """CodeRabbit, review of #266: "(feat. Jane - Acoustic Version)"."""
    plain = {
        "codec": "alac",
        "title": "Song",
        "album": "A",
        "sample_rate": 44100,
        "duration": 200.0,
    }
    labelled = {**plain, "title": "Song (feat. Jane - Acoustic Version)", "sample_rate": 96000}

    assert decide(labelled, plain) == ("library", "original/arrangement")


def test_an_empty_audio_hash_shares_no_label():
    """CodeRabbit, review of #266: two rows with an empty hash are not one recording."""
    from musaeus.stages.dupe_resolver import _share_version_label

    demo = {"title": "Song (Demo)", "audio_hash": ""}
    other = {"title": "Song", "audio_hash": ""}

    _share_version_label([demo, other])

    assert not other.get("lower_version")


_ALBUM = {"codec": "alac", "title": "My Love", "album": "FutureSex/LoveSounds",
          "sample_rate": 44100, "duration": 276.0}  # fmt: skip


@pytest.mark.parametrize(
    "label",
    ["Art Mix", "Steve Angello & Sebastian Ingrosso Mix", "Radio Edit", "Edit",
     "Single Version", "7'' Version", "Film Version", "Extended Version", "Club Mix",
     "Single Version - Mono Version"],
)  # fmt: skip
def test_an_edit_or_named_mix_ranks_below_the_album_version(label):
    """Grey, 2026-10-30: named mixes rank lower, like a remix; single, radio, 7''
    and film versions: "album wins"."""
    edit = {**_ALBUM, "title": f"My Love ({label})", "sample_rate": 96000, "duration": 613.0}

    assert decide(edit, _ALBUM) == ("library", "original/remaster")


@pytest.mark.parametrize(
    "title",
    ["My Love (Original Mix)", "My Love (Album Version)", "My Love (Stereo Mix)",
     "My Love (Mono)", 'My Love (From The Film "Pitch Perfect")', "Club Tropicana"],
)  # fmt: skip
def test_a_neutral_label_is_not_a_new_mix(title):
    other = {**_ALBUM, "title": title, "sample_rate": 96000}
    base = {**_ALBUM, "title": title.split(" (")[0]}

    assert decide(other, base)[1] == "quality"
