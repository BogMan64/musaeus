"""A duet files under its primary artist. A band does not get taken apart.

"Johnny Mathis & Deniece Williams" made a one-track folder of its own. It
belongs under Johnny Mathis, while the TAG keeps the full credit -- three
fields, three jobs: the tag names who performed, the folder decides where it
sits.

The difficulty is that "&" both joins two artists AND lives inside band
names. Splitting on it blindly turns Simon & Garfunkel into Simon.

Evidence used, strongest first:
  1. MusicBrainz resolved the whole string -> one entity, never split
  2. feat./featuring/ft./with -> always a collaboration
  3. "& The ...", "& His ..." -> the band's own name
  4. otherwise split only when the tail is a full personal name (2+ words)

Measured against the real library on 2026-09-22: mb_artist_name was recorded
for Simon & Garfunkel, Sam & Dave, Ike & Tina Turner, Jr. Walker & The All
Stars and Harold Melvin & The Blue Notes, and absent for the two duets. Hall
& Oates is the case that proves rule 4 earns its place -- a real band with no
MusicBrainz entry, saved only because "Oates" is a single word.
"""

import pytest

from musaeus.artist_form import folder_artist, sort_form


@pytest.mark.parametrize(
    "credit,mb,expected",
    [
        # duets -> file under the primary artist
        ("Johnny Mathis & Deniece Williams", None, "Johnny Mathis"),
        ("Brook Benton & Dinah Washington", None, "Brook Benton"),
        # explicit collaboration markers
        ("Calvin Harris feat. Future", None, "Calvin Harris"),
        ("DJ Khaled featuring Justin Bieber", None, "DJ Khaled"),
        ("Elton John with Dua Lipa", None, "Elton John"),
        ("Drake ft. Rihanna", None, "Drake"),
    ],
)
def test_a_collaboration_files_under_the_primary_artist(credit, mb, expected):
    assert folder_artist(credit, mb) == expected


@pytest.mark.parametrize(
    "band,mb",
    [
        ("Harold Melvin & The Blue Notes", "Harold Melvin & The Blue Notes"),
        ("Jr. Walker & The All Stars", "Jr. Walker & The All Stars"),
        ("Sly & The Family Stone", None),
        ("Lucky Millinder & His Orchestra", None),
        ("Simon & Garfunkel", "Simon & Garfunkel"),
        ("Sam & Dave", "Sam & Dave"),
        ("Ike & Tina Turner", "Ike & Tina Turner"),
        ("Hall & Oates", None),
        ("The Beatles", None),
    ],
)
def test_a_band_is_never_taken_apart(band, mb):
    assert folder_artist(band, mb) == band


def test_musicbrainz_agreement_outranks_every_other_rule():
    """A resolved MB name means one entity, whatever the string looks like."""
    assert folder_artist("Ike & Tina Turner", "Ike & Tina Turner") == "Ike & Tina Turner"


def test_a_single_word_tail_is_never_split():
    """Hall & Oates has no MusicBrainz entry here and must still survive."""
    assert folder_artist("Hall & Oates", None) == "Hall & Oates"


def test_the_result_still_sorts():
    """Whatever comes out is still fed through sort_form for the path."""
    assert sort_form(folder_artist("Johnny Mathis & Deniece Williams", None)) == "Johnny Mathis"
    assert sort_form(folder_artist("The Beatles", None)) == "Beatles, The"


def test_empty_and_odd_input_is_returned_unchanged():
    for odd in ("", "&", " & ", "Artist &"):
        assert folder_artist(odd, None) == odd
