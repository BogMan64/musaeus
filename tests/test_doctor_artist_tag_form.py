"""doctor asks whether archive.artist is in the form the world can read.

MUSAEUS stored "Beatles, The". That files under B, which is right for a
folder-browsed library, and it is the wrong thing for the one field every
external service reads. artist_form.py measured it on 2026-08-29: 376 of 839
cached misses were in `X, The` form, and 0 of 2,158 hits were. It went on to
break five separate things -- source lookups, MusicBrainz, MasterLaw
membership tests, CAR match-back, playlist fallback -- each patched where it
was found. The migration of 2026-09-16 converted 1,914 rows; this guard is
what stops the convention drifting back.

THE PREDICATE IS THE WHOLE POINT OF THIS FILE.

The guard was first written with `has_article()`, which is a correct function
being asked the wrong question. Its docstring says what it does -- "True when
the two forms differ -- i.e. the name carries an article" -- and "The
Beatles" carries an article just as surely as "Beatles, The" does. So the
guard reported all 1,914 rows still in sort form in the same breath as the
migration reporting all 1,914 converted, and both were telling the truth
about different questions. A guard that cannot go green is worse than no
guard: it trains you to ignore it.

The sort-form test is whether converting to natural form CHANGES the string.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from musaeus.artist_form import has_article, natural_form
from musaeus.config import MusicConfig
from musaeus.doctor import Report, _artist_tag_is_natural_form


@pytest.fixture
def cfg(tmp_path) -> MusicConfig:
    db = tmp_path / "musaeus.db"
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE archive (artist TEXT, status TEXT)")
    conn.commit()
    conn.close()
    return MusicConfig(
        vault_root=tmp_path,
        inbox=tmp_path / "INBOX",
        staging=tmp_path / "STAGING",
        quarantine=tmp_path / "Q",
        runs_root=tmp_path / "RUNS",
        meta_dir=tmp_path / "MetaData",
        alac_library=tmp_path / "L",
        db_path=db,
    )


def _artists(cfg, *names: str) -> None:
    conn = sqlite3.connect(cfg.db_path)
    conn.executemany(
        "INSERT INTO archive (artist, status) VALUES (?, 'CATALOGUED')",
        [(n,) for n in names],
    )
    conn.commit()
    conn.close()


def _only(rep: Report):
    found = [f for f in rep.findings if f.check == "artist tag form"]
    assert len(found) == 1, f"expected one finding, got {found}"
    return found[0]


class TestTheMigratedStateIsGreen:
    """The state the library is actually in, as of 2026-09-16."""

    def test_natural_form_passes(self, cfg):
        _artists(cfg, "The Beatles", "The Who", "The Chieftains")
        rep = Report()
        _artist_tag_is_natural_form(cfg, rep)
        assert _only(rep).level == "ok"

    def test_an_article_bearing_name_is_not_by_itself_a_finding(self, cfg):
        """The regression, stated as a test.

        Every one of these carries an article, so the first version of the
        guard flagged all of them -- immediately after they had been migrated
        correctly. If this test fails, the predicate has slipped back to
        has_article().
        """
        _artists(cfg, "The Beatles", "El Gran Combo", "Los Lobos", "La Roux")
        rep = Report()
        _artist_tag_is_natural_form(cfg, rep)
        f = _only(rep)
        assert f.level == "ok", f"article-bearing natural form was flagged: {f.detail}"

    def test_a_name_with_no_article_passes(self, cfg):
        _artists(cfg, "Nine Inch Nails", "Blur", "Portishead")
        rep = Report()
        _artist_tag_is_natural_form(cfg, rep)
        assert _only(rep).level == "ok"


class TestSortFormIsStillCaught:
    """Green on correct data is only half of it -- it must still go red."""

    def test_the_classic_offender(self, cfg):
        _artists(cfg, "Beatles, The")
        rep = Report()
        _artist_tag_is_natural_form(cfg, rep)
        f = _only(rep)
        assert f.level == "warn"
        assert "Beatles, The" in f.detail

    def test_it_counts_tracks_not_artists(self, cfg):
        _artists(cfg, "Who, The", "Who, The", "Who, The", "Chieftains, The")
        rep = Report()
        _artist_tag_is_natural_form(cfg, rep)
        f = _only(rep)
        assert f.count == 4, "4 tracks across 2 artists"
        assert "2 artist(s)" in f.detail

    def test_a_non_english_sort_form_is_caught_too(self, cfg):
        """The library holds Spanish, French, Dutch and German names, and the
        sort form was applied to those as well."""
        _artists(cfg, "Gran Combo, El")
        rep = Report()
        _artist_tag_is_natural_form(cfg, rep)
        assert _only(rep).level == "warn"

    def test_one_offender_among_many_good_rows_still_reports(self, cfg):
        _artists(cfg, *(["The Beatles"] * 50), "Stooges, The")
        rep = Report()
        _artist_tag_is_natural_form(cfg, rep)
        f = _only(rep)
        assert f.level == "warn"
        assert f.count == 1


class TestProtectedNamesAreNotMangled:
    """De La Soul has been split into "La Soul, De" three separate times.

    The guard must not become the fourth. It reads the same transforms the
    migration used, so if a protected name ever round-trips wrongly this goes
    red against correct data and the mistake surfaces here rather than in the
    library.
    """

    @pytest.mark.parametrize(
        "name", ["De La Soul", "Los Lobos", "The The", "El Gran Combo", "Los Angeles Negros"]
    )
    def test_it_passes_a_protected_name(self, cfg, name):
        _artists(cfg, name)
        rep = Report()
        _artist_tag_is_natural_form(cfg, rep)
        f = _only(rep)
        assert f.level == "ok", f"{name!r} would be 'corrected' to {natural_form(name)!r}"


class TestThePredicateItself:
    def test_has_article_cannot_tell_the_forms_apart(self):
        """Pinning WHY the guard may not use it, so nobody re-adopts it.

        This is not a complaint about has_article -- it answers its own
        question correctly. It is a record that its question is a different
        one.
        """
        assert has_article("Beatles, The") is True
        assert has_article("The Beatles") is True

    def test_natural_form_comparison_can(self):
        assert natural_form("Beatles, The") != "Beatles, The"
        assert natural_form("The Beatles") == "The Beatles"

    def test_the_guard_does_not_import_has_article(self):
        """A reviewer swapping the predicate back would pass every data test
        above only if they also reproduced the bug -- but the import is the
        cheap, unambiguous tell, and it fails loudly."""
        src = (Path(__file__).resolve().parents[1] / "musaeus" / "doctor.py").read_text()
        body = src[src.index("def _artist_tag_is_natural_form") :]
        body = body[: body.index("\ndef _")]
        assert "has_article" not in body.replace("has_article()", ""), (
            "the sort-form guard must compare against natural_form, not ask "
            "whether an article is present"
        )


class TestItDoesNotBreakTheCaller:
    def test_no_database_is_skipped_not_failed(self, cfg):
        Path(cfg.db_path).unlink()
        rep = Report()
        _artist_tag_is_natural_form(cfg, rep)
        assert _only(rep).level == "ok"

    def test_a_table_without_the_columns_warns_rather_than_raising(self, cfg):
        conn = sqlite3.connect(cfg.db_path)
        conn.execute("DROP TABLE archive")
        conn.execute("CREATE TABLE archive (something_else TEXT)")
        conn.commit()
        conn.close()
        rep = Report()
        _artist_tag_is_natural_form(cfg, rep)
        assert _only(rep).level == "warn"

    def test_blank_and_null_artists_are_ignored(self, cfg):
        conn = sqlite3.connect(cfg.db_path)
        conn.executemany(
            "INSERT INTO archive (artist, status) VALUES (?, 'CATALOGUED')",
            [(None,), ("",), ("   ",), ("The Beatles",)],
        )
        conn.commit()
        conn.close()
        rep = Report()
        _artist_tag_is_natural_form(cfg, rep)
        assert _only(rep).level == "ok"
