"""Does the heuristic reproduce judgements Grey has already made by hand?

That is the only test that matters. The wishlist entry says this is "not a new
capability so much as the automation of a judgement Grey has already made a
dozen times", and names three: Jan & Dean's instrumental version, the Everly
Brothers' single-versus-2006-remaster, and the "(2011 Remaster)" calls. Those
are the fixtures below.

The scorer is pure, so every one of these runs offline in milliseconds. No
network, no cassettes, no rate limit. That is the reason for splitting the
heuristic away from the clients: the part that can be wrong in an interesting
way is the part that needs no I/O to exercise.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fmradio.model import Candidate  # noqa: E402
from fmradio.score import CONFIDENT_MARGIN, rank  # noqa: E402


def C(**kw) -> Candidate:
    return Candidate(**kw)


class TestTheThreeJudgementsGreyHasAlreadyMade:
    def test_the_2011_remaster_loses_to_the_original_single(self):
        """The call Grey has made repeatedly. The remaster is a Single too, and
        may even be the most played, so no single signal settles it -- the
        first-release-date is what decides."""
        original = C(
            title="Won't Get Fooled Again",
            release_group_type="Single",
            first_release_date="1971-06-25",
            length_seconds=210,
            media_format="Vinyl",
            listen_count=40_000,
        )
        remaster = C(
            title="Won't Get Fooled Again (2011 Remaster)",
            disambiguation="2011 remaster",
            release_group_type="Single",
            first_release_date="2011-03-01",
            length_seconds=210,
            media_format="Digital Media",
            listen_count=95_000,
        )
        p = rank("Who, The", "Won't Get Fooled Again", [original, remaster])
        assert p.best.candidate is original, p.best.explanation
        # The remaster must lose FOR THE RIGHT REASON. Without this the test
        # would pass on any ordering accident.
        loser = next(v for v in p.verdicts if v.candidate is remaster)
        assert any("remaster" in r and r.startswith("-") for r in loser.reasons), (
            f"the remaster marker did not fire: {loser.explanation}"
        )
        assert any("most played" in r for r in loser.reasons), (
            "the remaster genuinely is the most played here; if that signal "
            "stopped firing this test is no longer testing the hard case"
        )

    def test_the_instrumental_loses_even_when_it_is_the_only_early_pressing(self):
        """Jan & Dean's instrumental. Deliberately given the STRONGER position
        on date and type, so the test proves the instrumental penalty can
        overcome them rather than merely agreeing with them."""
        instrumental = C(
            title="Baby Talk (Instrumental)",
            release_group_type="Single",
            first_release_date="1959",
            length_seconds=140,
        )
        vocal = C(
            title="Baby Talk",
            release_group_type="Album",
            first_release_date="1960",
            length_seconds=145,
        )
        p = rank("Jan & Dean", "Baby Talk", [instrumental, vocal])
        assert p.best.candidate is vocal, p.best.explanation

    def test_an_instrumental_with_no_vocal_sibling_is_NOT_penalised(self):
        """The other side of that rule, and the reason it is relative rather
        than a flat penalty. Instrumentals genuinely were radio hits --
        "Green Onions", "Telstar", "Apache" -- and Grey's own MasterLaw keeps
        "Mort Stevens & His Orchestra, Hawaii Five-O" under Surf Rock. A flat
        instrumental penalty would rank the real hit below nothing at all."""
        only = C(
            title="Hawaii Five-O",
            release_group_type="Single",
            first_release_date="1969",
            length_seconds=118,
            media_format="Vinyl",
        )
        p = rank("Mort Stevens & His Orchestra", "Hawaii Five-O", [only])
        assert not p.verdicts[0].excluded
        assert not any(
            "instrumental" in r and r.startswith("-") for r in p.verdicts[0].reasons
        ), p.verdicts[0].explanation

    def test_the_instrumental_variant_loses_but_is_not_excluded(self):
        """It stays in the ranking with its reason recorded, because it may be
        the only copy Grey holds. Propose, do not decide."""
        instrumental = C(title="Song (Instrumental)", first_release_date="1962")
        vocal = C(title="Song", first_release_date="1962")
        p = rank("Artist", "Song", [instrumental, vocal])
        loser = next(v for v in p.verdicts if v.candidate is instrumental)
        assert not loser.excluded
        assert any("vocal pressing of this song exists" in r for r in loser.reasons)

    def test_the_single_beats_a_2006_remaster_of_the_same_length(self):
        """The Everly Brothers case: same recording, same length, and only the
        date and the remaster marker separate them."""
        single = C(
            title="Cathy's Clown",
            release_group_type="Single",
            first_release_date="1960-04",
            length_seconds=142,
            media_format="Vinyl",
        )
        remaster = C(
            title="Cathy's Clown",
            disambiguation="2006 remastered",
            release_group_type="Album",
            secondary_types=("Compilation",),
            first_release_date="2006",
            length_seconds=142,
        )
        p = rank("Everly Brothers, The", "Cathy's Clown", [single, remaster])
        assert p.best.candidate is single
        assert p.margin >= CONFIDENT_MARGIN
        assert p.confident


class TestTheAlbumCutVersusTheSingleEdit:
    def test_the_seven_minute_album_cut_loses_to_the_radio_edit(self):
        """The brief's own example: 'standard single length rather than the
        7-minute album cut'."""
        album_cut = C(
            title="Layla",
            release_group_type="Album",
            first_release_date="1970-11",
            length_seconds=425,
        )
        radio_edit = C(
            title="Layla (Radio Edit)",
            release_group_type="Single",
            first_release_date="1971-03",
            length_seconds=173,
        )
        p = rank("Derek & the Dominos", "Layla", [album_cut, radio_edit])
        assert p.best.candidate is radio_edit, p.best.explanation
        assert any("shortest here" in r for r in p.best.reasons)

    def test_edit_is_a_POSITIVE_marker_not_an_exclusion(self):
        """ORPHEUS's identifier listed 'edit' under exclude_keywords, which
        filtered out its own best signal. Pinned so it cannot come back."""
        p = rank(
            "Queen",
            "Bohemian Rhapsody",
            [C(title="Bohemian Rhapsody (Single Edit)", length_seconds=180)],
        )
        assert not p.verdicts[0].excluded
        assert p.verdicts[0].score > 0, p.verdicts[0].explanation


