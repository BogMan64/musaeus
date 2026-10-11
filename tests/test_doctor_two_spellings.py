"""`musaeus doctor`: one act filed under two spellings (Grey, 2026-10-11)."""

from __future__ import annotations

from pathlib import Path

from musaeus import doctor
from musaeus.config import MusicConfig
from musaeus.db import open_db, upsert_archive


def _cfg(tmp_path: Path) -> MusicConfig:
    c = MusicConfig(
        vault_root=tmp_path,
        inbox=tmp_path / "INBOX",
        staging=tmp_path / "STAGING",
        quarantine=tmp_path / "QUARANTINE",
        runs_root=tmp_path / "RUNS",
        meta_dir=tmp_path / "MetaData",
        alac_library=tmp_path / "Libraries" / "ALAC_Library",
        db_path=tmp_path / "musaeus.db",
    )
    c.ensure_dirs()
    return c


def _finding(cfg, artists):
    conn = open_db(cfg.db_path)
    for i, a in enumerate(artists):
        upsert_archive(conn, {"file_path": f"/m/{i}.m4a", "status": "CATALOGUED", "artist": a})
    conn.commit()
    conn.close()
    rep = doctor.Report()
    doctor._one_act_two_spellings(cfg, rep)
    (f,) = [x for x in rep.findings if x.check == "one act, two spellings"]
    return f


def test_and_versus_ampersand_is_one_act(tmp_path):
    f = _finding(_cfg(tmp_path), ["Jan & Dean", "Jan And Dean", "Blondie"])
    assert f.level == "warn" and f.count == 1
    assert "Jan & Dean" in f.detail and "Jan And Dean" in f.detail


def test_punctuation_accents_and_the(tmp_path):
    f = _finding(
        _cfg(tmp_path),
        ["Run-DMC", "Run-D.M.C.", "José Feliciano", "Jose Feliciano", "The Who", "Who, The"],
    )
    assert f.count == 2  # Run-DMC and Feliciano; "The Who"/"Who, The" file as one


def test_distinct_acts_are_fine(tmp_path):
    f = _finding(_cfg(tmp_path), ["Steve Miller", "Steve Miller Band", "Sonny & Cher"])
    assert f.level == "ok"
