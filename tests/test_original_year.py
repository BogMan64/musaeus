"""
Tests for OriginalYearStage's matching guards.

The stage exists because `year` is the edition year, not the recording
year — measured 2026-08-23, 57% of Rock & Roll tracks and 51% of Blues
carried a year >= 2010, and the Beach Boys' "409" (1962) was dated 2012.

What is pinned here is the refusal path. A wrong original_year is worse
than a missing one: downstream it is indistinguishable from a right one,
which is the exact failure this project keeps finding. Every guard below
must be able to reject, so none of them is a check that cannot fail.
"""

from __future__ import annotations

import pytest

from musaeus.stages.original_year import (
    earliest_year,
    find_original_year,
    strip_edition_markers,
)


def _rec(score=100, artist="Al Green", length_ms=195000, **kw):
    """One MusicBrainz recording search result."""
    rec = {
        "score": score,
        "artist-credit": [{"artist": {"name": artist}}],
        "length": length_ms,
    }
    rec.update(kw)
    return rec


class TestEditionMarkerStripping:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("409 (Remastered 2012)", "409"),
            ("California Girls (Stereo)", "California Girls"),
            ("And Your Dream Comes True (Mono)", "And Your Dream Comes True"),
            ("The Boy From New York City (Remastered 2012)", "The Boy From New York City"),
            ("Dance, Dance, Dance (stereo)", "Dance, Dance, Dance"),
            ("Let's Stay Together [Remaster]", "Let's Stay Together"),
            ("Something (Deluxe Edition)", "Something"),
            ("Rocks (50th Anniversary Edition)", "Rocks"),
        ],
    )
    def test_edition_markers_are_removed(self, raw, expected):
        assert strip_edition_markers(raw) == expected

    @pytest.mark.parametrize(
        "raw",
        [
            "Hotel California (Live)",
            "Layla (Acoustic)",
            "Blue Monday (Radio Edit)",
            "Heroes (Demo)",
            "Sweet Child O' Mine",
            "Dancing (In the Street)",
        ],
    )
    def test_a_different_recording_is_never_stripped(self, raw):
        # A live or acoustic take has its own first release date. Folding it
        # into the studio cut would import the wrong year with full confidence.
        assert strip_edition_markers(raw) == raw

    def test_marker_only_title_is_left_alone(self):
        # Emptying the title would search MusicBrainz for "", matching
        # everything — the worst available failure.
        assert strip_edition_markers("(Remastered)") == "(Remastered)"


class TestEarliestYear:
    def test_takes_the_minimum_across_recording_and_releases(self):
        rec = _rec(
            **{"first-release-date": "1994-09-27"},
            releases=[{"date": "2012-06-01"}, {"date": "1962-07-16"}],
        )
        assert earliest_year(rec) == 1962

    def test_reads_release_group_dates_too(self):
        rec = _rec(releases=[{"release-group": {"first-release-date": "1965"}}])
        assert earliest_year(rec) == 1965

    def test_no_dates_yields_none(self):
        assert earliest_year(_rec(releases=[])) is None
        assert earliest_year(_rec(releases=[{"date": ""}, {"date": None}])) is None

    def test_malformed_dates_are_ignored_not_parsed_loosely(self):
        assert earliest_year(_rec(releases=[{"date": "19??"}, {"date": "1971"}])) == 1971