class TestExclusions:
    def test_a_live_take_is_excluded_outright(self):
        studio = C(title="Freebird", release_group_type="Single", length_seconds=290)
        live = C(
            title="Freebird (Live at the Fox Theatre)",
            secondary_types=("Live",),
            release_group_type="Album",
            first_release_date="1976",
            length_seconds=290,
        )
        p = rank("Lynyrd Skynyrd", "Freebird", [studio, live])
        excluded = [v for v in p.verdicts if v.excluded]
        assert len(excluded) == 1
        assert excluded[0].candidate is live
        assert "live" in excluded[0].excluded

    def test_a_live_marker_in_the_title_alone_is_enough(self):
        """MusicBrainz carries 'live' in secondary types OR in free text,
        depending on who entered the release. Both have to work."""
        p = rank(
            "Eagles",
            "Hotel California",
            [C(title="Hotel California (Live at the Forum)", length_seconds=400)],
        )
        assert p.verdicts[0].excluded

    def test_a_karaoke_pressing_is_excluded(self):
        """Section 4: karaoke products are removal candidates, not versions."""
        p = rank(
            "Party Tyme",
            "Song",
            [C(title="Song (Made Popular By Someone) [Karaoke Version]")],
        )
        assert p.verdicts[0].excluded

    def test_all_candidates_excluded_is_reported_not_crashed(self):
        p = rank("X", "Y", [C(title="Y (Live)"), C(title="Y (Karaoke)")])
        assert p.best is None or p.best.excluded
        assert "every candidate was excluded" in p.undecided


