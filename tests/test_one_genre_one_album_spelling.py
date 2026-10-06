"""Grey, 2026-10-06: one genre per artist, and one spelling per album -- by itself.

Ten artists sat under two genres: a credit such as "Stevie Ray Vaughan & Double Trouble"
arrived with its source's genre ("Southern Rock") and, having no MasterLaw ruling of its
own, kept it -- the lead artist's ruling was only used for EMPTY genres. And 51 album
folders were split by capital letters alone ("Kind of Blue" / "Kind Of Blue").
"""

from __future__ import annotations

from pathlib import Path

import pytest

from musaeus.album_spelling import plan_album_spelling, style_score
from musaeus.config import MusicConfig
from musaeus.context import RunContext
from musaeus.db import open_db, upsert_archive
from musaeus.stages.artist_consolidate import ArtistConsolidateStage
from musaeus.stages.genre_validate import GenreValidateStage


@pytest.fixture
def vault(tmp_path: Path):
    cfg = MusicConfig(
        vault_root=tmp_path, inbox=tmp_path / "INBOX", staging=tmp_path / "STAGING",
        quarantine=tmp_path / "Q", runs_root=tmp_path / "RUNS", meta_dir=tmp_path / "MetaData",
        alac_library=tmp_path / "ALAC-Library", db_path=tmp_path / "musaeus.db",
    )  # fmt: skip
    cfg.meta_dir.mkdir(parents=True)
    (cfg.meta_dir / "Genre_Allowed.txt").write_text(
        "Blues\nSouthern Rock\nJazz\nRock\n", encoding="utf-8"
    )
    (cfg.meta_dir / "MasterLaw.csv").write_text(
        "artist,genre\nStevie Ray Vaughan,Blues\nMiles Davis,Jazz\n", encoding="utf-8"
    )
    (cfg.meta_dir / "artist_canon.tsv").write_text("", encoding="utf-8")
    return RunContext.new(cfg, open_db(cfg.db_path), dry_run=False)


def _add(ctx, name, **row):
    f = ctx.config.alac_library / name
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_bytes(b"x")
    upsert_archive(ctx.conn, {"file_path": str(f), "status": "CATALOGUED", "title": name, **row})
    ctx.conn.commit()
    return str(f)


def _get(ctx, fp, col):
    return ctx.conn.execute(f"SELECT {col} FROM archive WHERE file_path=?", (fp,)).fetchone()[0]


# ── one genre per artist ────────────────────────────────────────────────────


def test_a_credit_with_its_own_genre_takes_the_lead_artists_ruling(vault):
    fp = _add(vault, "a.m4a", artist="Stevie Ray Vaughan & Double Trouble", genre="Southern Rock")
    GenreValidateStage().execute(vault)
    assert _get(vault, fp, "genre") == "Blues"
    # borrowed, not a ruling on the credit: a later exact MasterLaw entry still wins
    assert _get(vault, fp, "genre_ruled_at") is None


def test_an_exact_ruling_for_the_credit_still_wins(vault):
    law = vault.config.meta_dir / "MasterLaw.csv"
    law.write_text(law.read_text() + "Stevie Ray Vaughan & Double Trouble,Southern Rock\n")
    fp = _add(vault, "a.m4a", artist="Stevie Ray Vaughan & Double Trouble", genre="Southern Rock")
    GenreValidateStage().execute(vault)
    assert _get(vault, fp, "genre") == "Southern Rock"


def test_a_ruled_credit_is_reported_not_changed(vault):
    # The stage adds genre_ruled_at on its first run; a fresh catalogue lacks it.
    GenreValidateStage().execute(vault)
    fp = _add(vault, "a.m4a", artist="Stevie Ray Vaughan & Double Trouble", genre="Southern Rock")
    vault.conn.execute("UPDATE archive SET genre_ruled_at=datetime('now') WHERE file_path=?", (fp,))
    vault.conn.commit()
    GenreValidateStage().execute(vault)
    assert _get(vault, fp, "genre") == "Southern Rock"


# ── one spelling per album ──────────────────────────────────────────────────


def _row(artist, album, filed=False, mb=None):
    return {"file_path": f"{artist}/{album}", "artist": artist, "album": album,
            "finalized_at": "x" if filed else None, "mb_artist_name": mb}  # fmt: skip


def test_a_new_song_takes_the_spelling_already_filed():
    filed = _row("Miles Davis", "Kind Of Blue", filed=True)  # even the less official one
    new = _row("Miles Davis", "Kind of Blue")
    assert [(r["file_path"], b) for r, b in plan_album_spelling([filed, new])] == [
        ("Miles Davis/Kind of Blue", "Kind Of Blue")
    ]


def test_with_two_spellings_filed_the_official_style_wins():
    rows = [_row("AC/DC", "Back In Black", True), _row("AC/DC", "Back In Black", True),
            _row("AC/DC", "Back in Black", True)]  # fmt: skip
    assert {b for _r, b in plan_album_spelling(rows)} == {"Back in Black"}


def test_a_credit_shares_its_lead_artists_album_spelling():
    rows = [_row("Stevie Ray Vaughan", "The Sky Is Crying", True),
            _row("Stevie Ray Vaughan & Double Trouble", "The Sky is Crying")]  # fmt: skip
    assert [b for _r, b in plan_album_spelling(rows)] == ["The Sky Is Crying"]


def test_apostrophes_and_a_final_full_stop_are_left_alone():
    rows = [_row("Marvin Gaye", "What’s Going On", True), _row("Marvin Gaye", "What's Going On"),
            _row("Marvin Gaye", "M.P.G.", True), _row("Marvin Gaye", "M.P.G")]  # fmt: skip
    assert plan_album_spelling(rows) == []


def test_the_same_album_name_by_two_artists_is_two_albums():
    rows = [_row("Weezer", "Weezer", True), _row("Weezer Tribute", "WEEZER")]
    assert plan_album_spelling(rows) == []


@pytest.mark.parametrize(
    ("better", "worse"),
    [("Kind of Blue", "Kind Of Blue"), ("Chicago VI", "Chicago Vi"), ("OU812", "Ou812"),
     ("ABC", "Abc"), ("Déjà Vu", "Déjà vu"), ("Tres Hombres", "Tres hombres")],
)  # fmt: skip
def test_the_official_style(better, worse):
    assert style_score(better) > style_score(worse)


def test_the_stage_respells_and_records_it(vault):
    filed = _add(vault, "f.m4a", artist="Miles Davis", album="Kind of Blue")
    vault.conn.execute("UPDATE archive SET finalized_at='x' WHERE file_path=?", (filed,))
    new = _add(vault, "n.m4a", artist="Miles Davis", album="Kind Of Blue")
    vault.conn.commit()
    ArtistConsolidateStage().execute(vault)
    assert _get(vault, new, "album") == "Kind of Blue"
    ev = vault.conn.execute(
        "SELECT old_value, new_value FROM events WHERE event_type='ALBUM_SPELLING_UNIFIED'"
    ).fetchall()
    assert [tuple(e) for e in ev] == [("Kind Of Blue", "Kind of Blue")]