class TestGuardsRejectBadMatches:
    """find_original_year must return None, with a reason, rather than a
    plausible-looking wrong year."""

    def _patch(self, monkeypatch, recordings):
        import musaeus.stages.original_year as oy

        monkeypatch.setattr(oy, "_mb_get", lambda *a, **k: {"recordings": recordings})
        monkeypatch.setattr(oy.time, "sleep", lambda *_: None)

    def test_accepts_a_clean_match(self, monkeypatch):
        self._patch(monkeypatch, [_rec(**{"first-release-date": "1972-01-01"})])
        year, reason = find_original_year("Al Green", "Let's Stay Together", 195.0, 2014)
        assert year == 1972
        assert reason == ""

    def test_low_score_is_refused(self, monkeypatch):
        self._patch(monkeypatch, [_rec(score=40, **{"first-release-date": "1972"})])
        year, reason = find_original_year("Al Green", "Let's Stay Together", 195.0, 2014)
        assert year is None
        assert reason

    def test_different_artist_is_refused(self, monkeypatch):
        # A cover under the same title is the most likely wrong answer.
        self._patch(monkeypatch, [_rec(artist="Tina Turner", **{"first-release-date": "1983"})])
        year, reason = find_original_year("Al Green", "Let's Stay Together", 195.0, 2014)
        assert year is None
        assert "artist" in reason

    def test_length_disagreement_is_refused(self, monkeypatch):
        self._patch(monkeypatch, [_rec(length_ms=420000, **{"first-release-date": "1972"})])
        year, reason = find_original_year("Al Green", "Let's Stay Together", 195.0, 2014)
        assert year is None
        assert "length" in reason

    def test_small_length_drift_is_tolerated(self, monkeypatch):
        # Remasters shift by a second or two; that must not cost us the match.
        self._patch(monkeypatch, [_rec(length_ms=197500, **{"first-release-date": "1972"})])
        year, _ = find_original_year("Al Green", "Let's Stay Together", 195.0, 2014)
        assert year == 1972

    def test_year_later_than_the_file_is_refused(self, monkeypatch):
        # An original cannot post-date the pressing we hold. When MB says it
        # does, one of the two is wrong and we cannot tell which.
        self._patch(monkeypatch, [_rec(**{"first-release-date": "2019"})])
        year, reason = find_original_year("Al Green", "Let's Stay Together", 195.0, 1972)
        assert year is None
        assert "later" in reason

    def test_implausible_year_is_refused(self, monkeypatch):
        self._patch(monkeypatch, [_rec(**{"first-release-date": "1301"})])
        year, reason = find_original_year("Al Green", "Let's Stay Together", 195.0, 2014)
        assert year is None

    def test_stored_article_form_still_matches_the_credit(self, monkeypatch):
        # The library stores "Beach Boys, The"; MusicBrainz credits
        # "The Beach Boys". Without folding, every article artist would miss.
        self._patch(
            monkeypatch,
            [_rec(artist="The Beach Boys", length_ms=120000, **{"first-release-date": "1962"})],
        )
        year, _ = find_original_year("Beach Boys, The", "409 (Remastered 2012)", 120.0, 2012)
        assert year == 1962

    def test_missing_duration_does_not_block_a_match(self, monkeypatch):
        self._patch(monkeypatch, [_rec(**{"first-release-date": "1972"})])
        year, _ = find_original_year("Al Green", "Let's Stay Together", None, 2014)
        assert year == 1972

    def test_lookup_error_is_reported_not_raised(self, monkeypatch):
        import musaeus.stages.original_year as oy

        def boom(*a, **k):
            raise OSError("network down")

        monkeypatch.setattr(oy, "_mb_get", boom)
        year, reason = find_original_year("Al Green", "Let's Stay Together", 195.0, 2014)
        assert year is None
        assert "lookup error" in reason

    def test_empty_inputs_are_refused_before_any_request(self, monkeypatch):
        import musaeus.stages.original_year as oy

        called = []
        monkeypatch.setattr(oy, "_mb_get", lambda *a, **k: called.append(1) or {})
        assert find_original_year("", "Let's Stay Together", 195.0, 2014)[0] is None
        assert find_original_year("Al Green", "", 195.0, 2014)[0] is None
        assert not called


class TestSearchStrategy:
    """The two settings that decided whether this stage worked at all.
    Both were found live, not by reasoning — pinned so a later tidy-up
    cannot quietly undo them."""

    def test_page_size_is_musicbrainz_maximum(self):
        # At 25, every result for "Dancing Queen" was a compilation entry and
        # the stage returned 1990 with full confidence. The 1976 original sits
        # beyond that page. 102 recording entries exist for that one song.
        from musaeus.stages.original_year import _CANDIDATE_LIMIT

        assert _CANDIDATE_LIMIT == 100

    def test_query_is_not_pre_encoded(self, monkeypatch):
        # quote()-ing inside the Lucene query double-encodes it once _mb_get
        # runs urlencode, and MusicBrainz then matches nothing at all.
        import musaeus.stages.original_year as oy

        seen = {}

        def capture(path, params):
            seen["query"] = params["query"]
            return {"recordings": []}

        monkeypatch.setattr(oy, "_mb_get", capture)
        find_original_year("Beach Boys, The", "409", 119.0, 2012)
        assert "%20" not in seen["query"]
        assert 'artist:"The Beach Boys"' in seen["query"]

    def test_bracketed_title_is_retried_bare_on_a_miss(self, monkeypatch):
        # "Downtown (64 Original Release with Orchestra)" finds nothing under
        # its full title and 1964 under "Downtown".
        import musaeus.stages.original_year as oy

        queries = []

        def two_step(path, params):
            queries.append(params["query"])
            if "64 Original" in params["query"]:
                return {"recordings": []}
            return {
                "recordings": [
                    {
                        "score": 100,
                        "artist-credit": [{"artist": {"name": "Petula Clark"}}],
                        "length": 187000,
                        "first-release-date": "1964-11",
                    }
                ]
            }

        monkeypatch.setattr(oy, "_mb_get", two_step)
        monkeypatch.setattr(oy.time, "sleep", lambda *_: None)  # don't pay the rate limit
        year, _ = find_original_year(
            "Petula Clark", "Downtown (64 Original Release with Orchestra)", 187.0, 2007
        )
        assert year == 1964
        assert len(queries) == 2

    def test_no_retry_when_the_first_attempt_matches(self, monkeypatch):
        import musaeus.stages.original_year as oy

        calls = []

        def once(path, params):
            calls.append(params["query"])
            return {
                "recordings": [
                    {
                        "score": 100,
                        "artist-credit": [{"artist": {"name": "Al Green"}}],
                        "length": 195000,
                        "first-release-date": "1972",
                    }
                ]
            }

        monkeypatch.setattr(oy, "_mb_get", once)
        find_original_year("Al Green", "Let's Stay Together (Remastered)", 195.0, 2014)
        assert len(calls) == 1


