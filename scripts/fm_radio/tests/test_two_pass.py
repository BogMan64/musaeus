"""Pass 2 scores in the same run.

THE ROUGH EDGE BEING FIXED. Pass 2 used to fetch MusicBrainz pressings, cache
them, and print "re-run to score the newly cached pressings". Two invocations for
one answer, the first always ending by admitting it had not finished, and the
second one's behaviour depending on cache state rather than its arguments.

Anyone who took the first CSV at face value read a verdict reached WITHOUT the
MusicBrainz evidence the run had just gone and fetched.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fm_radio_identifier import score_songs  # noqa: E402
from fmradio.popularity import DEFAULT_QUANTILE  # noqa: E402


class FakeCache:
    """Only the one method score_songs uses."""

    def __init__(self, songs: dict | None = None):
        self._songs = songs or {}

    def get_song(self, artist: str, title: str):
        return self._songs.get((artist, title))


def lb(name: str, listens: int, length_ms: int = 200_000) -> dict:
    return {
        "recording_name": name,
        "recording_mbid": f"mbid-{name}-{listens}",
        "total_listen_count": listens,
        "length": length_ms,
    }


def mb_pressing(title: str, date: str) -> dict:
    return {
        "title": title,
        "id": f"rec-{title}-{date}",
        "length": 200_000,
        "releases": [
            {
                "id": f"rel-{date}",
                "date": date,
                "release-group": {"primary-type": "Album", "secondary-types": []},
            }
        ],
    }


ARTIST, TITLE = "Test Artist", "Test Song"
SONGS = [(ARTIST, TITLE, "artist-mbid")]


class TestScoringIsSeparableFromFetching:
    def test_pass_1_ignores_cached_musicbrainz_pressings(self):
        """use_musicbrainz=False is what makes the first scoring pass honest:
        it must not quietly consume pressings left in the cache by an earlier
        run, or the same command gives different answers on consecutive runs."""
        cache = FakeCache({(ARTIST, TITLE): [mb_pressing(TITLE, "1969-01-01")]})
        by_artist = {"artist-mbid": [lb(TITLE, 50_000)]}
        proposals, _ = score_songs(SONGS, by_artist, cache, DEFAULT_QUANTILE)
        considered = proposals[0].verdicts
        assert len(considered) == 1, "the cached MB pressing must not be scored in pass 1"

    def test_pass_2_scores_the_new_pressings_in_the_same_call(self):
        """THE FIX. The second scoring pass sees the MusicBrainz pressings and
        produces more candidates -- no re-run, no second invocation."""
        cache = FakeCache({(ARTIST, TITLE): [mb_pressing(TITLE, "1969-01-01")]})
        by_artist = {"artist-mbid": [lb(TITLE, 50_000)]}
        p1, unsettled = score_songs(SONGS, by_artist, cache, DEFAULT_QUANTILE)
        p2, _ = score_songs(
            unsettled or SONGS, by_artist, cache, DEFAULT_QUANTILE, use_musicbrainz=True
        )
        assert len(p2[0].verdicts) > len(p1[0].verdicts)

    def test_scoring_makes_no_network_call(self):
        """score_songs is called twice per run, so it must be pure. If it grew a
        request, the second pass would double the run's traffic against two
        sources that both rate-limit to one call per second."""
        import urllib.request

        original = urllib.request.urlopen

        def explode(*a, **k):  # pragma: no cover
            raise AssertionError("score_songs made a network request")

        urllib.request.urlopen = explode
        try:
            score_songs(
                SONGS,
                {"artist-mbid": [lb(TITLE, 50_000)]},
                FakeCache(),
                DEFAULT_QUANTILE,
                use_musicbrainz=True,
            )
        finally:
            urllib.request.urlopen = original


class TestTheUnsettledListRoundTrips:
    def test_unsettled_entries_carry_the_mbid_back(self):
        """The unsettled list is fed straight back into score_songs, so it has
        to be the same 3-tuple shape as the input. It used to be (artist, title)
        only, which meant the re-scoring pass had no artist mbid and would have
        found zero ListenBrainz candidates -- pass 2 would have appeared to make
        every song worse."""
        _, unsettled = score_songs(SONGS, {}, FakeCache(), DEFAULT_QUANTILE)
        assert unsettled, "a song with no candidates is not settled"
        for entry in unsettled:
            assert len(entry) == 3
            artist, title, mbid = entry
            assert (artist, title, mbid) in SONGS

    def test_replacement_key_folds_the_same_way_both_sides(self):
        """main() substitutes re-scored proposals by (artist, title) lowered and
        stripped. If a Proposal did not carry back the artist and title it was
        given, the substitution would silently match nothing and pass 2 would
        have no visible effect at all."""
        proposals, _ = score_songs(
            [("  Test Artist ", " Test Song ", "artist-mbid")],
            {"artist-mbid": [lb(TITLE, 50_000)]},
            FakeCache(),
            DEFAULT_QUANTILE,
        )
        p = proposals[0]
        key = (p.artist.strip().lower(), p.title.strip().lower())
        assert key == (ARTIST.lower(), TITLE.lower())


class TestTheFloorIsExplainedOnEveryRow:
    def test_the_floor_is_recorded_in_the_reasons(self):
        """The floor changes which pressings a reviewer ever sees, so the number
        travels with the recommendation rather than living only in the code."""
        proposals, _ = score_songs(
            SONGS,
            {"artist-mbid": [lb(TITLE, 50_000), lb("Other", 900_000)]},
            FakeCache(),
            DEFAULT_QUANTILE,
        )
        reasons = " ".join(proposals[0].best.reasons)
        assert "popularity floor" in reasons

    def test_a_waived_floor_says_so(self):
        """An album cut below its artist's top decile is kept, and the row must
        say the floor was waived -- it is also the shape of a title-matching
        failure, and the two need telling apart."""
        by_artist = {
            "artist-mbid": [lb("Massive Hit", 5_000_000), lb(TITLE, 900)],
        }
        proposals, _ = score_songs(SONGS, by_artist, FakeCache(), DEFAULT_QUANTILE)
        reasons = " ".join(proposals[0].best.reasons)
        assert "waived" in reasons
        assert proposals[0].verdicts, "the song itself is never filtered away"