class TestListenBrainzIsUsedHonestly:
    def test_unknown_listens_are_never_treated_as_zero(self):
        """The distinction that made musaeus_report.py report nothing at all
        about 2,862 pending duplicate groups. None means unknown."""
        known = C(title="A", first_release_date="1970", listen_count=500)
        unknown = C(title="B", first_release_date="1970", listen_count=None)
        p = rank("Artist", "Song", [known, unknown])
        unknown_verdict = next(v for v in p.verdicts if v.candidate is unknown)
        assert any("unknown, not zero" in r for r in unknown_verdict.reasons)
        # And it must not be penalised for the absence.
        assert not any("listens" in r and "-" in r for r in unknown_verdict.reasons)

    def test_most_played_helps_but_does_not_override_a_remaster_penalty(self):
        """The Beatles problem: the most-played version of a catalogue song is
        often a modern remaster, because that is what streaming carries. Play
        count must not be able to win on its own."""
        remaster = C(
            title="Come Together (2009 Remaster)",
            disambiguation="2009 remaster",
            release_group_type="Single",
            first_release_date="2009",
            length_seconds=259,
            listen_count=2_000_000,
        )
        original = C(
            title="Come Together",
            release_group_type="Single",
            first_release_date="1969-10",
            length_seconds=259,
            media_format="Vinyl",
            listen_count=90_000,
        )
        p = rank("Beatles, The", "Come Together", [remaster, original])
        assert p.best.candidate is original, p.best.explanation

    def test_a_near_tie_in_plays_does_not_reward_both_as_winners(self):
        a = C(title="A", first_release_date="1970", listen_count=1000)
        b = C(title="B", first_release_date="1970", listen_count=999)
        p = rank("Artist", "Song", [a, b])
        winners = [
            v for v in p.verdicts if any("most played here" in r for r in v.reasons)
        ]
        assert len(winners) == 1


class TestItProposesRatherThanDecides:
    def test_a_narrow_margin_is_reported_as_undecided(self):
        """Every review artefact in this project proposes into a CSV and lets a
        human rule. A 3-point gap is not an answer."""
        a = C(title="Song", release_group_type="Single", first_release_date="1970")
        b = C(title="Song", release_group_type="Single", first_release_date="1971")
        p = rank("Artist", "Song", [a, b])
        assert not p.confident
        assert "too close to call" in p.undecided

    def test_a_clear_winner_is_confident(self):
        a = C(
            title="Song",
            release_group_type="Single",
            first_release_date="1970",
            length_seconds=180,
            media_format="Vinyl",
            listen_count=10_000,
        )
        b = C(
            title="Song (2015 Remaster)",
            disambiguation="2015 remaster",
            release_group_type="Album",
            first_release_date="2015",
            length_seconds=600,
        )
        p = rank("Artist", "Song", [a, b])
        assert p.confident
        assert p.best.candidate is a

    def test_no_candidates_is_undecided_not_an_exception(self):
        p = rank("Artist", "Song", [])
        assert p.best is None
        assert "no candidates" in p.undecided

    def test_every_verdict_explains_itself(self):
        """A heuristic that cannot say why is one a reviewer trusts blindly or
        ignores entirely."""
        p = rank(
            "Artist",
            "Song",
            [
                C(title="Song", release_group_type="Single", first_release_date="1970"),
                C(title="Song (Remaster)", first_release_date="2000"),
            ],
        )
        for v in p.verdicts:
            assert v.explanation
            assert v.explanation != "no signal either way"


class TestSparseDataDoesNotCrashIt:
    def test_a_candidate_with_nothing_but_a_title_is_handled(self):
        p = rank("Artist", "Song", [C(title="Song")])
        assert p.verdicts[0].explanation

    def test_a_junk_date_is_treated_as_absent(self):
        p = rank("Artist", "Song", [C(title="Song", first_release_date="unknown")])
        assert p.verdicts[0].candidate.year is None
        assert any("cannot judge" in r for r in p.verdicts[0].reasons)

    def test_early_rock_is_not_penalised_for_being_short(self):
        """Section 5 measured a 120-second floor flagging 397 complete
        recordings -- 'Hit the Road Jack' at 2:00, 'All Shook Up' at 1:58.
        ORPHEUS used (120, 360); this uses 100 so they clear it."""
        p = rank(
            "Ray Charles",
            "Hit the Road Jack",
            [C(title="Hit the Road Jack", length_seconds=118)],
        )
        assert any("single length" in r for r in p.verdicts[0].reasons)
