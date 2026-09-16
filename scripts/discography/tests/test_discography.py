"""Tests for the gap detector.

Everything here runs offline. The scope ruling and the matcher are pure, which is
deliberate: they are the two places a wrong answer is expensive, and neither
should need MusicBrainz to be checkable.
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from discography.library import (  # noqa: E402
    OwnedArtist,
    _is_real_album,
    load_artists,
)
from discography.match import Gap, find_gaps, fold, owned_keys  # noqa: E402
from discography.scope import (  # noqa: E402
    ReleaseGroup,
    excluded_because,
    is_studio_album,
    partition,
)


def rg(title: str, primary: str = "Album", secondary: tuple = (), date: str = "") -> ReleaseGroup:
    return ReleaseGroup(
        mbid=f"mbid-{title}",
        title=title,
        primary_type=primary,
        secondary_types=secondary,
        first_release_date=date,
    )


# ── The ruling ────────────────────────────────────────────────────────────────
class TestTheRuling:
    """primary-type = Album, no secondary types. Grey's call."""

    def test_a_studio_album_passes(self):
        assert is_studio_album(rg("Nebraska", date="1982-09-30"))

    def test_a_live_album_is_excluded_and_this_is_the_operative_half(self):
        """A live album has primary-type Album AND secondary-type Live. Filtering
        on primary type alone lets every live album and greatest-hits through --
        which is the noise the ruling exists to remove."""
        live = rg("Before the Flood", secondary=("Live",))
        assert live.primary_type == "Album"
        assert not is_studio_album(live)
        assert "Live" in excluded_because(live)

    def test_compilations_soundtracks_and_remixes_excluded(self):
        for sec in ("Compilation", "Soundtrack", "Remix", "Demo", "Interview"):
            assert not is_studio_album(rg("X", secondary=(sec,))), sec

    def test_singles_and_eps_excluded(self):
        assert not is_studio_album(rg("Hey Jude", primary="Single"))
        assert not is_studio_album(rg("An EP", primary="EP"))

    def test_a_missing_primary_type_is_excluded_not_assumed(self):
        """MusicBrainz sometimes has no type recorded. Guessing "probably an
        album" would put unverified rows in a hunting list."""
        r = rg("Mystery", primary="")
        assert not is_studio_album(r)
        assert "no primary type" in excluded_because(r)

    def test_exclusion_gives_a_reason_not_just_a_boolean(self):
        """This filter removes the large majority of what MusicBrainz returns,
        which is exactly when being able to check it matters."""
        assert excluded_because(rg("Nebraska")) == ""
        assert "Live" in excluded_because(rg("X", secondary=("Live",)))
        assert "Single" in excluded_because(rg("X", primary="Single"))

    def test_multiple_secondary_types_all_reported(self):
        why = excluded_because(rg("X", secondary=("Live", "Compilation")))
        assert "Live" in why and "Compilation" in why

    def test_blank_secondary_entries_do_not_exclude(self):
        """An empty string in the list is not a secondary type. Treating it as
        one would exclude every studio album whose list came back as ['']."""
        assert is_studio_album(rg("Nebraska", secondary=("", "  ")))

    def test_partition_returns_both_halves(self):
        groups = [rg("A"), rg("B", secondary=("Live",)), rg("C", primary="Single")]
        keep, drop = partition(groups)
        assert [g.title for g in keep] == ["A"]
        assert {t for (g, t) in [(g, g.title) for g, _ in drop]} == {"B", "C"}
        assert len(keep) + len(drop) == len(groups)

    def test_year_parses_and_tolerates_junk(self):
        assert rg("A", date="1982-09-30").year == 1982
        assert rg("A", date="1982").year == 1982
        assert rg("A", date="").year is None
        assert rg("A", date="unknown").year is None


