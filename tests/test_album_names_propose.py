"""Album-name proposal: pure logic.

Ported from ~/Desktop/POST.Code/ on 2026-09-14 when the script moved into
the repository. The Spotify cases became MusicBrainz cases -- the tier names
never mentioned a source, so only the fixtures changed.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts" / "album_names"))

from propose_album_names import (
    Answer,
    _choose,
    _is_single_release,
    _year_of,
    fold,
    verdict,
)


class TestFold:
    def test_case_insensitive(self):
        assert fold("Hello World") == fold("hello world")

    def test_accents_are_folded(self):
        assert fold("Beyoncé") == fold("Beyonce")

    def test_punctuation_is_stripped(self):
        assert fold("Rock & Roll") == fold("Rock and Roll".replace("and", "&"))
        assert fold("Don't Stop") == fold("Dont Stop")

    def test_strip_noise_removes_featured_artist_clause(self):
        assert fold("Candy Shop (feat. Olivia)", strip_noise=True) == fold(
            "Candy Shop", strip_noise=True
        )

    def test_strip_noise_removes_remaster_qualifier(self):
        assert fold("Voices Carry (2015 Remaster)", strip_noise=True) == fold(
            "Voices Carry", strip_noise=True
        )

    def test_strip_noise_removes_bare_single_suffix(self):
        assert fold("Broken Strings - Single", strip_noise=True) == fold(
            "Broken Strings", strip_noise=True
        )

    def test_without_strip_noise_the_clause_is_kept(self):
        assert fold("Song (Live)") != fold("Song")


class TestYearOf:
    def test_extracts_leading_year(self):
        assert _year_of("1969-09-26") == 1969
        assert _year_of("1971") == 1971

    def test_junk_date_sorts_last(self):
        assert _year_of("unknown") == 9999
        assert _year_of("") == 9999
        assert _year_of("unknown") > _year_of("2024")


class TestIsSingleRelease:
    def test_identical_title_and_collection_is_a_single(self):
        assert _is_single_release("Candy Shop", "Candy Shop") is True

    def test_collection_with_feat_clause_matching_track_is_a_single(self):
        assert _is_single_release("Candy Shop", "Candy Shop (feat. Olivia)") is True

    def test_a_real_album_is_not_a_single(self):
        assert _is_single_release("Candy Shop", "The Massacre") is False

    def test_empty_collection_is_not_a_single(self):
        assert _is_single_release("Candy Shop", "") is False

    def test_a_much_longer_collection_starting_with_the_track_is_not_a_single(self):
        assert (
            _is_single_release("Rock", "Rock and Roll Over: The Complete Sessions")
            is False
        )


class TestSelfTitledFallback:
    def test_falls_back_to_self_titled_album_when_no_distinct_album_exists(self):
        cands = [("Iron Maiden", "Iron Maiden", "1980", "itunes")]
        result = _choose(cands)
        assert result.album == "Iron Maiden"
        assert result.is_self_titled_fallback is True

    def test_prefers_distinct_album_over_self_titled(self):
        cands = [
            ("Candy Shop", "Candy Shop", "2005", "itunes"),
            ("Candy Shop", "The Massacre", "2005", "itunes"),
        ]
        result = _choose(cands)
        assert result.album == "The Massacre"
        assert result.is_self_titled_fallback is False


class TestVerdict:
    def test_agreement_between_sources_is_the_top_tier(self):
        it = Answer(album="The Massacre", year="2005", source="itunes")
        dz = Answer(album="The Massacre", year="", source="deezer")
        confidence, album, note, _winner = verdict(it, dz)
        assert confidence == "1-AGREED"
        assert album == "The Massacre"
        # v2 returned "" here. v3 names the sources that agreed, so the CSV
        # says WHY a row is top-tier instead of asking you to take it on faith.
        assert "itunes" in note and "deezer" in note

    def test_agreement_is_accent_and_case_insensitive(self):
        it = Answer(album="Beyonce", source="itunes")
        dz = Answer(album="Beyoncé", source="deezer")
        confidence, _album, _note, _winner = verdict(it, dz)
        assert confidence == "1-AGREED"

    def test_disagreement_keeps_itunes_and_explains_why(self):
        it = Answer(album="The Massacre", year="2005", source="itunes")
        dz = Answer(album="Get Rich or Die Tryin'", year="", source="deezer")
        confidence, album, note, _winner = verdict(it, dz)
        assert confidence == "3-SOURCES DISAGREE"
        assert album == "The Massacre", "iTunes stays the tie-break winner"
        # v2's note explained the year tie-break; v3's explains the source
        # choice instead. Either way the note must say which source won and
        # what it proposed -- a disagreement the CSV does not explain is a
        # row the reviewer cannot rule on.
        assert "itunes" in note.lower()
        assert "The Massacre" in note

    def test_itunes_only_is_a_distinct_tier_from_agreement(self):
        it = Answer(album="El Dorado", source="itunes")
        dz = Answer()
        confidence, album, _note, _winner = verdict(it, dz)
        assert confidence == "2-ITUNES ONLY"
        assert album == "El Dorado"

    def test_deezer_only_is_a_distinct_tier_from_agreement(self):
        it = Answer()
        dz = Answer(album="Some Album", source="deezer")
        confidence, album, _note, _winner = verdict(it, dz)
        assert confidence == "2-DEEZER ONLY"
        assert album == "Some Album"

    def test_neither_source_answering_is_its_own_tier_not_a_crash(self):
        confidence, album, _note, _winner = verdict(Answer(), Answer())
        assert confidence == "4-NO ANSWER"
        assert album == ""


class TestVerdictThreeSource:
    def test_three_way_agreement(self):
        it = Answer(album="The Massacre", year="2005", source="itunes")
        dz = Answer(album="The Massacre", source="deezer")
        sp = Answer(album="The Massacre", year="2005", source="musicbrainz")
        confidence, album, _note, winner = verdict(it, dz, sp)
        assert confidence == "1-AGREED"
        assert album == "The Massacre"
        assert winner.source in ("itunes", "musicbrainz")

    def test_two_out_of_three_agreement(self):
        it = Answer(album="The Massacre", source="itunes")
        dz = Answer(album="Get Rich or Die Tryin'", source="deezer")
        sp = Answer(album="The Massacre", source="musicbrainz")
        confidence, album, note, winner = verdict(it, dz, sp)
        assert confidence == "1-AGREED"
        assert album == "The Massacre"
        assert "itunes" in note and "musicbrainz" in note

    def test_musicbrainz_only(self):
        it = Answer()
        dz = Answer()
        sp = Answer(album="El Dorado", source="musicbrainz")
        confidence, album, _note, winner = verdict(it, dz, sp)
        assert confidence == "2-MUSICBRAINZ ONLY"
        assert album == "El Dorado"
        assert winner.source == "musicbrainz"

    def test_disagreement_defaults_priorities(self):
        it = Answer(album="Album A", source="itunes")
        dz = Answer(album="Album B", source="deezer")
        sp = Answer(album="Album C", source="musicbrainz")
        confidence, album, _note, winner = verdict(it, dz, sp)
        assert confidence == "3-SOURCES DISAGREE"
        assert album == "Album A"


class TestFiveSourcesAndTheTieBreak:
    """Five sources, agreement still at two, and originals beat compilations.

    Grey's call 2026-09-15: add Last.fm and Discogs, "settle on two that
    match". The bar stays at two -- more sources raise the CHANCE of reaching
    two, they do not change what two means.
    """

    def test_two_agreeing_is_still_the_top_tier_with_five_asked(self):
        got = verdict(
            Answer(album="Rumours", source="itunes"),
            Answer(album="", source="deezer"),
            Answer(album="", source="musicbrainz"),
            Answer(album="Rumours", source="lastfm"),
            Answer(album="", source="discogs"),
        )
        assert got[0] == "1-AGREED"
        assert got[1] == "Rumours"

    def test_a_single_source_names_itself(self):
        c, album, _n, _w = verdict(Answer(album="Men In Blues", source="discogs"))
        assert c == "2-DISCOGS ONLY"
        assert album == "Men In Blues"

    def test_a_named_compilation_loses_to_an_original(self):
        """Measured on Redbone: iTunes offered a mixtape, another source the
        record. iTunes-first is a tie-break between EQUALS, and a compilation
        is not the equal of the album a song was released on."""
        _c, album, _n, w = verdict(
            Answer(album="Greatest Hits", source="itunes"),
            Answer(album="Wovoka", source="lastfm"),
        )
        assert album == "Wovoka"
        assert w.source == "lastfm"

    def test_when_every_answer_is_a_compilation_itunes_still_wins(self):
        _c, album, _n, w = verdict(
            Answer(album="Greatest Hits", source="itunes"),
            Answer(album="Gold", source="deezer"),
        )
        assert w.source == "itunes"
        assert album == "Greatest Hits"

    def test_when_no_answer_is_a_compilation_itunes_still_wins(self):
        _c, _album, _n, w = verdict(
            Answer(album="Rumours", source="itunes"),
            Answer(album="Tusk", source="deezer"),
        )
        assert w.source == "itunes"

    def test_the_filter_only_sees_compilations_that_say_so(self):
        """The documented limit, pinned so nobody assumes more than it does.

        "Rock Lobster (stereo)" 2026-09-15: iTunes '1979', Deezer 'Time
        Capsule', Last.fm "The B-52's" -- the last is the real album and the
        first two are compilations whose NAMES do not say so. The tie-break
        cannot see that, so iTunes wins and the answer is wrong. Fixing it
        needs release-type metadata, not a better regex.
        """
        _c, album, _n, w = verdict(
            Answer(album="1979", source="itunes"),
            Answer(album="Time Capsule", source="deezer"),
            Answer(album="The B-52's", source="lastfm"),
        )
        assert album == "1979", "known limit: an unnamed compilation is invisible here"
        assert w.source == "itunes"


class TestEarliestYearPrefersTheOriginal:
    """Borrowed from beets' `original_date`, 2026-09-16.

    Claude chat reviewed beets and found this the one direct hit on our
    compilation problem: beets separates "which release matched" from "what
    date should this carry", and can pin to the earliest. A compilation or
    reissue is almost always LATER than the record a song first appeared on,
    so among answers that are not named compilations, the earliest dated one
    is the better guess at the original.

    Applied after the named-compilation filter and before Grey's iTunes
    ruling, because iTunes-first is a tie-break between EQUALS.
    """

    def test_an_older_answer_beats_a_reissue(self):
        _c, album, _n, w = verdict(
            Answer(album="Greatest Hits 2011", year="2011", source="itunes"),
            Answer(album="Wovoka", year="1973", source="deezer"),
        )
        assert album == "Wovoka"
        assert w.source == "deezer", "the ruling yields to an older original"

    def test_an_undated_answer_cannot_win_by_saying_nothing(self):
        """A missing year is not evidence of being early. Treating it as 0
        would let the least informative source win every tie."""
        _c, album, _n, w = verdict(
            Answer(album="Rumours", year="1977", source="itunes"),
            Answer(album="Some Bootleg", year="", source="deezer"),
        )
        assert album == "Rumours"
        assert w.source == "itunes"

    def test_with_only_one_dated_answer_the_itunes_ruling_stands(self):
        _c, _album, _n, w = verdict(
            Answer(album="A", year="1999", source="itunes"),
            Answer(album="B", year="", source="deezer"),
        )
        assert w.source == "itunes"

    def test_equal_years_fall_back_to_the_itunes_ruling(self):
        _c, album, _n, w = verdict(
            Answer(album="First", year="1980", source="itunes"),
            Answer(album="Second", year="1980", source="deezer"),
        )
        assert w.source == "itunes"
        assert album == "First"

    def test_a_named_compilation_still_loses_even_when_older(self):
        """Order matters: the compilation filter runs BEFORE the year test,
        so an old compilation does not beat a newer original."""
        _c, album, _n, _w = verdict(
            Answer(album="Greatest Hits", year="1970", source="itunes"),
            Answer(album="Wovoka", year="1973", source="deezer"),
        )
        assert album == "Wovoka"
