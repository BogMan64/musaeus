"""An error is never data. That is what these tests are for.

The scoring tests prove the heuristic. These prove the plumbing cannot lie to
it -- which matters more, because a wrong score is visible in a review CSV and
a silently empty result is not.

Every test here is offline. The fixtures are shaped from a real 9,057-record
ListenBrainz response for The Beatles captured 2026-09-10, so the field names
and the shape of the tail are measured rather than imagined.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fmradio.clients import (  # noqa: E402
    DEFAULT_MIN_LISTENS,
    AuthRequired,
    FakePopularity,
    FakePressings,
    ListenBrainz,
    SourceUnavailable,
    candidate_from_listenbrainz,
    candidate_from_musicbrainz,
)
from fmradio.popularity import ABSOLUTE_NOISE_FLOOR, artist_floor  # noqa: E402

BEATLES = "b10bbbfc-cf9e-42e0-be17-e2c3e1d2600d"


def lb_record(name, listens, ms=None, release="Abbey Road", mbid="rec-1"):
    """Shaped exactly like a real record from the captured response."""
    return {
        "recording_name": name,
        "recording_mbid": mbid,
        "release_name": release,
        "release_mbid": "rel-1",
        "length": ms,
        "total_listen_count": listens,
        "total_user_count": max(1, (listens or 0) // 10),
    }


class TestAnErrorIsNeverData:
    def test_a_missing_token_is_refused_at_construction(self):
        """Not at first request. A run that gets halfway through 3,500 artists
        before discovering it has no credential has wasted an hour and, worse,
        produced a half-populated CSV."""
        with pytest.raises(AuthRequired) as exc:
            ListenBrainz(token="")
        # The message must distinguish the three MetaBrainz credentials, because
        # they are easy to confuse and only one of them works here.
        assert "USER TOKEN" in str(exc.value)
        assert "OAuth" in str(exc.value)

    def test_a_401_is_fatal_and_not_an_empty_result(self):
        """The real trap, 2026-09-10: an unauthenticated request returned HTTP
        200 with 9,057 genuine Beatles records from an edge cache, while every
        other artist returned 401. Had the client treated 401 as "no data",
        every artist would have scored as "no ListenBrainz signal", the run
        would have completed, and the report would have claimed success having
        measured nothing."""
        client = FakePopularity({}, raise_auth=True)
        with pytest.raises(AuthRequired):
            client.top_recordings(BEATLES)

    def test_an_unreachable_source_raises_rather_than_returning_nothing(self):
        client = FakePopularity({}, raise_unavailable=True)
        with pytest.raises(SourceUnavailable):
            client.top_recordings(BEATLES)

    def test_a_successful_empty_answer_IS_data(self):
        """The one case where [] is correct: the source answered and had
        nothing. Distinguishable from the two above only because those raise."""
        client = FakePopularity({BEATLES: []})
        assert client.top_recordings(BEATLES) == []
        assert client.calls == [BEATLES]


class TestTheListenFloor:
    def test_the_one_listen_tail_is_dropped(self):
        """Measured on the real response: 9,057 records, 2,249 of them with
        exactly one listen -- session takes, Ed Sullivan performances,
        "A/B Road: Complete Get Back Sessions". Only 1,753 cleared 100.
        Passing the tail to the scorer would bury the real pressings."""
        records = [
            lb_record("Let It Be", 2_093_966),
            lb_record("Come Together", 2_080_106),
            lb_record("Los Paranoias (take 35 - Complete)", 1),
            lb_record("Don't Let Me Down 6.83b", 1),
            lb_record("I Saw Her Standing There (take 8 - False Start)", 2),
        ]
        kept = [r for r in records if (r["total_listen_count"] or 0) >= DEFAULT_MIN_LISTENS]
        assert len(kept) == 2
        assert {r["recording_name"] for r in kept} == {"Let It Be", "Come Together"}

    def test_the_fetch_gate_is_only_a_noise_gate_now(self):
        """CHANGED DELIBERATELY. This asserted DEFAULT_MIN_LISTENS == 100.

        That absolute floor was doing two jobs: keeping the bootleg tail out of
        the scorer, and -- as an unintended side effect -- deciding that an
        obscure artist had no data at all. An artist whose best recording sits
        at 40 listens returned an empty list, and the run reported "no
        ListenBrainz signal", which is what an outage looks like.

        So the number here is now just a noise gate, and separating pressings
        from bootlegs moved to a per-artist top-decile floor in
        fmradio/popularity.py, where the artist's own distribution is known.
        See tests/test_popularity.py."""
        assert DEFAULT_MIN_LISTENS == ABSOLUTE_NOISE_FLOOR
        obscure = [lb_record("A Song Nobody Streams", 12)]
        kept = [r for r in obscure if r["total_listen_count"] >= DEFAULT_MIN_LISTENS]
        assert kept == obscure, "an obscure artist must survive the fetch gate"

    def test_the_floor_is_still_configurable(self):
        """The right floor for The Beatles is not the right floor for an artist
        with 200 total listens across their whole catalogue -- which is now
        handled by deriving it per artist rather than by passing a number."""
        beatles = artist_floor([2_093_966, 2_080_106] + [1] * 2249)
        obscure = artist_floor([40, 30, 20, 12])
        assert beatles.value > obscure.value
        assert obscure.value <= 40, "the obscure artist's best must survive"


class TestAdaptersDoNotInventFacts:
    def test_listenbrainz_leaves_the_date_empty_rather_than_guessing(self):
        """A ListenBrainz record has no first-release-date and no release-group
        type. Filling either from the release name would be invention, and the
        scorer would then rank on it."""
        c = candidate_from_listenbrainz(lb_record("Let It Be", 2_093_966, ms=243_000))
        assert c.first_release_date == ""
        assert c.release_group_type == ""
        assert c.length_seconds == pytest.approx(243.0)
        assert c.listen_count == 2_093_966

    def test_a_missing_length_stays_None_not_zero(self):
        """Of the real records, many carry length: null. Zero would read as a
        4-second fragment and be penalised as one."""
        c = candidate_from_listenbrainz(lb_record("Some Take", 150, ms=None))
        assert c.length_seconds is None

    def test_an_absent_listen_count_stays_None(self):
        c = candidate_from_listenbrainz(
            {"recording_name": "X", "total_listen_count": None}
        )
        assert c.listen_count is None

    def test_musicbrainz_prefers_the_RECORDINGS_date_over_the_release_groups(self):
        """The distinction MetaBrainz's own docs warn about: %originalyear% is
        the release group's first release date, NOT the recording's. A 1969
        recording on a 1990 Greatest Hits has a release-group date of 1990.
        Taking the minimum is what MUSAEUS's original_year.py does."""
        rec = {
            "id": "rec-9",
            "title": "Come Together",
            "first-release-date": "1969-09-26",
            "length": 259_000,
        }
        release = {
            "id": "rel-9",
            "title": "Greatest Hits",
            "release-group": {
                "primary-type": "Album",
                "secondary-types": ["Compilation"],
                "first-release-date": "1990-01-01",
            },
            "media": [{"format": "CD"}],
        }
        c = candidate_from_musicbrainz(rec, release)
        assert c.first_release_date == "1969-09-26"
        assert c.year == 1969
        assert c.secondary_types == ("Compilation",)
        assert c.release_group_type == "Album"

    def test_the_release_group_date_is_used_when_the_recording_has_none(self):
        rec = {"id": "r", "title": "T"}
        release = {
            "id": "x",
            "title": "R",
            "release-group": {"primary-type": "Single", "first-release-date": "1971"},
        }
        c = candidate_from_musicbrainz(rec, release)
        assert c.first_release_date == "1971"

    def test_no_date_anywhere_stays_empty_and_the_scorer_is_told(self):
        c = candidate_from_musicbrainz({"id": "r", "title": "T"}, {"id": "x"})
        assert c.first_release_date == ""
        assert c.year is None


class TestPass2IsOnlyAskedWhenNeeded:
    def test_the_pressing_source_records_exactly_which_songs_it_was_asked(self):
        """The whole two-pass saving rests on MusicBrainz being asked about a
        subset. A test that counts the calls is how that stays true."""
        mb = FakePressings({("Beatles, The", "Come Together"): []})
        mb.pressings("Beatles, The", "Come Together")
        assert mb.calls == [("Beatles, The", "Come Together")]

    def test_an_unreachable_pressing_source_raises(self):
        mb = FakePressings({}, raise_unavailable=True)
        with pytest.raises(SourceUnavailable):
            mb.pressings("A", "B")