# ── Matching ──────────────────────────────────────────────────────────────────
class TestMatching:
    def test_exact_title_matches(self):
        gaps, matched = find_gaps("X", {"Nebraska"}, [rg("Nebraska")])
        assert not gaps
        assert len(matched) == 1

    def test_edition_noise_does_not_create_a_false_gap(self):
        """THE FAILURE THAT COSTS TRUST. Report an album as missing that is
        sitting in the library, once, and the report stops being believed."""
        for owned in (
            "Nebraska (2015 Remaster)",
            "Nebraska [Deluxe Edition]",
            "nebraska",
            "Nebraska (Expanded)",
        ):
            gaps, matched = find_gaps("X", {owned}, [rg("Nebraska")])
            assert not gaps, f"{owned!r} should match Nebraska"
            assert matched

    def test_album_editions_musaeus_does_not_strip(self):
        """MUSAEUS's STRIP_WORDS is tuned for TRACK version qualifiers, so it
        folds "(2015 Remaster)" but leaves "[Deluxe Edition]", "(Expanded)" and
        "(Anniversary Edition)" in place. Those survivals are false gaps."""
        for owned in (
            "Nebraska [Deluxe Edition]",
            "Nebraska (Expanded Edition)",
            "Nebraska (Anniversary Edition)",
            "Nebraska (Super Deluxe)",
            "Nebraska (2015 Remastered Edition)",
            "Nebraska - Deluxe Edition",
        ):
            gaps, matched = find_gaps("X", {owned}, [rg("Nebraska")])
            assert not gaps, f"{owned!r} should match Nebraska"
            assert matched

    def test_edition_words_in_a_real_title_are_not_stripped(self):
        """THE OPPOSITE FAILURE, and it is worse: a false match reports an album
        as OWNED when it is not, so the gap silently disappears. "Special Beat
        Service" and "Mono" are real album titles. Stripping edition words
        wherever they appear would fold different records together, so the strip
        is bracket-scoped and only fires when the bracket says nothing else."""
        gaps, matched = find_gaps("X", {"Special Beat Service"}, [rg("Special")])
        assert [g.album for g in gaps] == ["Special"]
        assert not matched

        gaps, _ = find_gaps("X", {"Mono"}, [rg("Stereo")])
        assert [g.album for g in gaps] == ["Stereo"]

    def test_a_bracket_with_real_words_is_kept(self):
        """"(Just When I Thought I Was Over You)" is part of the title, not an
        edition. Only brackets made entirely of edition words are removed."""
        gaps, matched = find_gaps(
            "X", {"Here I Am (Just When I Thought I Was Over You)"}, [rg("Here I Am")]
        )
        assert [g.album for g in gaps] == ["Here I Am"]
        assert not matched

    def test_a_dash_tail_that_is_not_an_edition_survives(self):
        """"Zuma - Live Rust" must not lose its tail to the dash rule."""
        gaps, _ = find_gaps("X", {"Zuma - Live Rust"}, [rg("Zuma")])
        assert [g.album for g in gaps] == ["Zuma"]

    def test_a_leading_the_does_not_split_a_record(self):
        gaps, _ = find_gaps("X", {"The Wall"}, [rg("Wall")])
        assert not gaps

    def test_a_real_gap_is_reported(self):
        gaps, matched = find_gaps("Springsteen", {"Born to Run"}, [rg("Born to Run"), rg("Nebraska", date="1982")])
        assert [g.album for g in gaps] == ["Nebraska"]
        assert gaps[0].year == 1982
        assert len(matched) == 1

    def test_similar_but_different_titles_stay_different(self):
        gaps, _ = find_gaps("Alice Cooper", {"Killer"}, [rg("Killers")])
        assert [g.album for g in gaps] == ["Killers"]

    def test_gaps_are_ordered_oldest_first(self):
        gaps, _ = find_gaps("X", set(), [rg("B", date="1990"), rg("A", date="1970")])
        assert [g.album for g in gaps] == ["A", "B"]

    def test_undated_albums_sort_last_not_first(self):
        gaps, _ = find_gaps("X", set(), [rg("NoDate"), rg("Dated", date="1970")])
        assert [g.album for g in gaps] == ["Dated", "NoDate"]

    def test_an_empty_fold_never_becomes_a_key(self):
        """An album tag of punctuation only, or made entirely of stripped edition
        words, folds to "". As a key it would match any release group that also
        folded to empty and silently mark real gaps as owned."""
        assert owned_keys({"(Remastered)", "...", "   "}) == set()
        gaps, matched = find_gaps("X", {"(Remastered)"}, [rg("Nebraska")])
        assert len(gaps) == 1 and not matched

    def test_compilation_flag_propagates_to_every_gap(self):
        gaps, _ = find_gaps("X", {"Greatest Hits"}, [rg("A"), rg("B")], has_compilation=True)
        assert all(g.may_be_covered_by_compilation for g in gaps)

    def test_every_studio_album_is_either_owned_or_missing(self):
        """The invariant the CLI asserts on its own totals. If matching ever
        both-counted or dropped a release group, the summary arithmetic would
        stop closing and the report would be quietly wrong."""
        canonical = [rg(f"Album {i}", date=f"19{70 + i}") for i in range(8)]
        owned = {"Album 1", "Album 3 (Remaster)", "Album 7"}
        gaps, matched = find_gaps("X", owned, canonical)
        assert len(gaps) + len(matched) == len(canonical)


