"""Review of slice C (#147), findings 7 and 8: `musaeus review`.

7. generate rewrote the sheets with approve blank: running it again before
   apply wiped every mark.
8. apply updated the one sample file a row names, counted it applied whether
   or not anything changed, and never checked current_value.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from musaeus.approval import (
    ReviewEntry,
    apply_approved_fixes,
    read_review_tsv,
    write_review_tsv,
)
from musaeus.db import open_db, upsert_archive


def _entry(**kw) -> ReviewEntry:
    base = {"file_path": "/x/1.m4a", "field_name": "artist", "current_value": "Beetles",
            "suggested_value": "Beatles, The", "source": "canon_fuzzy", "confidence": 0.88}  # fmt: skip
    base.update(kw)
    return ReviewEntry(**base)


def test_generating_again_keeps_the_marks(tmp_path):
    sheet = tmp_path / "artist_review.tsv"
    write_review_tsv(sheet, [_entry()])
    marked = read_review_tsv(sheet)
    marked[0].approve = "yes"
    write_review_tsv(sheet, marked)
    write_review_tsv(sheet, [_entry()])  # generate again
    assert read_review_tsv(sheet)[0].approve == "yes", "the mark was wiped"


def test_a_year_typed_by_hand_survives_a_regenerate(tmp_path):
    sheet = tmp_path / "album_review.tsv"
    row = _entry(field_name="year", current_value="", suggested_value="", source="missing")
    write_review_tsv(sheet, [row])
    typed = read_review_tsv(sheet)
    typed[0].suggested_value, typed[0].approve = "1965", "yes"
    write_review_tsv(sheet, typed)
    write_review_tsv(sheet, [_entry(field_name="year", current_value="", suggested_value="",
                                    source="missing")])  # fmt: skip
    got = read_review_tsv(sheet)[0]
    assert (got.suggested_value, got.approve) == ("1965", "yes")


@pytest.fixture
def conn(tmp_path: Path):
    c = open_db(tmp_path / "m.db")
    for n in range(3):
        upsert_archive(c, {"file_path": f"/x/{n}.m4a", "status": "CATALOGUED", "artist": "Beetles"})
    c.commit()
    yield c
    c.close()


def test_an_artist_correction_reaches_every_row(conn, tmp_path):
    sheet = tmp_path / "artist_review.tsv"
    write_review_tsv(sheet, [_entry(approve="yes")])
    report = apply_approved_fixes(conn, sheet, "run1")
    artists = {r[0] for r in conn.execute("SELECT artist FROM archive")}
    assert artists == {"Beatles, The"}, "only the sample file was changed"
    assert report.applied == 1


def test_a_row_that_matches_nothing_is_not_counted(conn, tmp_path):
    sheet = tmp_path / "artist_review.tsv"
    write_review_tsv(sheet, [_entry(current_value="Nobody", approve="yes")])
    report = apply_approved_fixes(conn, sheet, "run1")
    assert report.applied == 0
    assert report.errors and "nothing matched" in report.errors[0]


# ── Pre-merge review of #173 ──────────────────────────────────────────────────


def test_a_note_grey_typed_keeps_the_mark(tmp_path):
    sheet = tmp_path / "artist_review.tsv"
    write_review_tsv(sheet, [_entry()])
    edited = read_review_tsv(sheet)
    edited[0].approve, edited[0].notes = "yes", "checked with the album sleeve"
    write_review_tsv(sheet, edited)
    write_review_tsv(sheet, [_entry()])  # generate again
    got = read_review_tsv(sheet)[0]
    assert (got.approve, got.notes) == ("yes", "checked with the album sleeve")


def test_an_artist_correction_leaves_ghosts_alone(conn, tmp_path):
    upsert_archive(conn, {"file_path": "/x/gone.m4a", "status": "GHOST", "artist": "Beetles"})
    conn.commit()
    sheet = tmp_path / "artist_review.tsv"
    write_review_tsv(sheet, [_entry(approve="yes")])
    apply_approved_fixes(conn, sheet, "run1")
    ghost = conn.execute("SELECT artist FROM archive WHERE status='GHOST'").fetchone()[0]
    assert ghost == "Beetles"


def test_the_apply_command_shows_what_it_did_not_apply(tmp_path, monkeypatch, capsys):
    from musaeus import approval
    from musaeus.config import MusicConfig

    cfg = MusicConfig(
        vault_root=tmp_path,
        inbox=tmp_path / "INBOX",
        staging=tmp_path / "STAGING",
        quarantine=tmp_path / "QUARANTINE",
        runs_root=tmp_path / "RUNS",
        meta_dir=tmp_path / "MetaData",
        alac_library=tmp_path / "Libraries" / "ALAC_Library",
        db_path=tmp_path / "musaeus.db",
    )
    cfg.ensure_dirs()
    write_review_tsv(
        cfg.meta_dir / "review" / "artist_review.tsv",
        [_entry(current_value="Nobody", approve="yes")],
    )
    monkeypatch.setattr("musaeus.config.get_config", lambda: cfg)
    code = approval.cmd_review_apply()
    assert code == 1
    assert "nothing matched" in capsys.readouterr().err