class TestCandidateSelection:
    """A full pass is hours of rate-limited lookups. The narrowing options
    exist so a pending decision does not have to wait for all of it."""

    def _stage_with(self, tmp_path, rows, **ctx_values):
        import sqlite3

        from musaeus.stages.original_year import OriginalYearStage

        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.execute(
            "CREATE TABLE archive (id INTEGER PRIMARY KEY, file_path TEXT, artist TEXT, "
            "title TEXT, year TEXT, duration REAL, genre TEXT, status TEXT, "
            "original_year_checked_at TEXT)"
        )
        for i, (artist, title, genre, checked) in enumerate(rows, 1):
            conn.execute(
                "INSERT INTO archive (id, file_path, artist, title, year, duration, genre, "
                "status, original_year_checked_at) VALUES (?,?,?,?,?,?,?,?,?)",
                (i, f"/x/{i}.m4a", artist, title, "2014", 200.0, genre, "CATALOGUED", checked),
            )
        conn.commit()

        class Ctx:
            def __init__(self, conn, values):
                self.conn = conn
                self._v = values

            def get(self, key, default=None):
                return self._v.get(key, default)

        return OriginalYearStage(), Ctx(conn, ctx_values)

    ROWS = [
        ("Abba", "Dancing Queen", "Pop", None),
        ("Madonna", "Holiday", "Pop", None),
        ("Miles Davis", "So What", "Jazz", None),
        ("Al Green", "Let's Stay Together", "R&B/Funk/Soul", None),
        ("Cher", "Believe", "Pop", "2026-08-24T00:00:00"),  # already checked
    ]

    def test_unfiltered_pass_takes_every_unchecked_row(self, tmp_path):
        stage, ctx = self._stage_with(tmp_path, self.ROWS)
        assert len(stage._candidates(ctx)) == 4

    def test_genre_filter_narrows_to_that_genre(self, tmp_path):
        stage, ctx = self._stage_with(tmp_path, self.ROWS, original_year_genre="Pop")
        got = stage._candidates(ctx)
        assert {r["artist"] for r in got} == {"Abba", "Madonna"}

    def test_already_checked_rows_are_never_revisited(self, tmp_path):
        # Cher is in Pop but carries a checked_at — a re-run must not pay for
        # her again, or the pass can never converge.
        stage, ctx = self._stage_with(tmp_path, self.ROWS, original_year_genre="Pop")
        assert all(r["artist"] != "Cher" for r in stage._candidates(ctx))

    def test_limit_bounds_the_pass(self, tmp_path):
        stage, ctx = self._stage_with(tmp_path, self.ROWS, original_year_limit=2)
        assert len(stage._candidates(ctx)) == 2

    def test_genre_is_parameterised_not_interpolated(self, tmp_path):
        # A genre with a quote in it must not be able to alter the query.
        stage, ctx = self._stage_with(tmp_path, self.ROWS, original_year_genre="Pop' OR '1'='1")
        assert stage._candidates(ctx) == []


class TestTransientFailuresAreRetryable:
    """A 503 says nothing about whether the recording exists.

    Measured on the Pop pass, 2026-08-24: 39 of 123 misses were transient
    (24 × HTTP 503, 15 × read timeout). Every one had been stamped
    original_year_checked_at, so a re-run would have skipped all 39
    permanently — the row would look decided when nothing had been decided.
    """

    def test_network_failures_are_classified_transient(self):
        from musaeus.stages.original_year import is_transient

        assert is_transient("lookup error: HTTP Error 503: Service Temporarily Unavailable")
        assert is_transient("lookup error: The read operation timed out")

    @pytest.mark.parametrize(
        "reason",
        [
            "no candidate scored high enough",
            "track length disagreed",
            "artist credit did not match",
            "no release date on the match",
            "no artist or title to search on",
            "MB year 2019 is later than the file's 1972",
        ],
    )
    def test_data_decisions_are_not_transient(self, reason):
        # These are the guards working. Retrying them would just burn the
        # rate limit to reach the same answer.
        from musaeus.stages.original_year import is_transient

        assert not is_transient(reason)

    def test_a_lookup_error_reports_as_transient(self, monkeypatch):
        import musaeus.stages.original_year as oy

        def boom(*a, **k):
            raise OSError("HTTP Error 503")

        monkeypatch.setattr(oy, "_mb_get", boom)
        _, reason = find_original_year("Al Green", "Let's Stay Together", 195.0, 2014)
        assert oy.is_transient(reason)