class TestFoldIsBorrowed:
    def test_fold_is_stable_and_case_insensitive(self):
        assert fold("Born to Run") == fold("BORN TO RUN")

    def test_provenance_is_reported(self):
        from discography import match

        text = match.provenance()
        assert match.IMPLEMENTATION in ("musaeus", "builtin")
        if match.IMPLEMENTATION == "builtin":
            assert match.IMPORT_ERROR and match.IMPORT_ERROR in text

    def test_musaeus_is_used_when_it_is_importable(self):
        """A canary, not a contract. If MUSAEUS is on this machine and the
        borrowing is dead, the tool silently uses a worse matcher."""
        import importlib.util

        from discography import match

        if importlib.util.find_spec("musaeus") is None:
            return
        assert match.IMPLEMENTATION == "musaeus", match.IMPORT_ERROR


# ── Eligibility ───────────────────────────────────────────────────────────────
class TestWhatCountsAsAnAlbum:
    def test_playlist_exports_are_not_albums(self):
        """The largest "album" in the library is "My playlist B" with 183
        tracks. Treating it as a record would make it look like one enormous
        release and would inflate every artist on it past the threshold."""
        assert not _is_real_album("My playlist B")
        assert not _is_real_album("Unknown Album")
        assert not _is_real_album("")
        assert _is_real_album("Nebraska")

    def test_compilation_detection(self):
        assert OwnedArtist("X", albums={"Greatest Hits"}).has_compilation
        assert OwnedArtist("X", albums={"The Best of X"}).has_compilation
        assert not OwnedArtist("X", albums={"Nebraska"}).has_compilation


def _fixture_db(tmp_path: Path) -> Path:
    db = tmp_path / "v.db"
    conn = sqlite3.connect(db)
    conn.execute(
        "CREATE TABLE archive (artist TEXT, album TEXT, status TEXT, "
        "mb_artist_id TEXT, mb_artist_name TEXT)"
    )
    rows = [
        # album-oriented: 3 albums, 6 tracks
        *[("Big Artist", f"Album {i}", "CATALOGUED", "mbid-big", "Big Artist") for i in (1, 1, 2, 2, 3, 3)],
        # one-track artist: the 277-artist majority
        ("Tiny Artist", "Some Album", "CATALOGUED", "", ""),
        # plenty of tracks but all on a playlist -- no real album
        *[("Playlist Only", "My playlist B", "CATALOGUED", "", "") for _ in range(9)],
        # not catalogued, must be ignored
        ("Pending Artist", "A", "DUPE_REVIEW", "", ""),
        ("Pending Artist", "B", "DUPE_REVIEW", "", ""),
    ]
    conn.executemany("INSERT INTO archive VALUES (?,?,?,?,?)", rows)
    conn.commit()
    conn.close()
    return db


