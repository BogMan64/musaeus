"""
Tests for GenreLaw and GenreValidateStage.

The behaviour worth pinning here is the separator folding and the
fill/flag split. Measured against the live library while building this:
naively comparing library genre to MasterLaw reported 2,240 mismatches,
but 881 of those were rows with NO genre at all (a fill, not a conflict)
and ~1,000 more were "Disco-Electronic" vs "Disco/Electronic" -- the same
genre, differing only because Sanitize strips "/" for filesystem safety.
Only 375 were real. A validator that cannot tell those three apart is
worse than none, because it trains you to ignore it.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from types import SimpleNamespace

import inspect
from unittest.mock import MagicMock
import pytest

from musaeus.canon.genre_law import GenreLaw
from musaeus.stages.genre_validate import GenreValidateStage


@pytest.fixture
def law_csv(tmp_path: Path) -> Path:
    p = tmp_path / "MasterLaw.csv"
    p.write_text(
        "artist,genre\n"
        "AC/DC,Hard Rock\n"
        'ABBA,"Pop, Rock"\n'
        "8D Tunes,Disco/Electronic\n"
        "  Spaced  Out  ,Jazz\n",
        encoding="utf-8",
    )
    return p


class TestGenreLaw:
    def test_lookup_is_case_and_space_insensitive(self, law_csv):
        law = GenreLaw(law_csv)
        assert law.genre_for("abba") == "Pop, Rock"
        assert law.genre_for("  ABBA ") == "Pop, Rock"
        assert law.genre_for("spaced out") == "Jazz"

    def test_unknown_artist_returns_none(self, law_csv):
        assert GenreLaw(law_csv).genre_for("Nobody At All") is None

    def test_agrees_returns_none_when_law_has_no_opinion(self, law_csv):
        # None and False mean different things: "no opinion" must not be
        # reported as a conflict.
        assert GenreLaw(law_csv).agrees("Nobody At All", "Rock") is None

    def test_separator_difference_is_not_a_conflict(self, law_csv):
        """The single most important case.

        Sanitize strips "/" from genres for filesystem safety, so the
        library stores "Disco-Electronic" for MasterLaw's
        "Disco/Electronic". Treating that as a mismatch generated about a
        thousand false conflicts on the real library.
        """
        law = GenreLaw(law_csv)
        assert law.agrees("8D Tunes", "Disco-Electronic") is True
        assert law.agrees("8D Tunes", "Disco/Electronic") is True

    def test_real_disagreement_still_flagged(self, law_csv):
        assert GenreLaw(law_csv).agrees("AC/DC", "Rock") is False

    def test_missing_file_is_not_an_error(self, tmp_path):
        law = GenreLaw(tmp_path / "nope.csv")
        assert len(law) == 0
        assert law.genre_for("ABBA") is None


def _ctx(tmp_path: Path, law_csv: Path, rows):
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute(
        "CREATE TABLE archive (file_path TEXT, status TEXT, artist TEXT, "
        "album TEXT, track INT, genre TEXT)"
    )
    conn.executemany("INSERT INTO archive VALUES (?,?,?,?,?,?)", rows)
    conn.commit()
    return SimpleNamespace(
        conn=conn,
        config=SimpleNamespace(meta_dir=law_csv.parent),
        get=lambda k, d=None: d,
        record_stage=lambda _r: None,
        log_event=lambda *a, **k: None,
    )


class TestGenreValidateStage:
    def test_fills_empty_genre_but_never_overwrites(self, tmp_path, law_csv):
        ctx = _ctx(
            tmp_path,
            law_csv,
            [
                ("/a.m4a", "CATALOGUED", "ABBA", "Album", 1, ""),
                ("/b.m4a", "CATALOGUED", "AC/DC", "Album", 2, "Rock"),
                ("/c.m4a", "CATALOGUED", "8D Tunes", "Album", 3, "Disco-Electronic"),
                ("/d.m4a", "CATALOGUED", "Nobody", "Album", 4, "Polka"),
            ],
        )
        result = GenreValidateStage().run(ctx)  # type: ignore[arg-type]

        assert result.files_changed == 1  # only the empty one
        genres = dict(ctx.conn.execute("SELECT file_path, genre FROM archive").fetchall())
        assert genres["/a.m4a"] == "Pop, Rock"  # filled
        assert genres["/b.m4a"] == "Rock"  # conflict, left ALONE
        assert genres["/c.m4a"] == "Disco-Electronic"  # separator, untouched
        assert genres["/d.m4a"] == "Polka"  # law has no opinion

    def test_conflicts_are_reported_not_corrected(self, tmp_path, law_csv):
        ctx = _ctx(tmp_path, law_csv, [("/b.m4a", "CATALOGUED", "AC/DC", "Al", 1, "Rock")])
        result = GenreValidateStage().run(ctx)  # type: ignore[arg-type]
        assert any("CONFLICTS (report only): 1 file(s) across 1 artist" in n for n in result.notes)
        assert any("Hard Rock" in n for n in result.notes)
        assert result.files_changed == 0

    def test_conflicts_are_grouped_by_artist_not_repeated_per_file(self, tmp_path, law_csv):
        """One decision per artist, however many tracks they have.

        The first live run printed the same AC/DC line 25 times and pushed
        every other artist out of the report -- AC/DC has 78 tracks. A
        report you have to scroll past is one you stop reading.
        """
        rows = [(f"/ac{i}.m4a", "CATALOGUED", "AC/DC", "Al", i, "Rock") for i in range(5)]
        rows.append(("/z.m4a", "CATALOGUED", "8D Tunes", "Al", 9, "Jazz"))
        ctx = _ctx(tmp_path, law_csv, rows)
        result = GenreValidateStage().run(ctx)  # type: ignore[arg-type]

        acdc = [n for n in result.notes if "AC/DC" in n]
        assert len(acdc) == 1, acdc
        assert "(5 files)" in acdc[0]
        # The quieter artist must survive into the report, not be crowded out.
        assert any("8D Tunes" in n for n in result.notes)
        assert any("across 2 artist(s)" in n for n in result.notes)

    def test_dry_run_writes_nothing(self, tmp_path, law_csv):
        ctx = _ctx(tmp_path, law_csv, [("/a.m4a", "CATALOGUED", "ABBA", "Al", 1, "")])
        result = GenreValidateStage().dry_run(ctx)  # type: ignore[arg-type]
        assert result.files_changed == 1
        assert ctx.conn.execute("SELECT genre FROM archive").fetchone()[0] == ""

    def test_no_masterlaw_is_a_clean_noop(self, tmp_path):
        missing = tmp_path / "empty_dir" / "MasterLaw.csv"
        missing.parent.mkdir()
        ctx = _ctx(tmp_path, missing, [("/a.m4a", "CATALOGUED", "ABBA", "Al", 1, "")])
        result = GenreValidateStage().run(ctx)  # type: ignore[arg-type]
        assert result.files_changed == 0
        assert any("nothing to validate against" in n for n in result.notes)



@pytest.fixture
def vault_with_genres(tmp_path: Path):
    """A vault whose allowed-genre list deliberately omits the genres given,
    so the vocabulary findings fire."""
    from musaeus.config import MusicConfig
    from musaeus.context import RunContext
    from musaeus.db import open_db, upsert_archive

    def _make(*genres: str) -> RunContext:
        cfg = MusicConfig(
            vault_root=tmp_path, inbox=tmp_path / "INBOX", staging=tmp_path / "STAGING",
            quarantine=tmp_path / "QUARANTINE", runs_root=tmp_path / "RUNS",
            meta_dir=tmp_path / "MetaData", alac_library=tmp_path / "ALAC-Library",
            db_path=tmp_path / "musaeus.db",
        )
        cfg.meta_dir.mkdir(parents=True, exist_ok=True)
        # An allow-list that admits something else entirely, so every genre
        # below is "in use but not listed".
        (cfg.meta_dir / "Genre_Allowed.txt").write_text("Jazz\n", encoding="utf-8")
        # The vocabulary block is gated on a non-empty law -- without
        # MasterLaw.csv the whole check short-circuits and reports nothing.
        (cfg.meta_dir / "MasterLaw.csv").write_text(
            "artist,genre\nSomebody Else,Jazz\n", encoding="utf-8")
        ctx = RunContext.new(cfg, open_db(cfg.db_path), dry_run=False)
        for i, g in enumerate(genres):
            f = cfg.alac_library / f"t{i}.m4a"
            f.parent.mkdir(parents=True, exist_ok=True)
            f.write_bytes(b"x")
            upsert_archive(ctx.conn, {"file_path": str(f), "status": "CATALOGUED",
                                      "artist": f"Artist {i}", "title": f"T{i}", "genre": g})
        ctx.conn.commit()
        return ctx

    return _make

class TestAGenreContainingACommaIsNotAmbiguous:
    """A genre VALUE can itself contain ", ".

    The live library holds one called 'Pop, Rock' across 72 tracks. Joining
    findings with a bare ", " rendered two of them as three and sent a
    reader hunting a missing 'Pop' and 'Rock' that were never missing --
    both are in Genre_Allowed.txt. Found 2026-09-05 running the checks
    against the real library; the message cost a detour before the data did.

    Asserted on the OUTPUT rather than on the source: an earlier version of
    this test grepped for joins without repr() and flagged the artist list,
    which legitimately needs none. It failed for the wrong reason.
    """

    def test_a_comma_bearing_genre_is_reported_as_one_item(self, vault_with_genres):
        ctx = vault_with_genres("Pop, Rock", "Doo Wop")
        out = GenreValidateStage().verify_effect(ctx, MagicMock(files_changed=1))
        vocab = [line for line in out if "outside the closed vocabulary" in line
                 or "absent from Genre_Allowed" in line]
        assert vocab, "expected the vocabulary findings"
        for line in vocab:
            # the count the message states must match what a reader can see
            stated = int(line.split()[0])
            assert line.count("'") == stated * 2, (
                f"{stated} item(s) claimed but {line.count(chr(39)) // 2} delimited: {line}"
            )
            assert "'Pop, Rock'" in line, (
                "a genre containing a comma must render as ONE quoted item"
            )
