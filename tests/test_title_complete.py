"""Titles cut short at the source are finished from the MusicBrainz recording (2026-10-06).

The USB1 source tags were cut at 36 characters; 15 library titles were completed by hand on
2026-10-06 ("Ain't No Fun (Waiting 'round To Be A" -> "... Millionaire)") and Grey asked that
MUSAEUS catch them itself. Only a title that is the START of its recording's title is changed.
"""

from __future__ import annotations

import pytest

from musaeus.config import MusicConfig
from musaeus.context import RunContext
from musaeus.db import open_db, upsert_archive
from musaeus.stages import title_complete as tc
from musaeus.stages.album_fill import Unavailable


@pytest.mark.parametrize(
    ("title", "mb", "want"),
    [
        ("Ain't No Fun (Waiting 'round To Be A", "Ain’t No Fun (Waiting ’Round to Be a Millionaire)",
         "Ain't No Fun (Waiting 'Round to Be a Millionaire)"),
        ("Ain't No Fun (Waiting Round To Be A", "Ain’t No Fun (Waiting Round to Be a Millionaire)",
         "Ain't No Fun (Waiting Round to Be a Millionaire)"),
        ("Homelands [Jigs The Kesh, The Blackt", "Homelands [Jigs - the Kesh, the Blackthorn Stick]",
         "Homelands [Jigs - the Kesh, the Blackthorn Stick]"),
        ("Nights In White Sa, The", "Nights in White Satin", "Nights in White Satin"),
        ("Under The Brid", "Under the Bridge", "Under the Bridge"),
        ("Solsbury Hill (Remastered)", "Solsbury Hill", None),  # differs, not cut
        ("Under the Bridge", "Under the Bridge", None),  # the same: nothing to finish
        ("Lola", "Lola Versus Powerman", None),  # too short to say it was cut
        ("Hot Stuff", "Hot Stuff (12\" version)", "Hot Stuff (12\" version)"),
    ],
)  # fmt: skip
def test_only_a_cut_off_start_is_finished(title, mb, want):
    assert tc.completed_title(title, mb) == want


@pytest.fixture
def ctx(tmp_path):
    cfg = MusicConfig(
        vault_root=tmp_path, inbox=tmp_path / "INBOX", staging=tmp_path / "STAGING",
        quarantine=tmp_path / "Q", runs_root=tmp_path / "RUNS", meta_dir=tmp_path / "MetaData",
        alac_library=tmp_path / "ALAC-Library", db_path=tmp_path / "musaeus.db",
    )  # fmt: skip
    c = RunContext.new(cfg, open_db(cfg.db_path), dry_run=False)
    from musaeus.stages.acousticid import _ensure_columns

    _ensure_columns(c.conn)  # acousticid_recording arrives with the AcoustID step
    rows = [("Under The Brid", "mb-rhcp"), ("Lola", "mb-kinks"), ("No Recording Known", "")]
    for i, (title, mbid) in enumerate(rows):
        fp = f"/l/{i}.m4a"
        upsert_archive(
            c.conn, {"file_path": fp, "status": "CATALOGUED", "title": title, "artist": "A"}
        )
        # upsert_archive keeps to the core columns; the AcoustID step writes this one itself
        c.conn.execute(
            "UPDATE archive SET acousticid_recording = ? WHERE file_path = ?", (mbid, fp)
        )
    c.conn.commit()
    return c


def _title(ctx, fp):
    return ctx.conn.execute(
        "SELECT title, title_checked_at FROM archive WHERE file_path=?", (fp,)
    ).fetchone()


def test_the_stage_finishes_stamps_and_records(ctx, monkeypatch):
    titles = {"mb-rhcp": "Under the Bridge", "mb-kinks": "Lola"}
    monkeypatch.setattr(tc, "recording_title", lambda mbid: titles[mbid])
    monkeypatch.setattr(tc, "_MB_RATE_S", 0)
    tc.TitleCompleteStage().execute(ctx)
    assert _title(ctx, "/l/0.m4a")[0] == "Under the Bridge"
    assert _title(ctx, "/l/1.m4a")[0] == "Lola" and _title(ctx, "/l/1.m4a")[1]  # checked, unchanged
    assert _title(ctx, "/l/2.m4a")[1] is None  # no recording: never asked
    ev = ctx.conn.execute(
        "SELECT old_value, new_value FROM events WHERE event_type='TITLE_COMPLETED'"
    ).fetchall()
    assert [tuple(e) for e in ev] == [("Under The Brid", "Under the Bridge")]


def test_a_lookup_that_cannot_be_made_is_asked_again_next_run(ctx, monkeypatch):
    def offline(mbid):
        raise Unavailable("offline")

    monkeypatch.setattr(tc, "recording_title", offline)
    monkeypatch.setattr(tc, "_MB_RATE_S", 0)
    tc.TitleCompleteStage().execute(ctx)
    assert tuple(_title(ctx, "/l/0.m4a")) == ("Under The Brid", None)


def test_a_preview_asks_nobody(ctx, monkeypatch):
    def boom(mbid):
        raise AssertionError("a preview reached the network")

    monkeypatch.setattr(tc, "recording_title", boom)
    ctx.dry_run = True
    tc.TitleCompleteStage().execute(ctx)