class TestEligibility:
    def test_only_album_oriented_artists_are_asked_about(self, tmp_path):
        """THE MEASUREMENT THAT SHAPES THE TOOL. 277 of 464 artists have exactly
        one track. Asking MusicBrainz about them produces "you own 1 track and
        are missing 14 albums" -- true, useless, and it buries the rows that
        matter."""
        artists, stats = load_artists(db=_fixture_db(tmp_path), cache_db=Path("/nonexistent"))
        assert [a.artist for a in artists] == ["Big Artist"]
        # 3, not 4: the DUPE_REVIEW artist is filtered by the status clause before
        # counting, so artists_total describes the population actually considered.
        assert stats["artists_total"] == 3
        assert stats["excluded_too_few_tracks"] >= 1

    def test_an_artist_with_only_playlist_tracks_is_excluded(self, tmp_path):
        """Nine tracks clears the track threshold, but they are all on "My
        playlist B", so there is no album collection to find gaps in."""
        artists, stats = load_artists(db=_fixture_db(tmp_path), cache_db=Path("/nonexistent"))
        assert "Playlist Only" not in [a.artist for a in artists]
        assert stats["excluded_too_few_albums"] >= 1

    def test_non_catalogued_rows_are_ignored(self, tmp_path):
        artists, _ = load_artists(db=_fixture_db(tmp_path), cache_db=Path("/nonexistent"))
        assert "Pending Artist" not in [a.artist for a in artists]

    def test_a_missing_cache_is_not_fatal_and_not_a_false_complete(self, tmp_path):
        """Without the cache an artist has no mbid, which must read as "we could
        not ask" -- never as an empty discography, which would say "you own
        everything"."""
        artists, stats = load_artists(db=_fixture_db(tmp_path), cache_db=Path("/nonexistent"))
        assert stats["unresolvable_no_mbid"] == 0  # this one has an archive mbid
        assert artists[0].mbid == "mbid-big"

    def test_cache_fills_a_missing_mbid(self, tmp_path):
        """The real reason this path exists: The Beatles, Dylan, Springsteen and
        Billy Joel -- the four largest collections -- have no mb_artist_id in the
        archive, but all four resolve from MUSAEUS's cache."""
        db = tmp_path / "v.db"
        conn = sqlite3.connect(db)
        conn.execute(
            "CREATE TABLE archive (artist TEXT, album TEXT, status TEXT, "
            "mb_artist_id TEXT, mb_artist_name TEXT)"
        )
        conn.executemany(
            "INSERT INTO archive VALUES (?,?,?,?,?)",
            [("Beatles, The", f"Album {i}", "CATALOGUED", "", "") for i in (1, 1, 2, 2, 3, 3)],
        )
        conn.commit()
        conn.close()

        cache = tmp_path / "mb.db"
        c2 = sqlite3.connect(cache)
        c2.execute(
            "CREATE TABLE mb_artist (artist_key TEXT PRIMARY KEY, mbid TEXT, "
            "mb_name TEXT, found INTEGER NOT NULL)"
        )
        c2.execute(
            "INSERT INTO mb_artist VALUES (?,?,?,?)",
            ("beatles, the", "b10bbbfc-cf9e-42e0-be17-e2c3e1d2600d", "The Beatles", 1),
        )
        c2.commit()
        c2.close()

        artists, stats = load_artists(db=db, cache_db=cache)
        assert stats["mbid_from_cache"] == 1
        assert artists[0].mbid.startswith("b10bbbfc")

    def test_a_not_found_cache_row_is_not_used(self, tmp_path):
        """found=0 means "looked up, not there". Using its empty mbid would send
        a browse request for an empty artist."""
        db = tmp_path / "v.db"
        conn = sqlite3.connect(db)
        conn.execute(
            "CREATE TABLE archive (artist TEXT, album TEXT, status TEXT, "
            "mb_artist_id TEXT, mb_artist_name TEXT)"
        )
        conn.executemany(
            "INSERT INTO archive VALUES (?,?,?,?,?)",
            [("Obscure", f"Album {i}", "CATALOGUED", "", "") for i in (1, 1, 2, 2, 3, 3)],
        )
        conn.commit()
        conn.close()

        cache = tmp_path / "mb.db"
        c2 = sqlite3.connect(cache)
        c2.execute(
            "CREATE TABLE mb_artist (artist_key TEXT PRIMARY KEY, mbid TEXT, "
            "mb_name TEXT, found INTEGER NOT NULL)"
        )
        c2.execute("INSERT INTO mb_artist VALUES (?,?,?,?)", ("obscure", "", "", 0))
        c2.commit()
        c2.close()

        artists, stats = load_artists(db=db, cache_db=cache)
        assert stats["unresolvable_no_mbid"] == 1
        assert artists[0].mbid == ""


class TestUnavailableIsNotEmpty:
    def test_the_exception_exists_and_is_distinct(self):
        """An artist whose lookup failed has an UNKNOWN discography. Reporting
        zero gaps would say "you own everything" about an artist nobody asked
        about -- the most damaging error this tool could make."""
        from discography.mb import Unavailable

        assert issubclass(Unavailable, RuntimeError)


class TestGapDataclass:
    def test_gap_carries_what_the_csv_needs(self):
        g = Gap(artist="A", album="B", mbid="m", year=1970)
        assert (g.artist, g.album, g.mbid, g.year) == ("A", "B", "m", 1970)
        assert g.may_be_covered_by_compilation is False
