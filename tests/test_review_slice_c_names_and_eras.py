"""Review of slice C (#147), findings 5, 6, 9 and 10."""

from __future__ import annotations

from pathlib import Path

import pytest

from musaeus.config import MusicConfig
from musaeus.context import RunContext
from musaeus.db import open_db, upsert_archive
from musaeus.stages import acousticid, acoustid_name
from musaeus.stages.acousticid import _pick_matching_recording


def _rec(rid, title, *artists):
    return {"id": rid, "title": title, "artists": [{"name": a} for a in artists]}


def test_a_partial_artist_name_is_not_the_artist():
    """6: 'Dion' took Celine Dion's recording (substring either way)."""
    recs = [_rec("celine", "Only Love", "Celine Dion")]
    assert _pick_matching_recording(recs, "Dion", "Only Love") is None


def test_the_recording_with_the_files_own_title_wins_over_a_live_one():
    """5: the first title containing the file's won, whatever the order."""
    recs = [
        _rec("live", "Born to Run (live at Hammersmith Odeon)", "Bruce Springsteen"),
        _rec("studio", "Born to Run", "Bruce Springsteen"),
    ]
    assert _pick_matching_recording(recs, "Bruce Springsteen", "Born To Run") == "studio"


def test_two_different_longer_titles_are_a_guess_and_refused():
    recs = [
        _rec("12in", 'Hot Stuff (12" version)', "Donna Summer"),
        _rec("live", "Hot Stuff (live)", "Donna Summer"),
    ]
    assert _pick_matching_recording(recs, "Donna Summer", "Hot Stuff") is None


def test_one_longer_title_still_completes_a_cut_short_one():
    recs = [_rec("12in", 'Hot Stuff (12" version)', "Donna Summer")]
    assert _pick_matching_recording(recs, "Donna Summer", "Hot Stuff") == "12in"


def test_a_duet_credit_matches_its_two_artists():
    recs = [_rec("duet", "Don't Go Breaking My Heart", "Elton John", "Kiki Dee")]
    got = _pick_matching_recording(recs, "Elton John & Kiki Dee", "Don't Go Breaking My Heart")
    assert got == "duet"


@pytest.fixture
def ctx(tmp_path: Path) -> RunContext:
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
    return RunContext.new(cfg, open_db(cfg.db_path), dry_run=False)


def test_one_unreadable_file_does_not_stop_the_others(ctx, monkeypatch):
    """9: an fpcalc error on one file broke out of the whole loop."""
    for name in ("01 - Broken.m4a", "02 - Yesterday.m4a"):
        p = ctx.config.alac_archive / "Unsorted" / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(b"x")
        upsert_archive(ctx.conn, {"file_path": str(p), "status": "CATALOGUED"})
    ctx.conn.commit()
    object.__setattr__(ctx.config, "acousticid_api_key", "fake-key")

    def fpcalc(path):
        if "Broken" in path:
            raise ValueError("fpcalc timed out after 60s")
        return 125.0, "AQAA"

    monkeypatch.setattr(acousticid, "_fpcalc", fpcalc)
    monkeypatch.setattr(acousticid, "_acousticid_query", lambda *a: [{"x": 1}])
    monkeypatch.setattr(
        acousticid, "name_from_results", lambda results, hint: ("The Beatles", "Yesterday", 0.99)
    )
    acoustid_name.AcoustIDNameStage().run(ctx)
    named = ctx.conn.execute(
        "SELECT COUNT(*) FROM archive WHERE artist = 'The Beatles'"
    ).fetchone()[0]
    assert named == 1, "the unreadable first file stopped the second being named"


def test_an_era_list_uses_the_recordings_year(ctx, tmp_path):
    """10: '409' (1962) on a 2012 compilation went only to the 2010s list."""
    from musaeus.db import ensure_columns
    from musaeus.stages.playlist import PlaylistStage

    song = ctx.config.alac_archive / "Rock" / "Beach Boys" / "X" / "409.m4a"
    song.parent.mkdir(parents=True)
    song.write_bytes(b"x")
    upsert_archive(
        ctx.conn,
        {"file_path": str(song), "status": "CATALOGUED", "genre": "Rock",
         "artist": "The Beach Boys", "title": "409", "year": "2012"},
    )  # fmt: skip
    ensure_columns(ctx.conn, (("original_year", "TEXT"),))
    ctx.conn.execute("UPDATE archive SET original_year = '1962'")
    ctx.conn.commit()
    PlaylistStage().run(ctx)
    eras = {p.name for p in ctx.config.playlists.rglob("Era_*.m3u8")}
    assert any("1960s" in n for n in eras), eras
    assert not any("2010s" in n for n in eras), eras
