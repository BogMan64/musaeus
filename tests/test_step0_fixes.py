"""Step 0: the small fixes queued on 2026-09-25/26, one test per fix.

damage     The Who's "Cut My Hair" master played 89 of 224 seconds and passed
           every check: Sentinel's audio hash decodes the whole file but only
           looked at ffmpeg's exit code, which was 0 despite 6,194 broken
           frames; CorruptStage fully decodes only 200 new files a run.
named      A file Sentinel could not decode was counted but never named, so
           the report showed its duplicate notes as the failure.
keeper     Two equally good copies: the new arrival won on a few bytes of
           tags, so 79 library copies were swapped for identical ones.
rv         "RV. 532" (with a dot) was not read as Vivaldi.
va         "Various Artists - Be My Baby" went unnamed; AcoustID can name
           such a row when its answer is clear.
adopt      A Ctrl-C inside Finalize left 9 files moved but their rows
           unsaved; the next Finalize called them "missing on disk".
art        A broken cover picture was reported as damaged audio: the
           "Last message repeated" line after an mjpeg error was kept.
dot        Sanitize stripped the final dot from "Run-D.M.C."; the canon put
           it back; every Act 1 did both.
"""

from __future__ import annotations

import json
import random
import subprocess
from pathlib import Path

import pytest

import musaeus.stages.acousticid as acoustid_mod
from musaeus.config import MusicConfig
from musaeus.context import RunContext
from musaeus.db import open_db, upsert_archive
from musaeus.hasher import audio_hash
from musaeus.stages.classical_composer import composer_for
from musaeus.stages.corrupt import audio_relevant_stderr
from musaeus.stages.dupe_resolver import DupeResolverStage
from musaeus.stages.finalize import FinalizeStage
from musaeus.stages.organize import library_relpath
from musaeus.stages.sanitize import SanitizeStage
from musaeus.stages.sentinel import SentinelStage
from musaeus.stages.various_artists_fix import VariousArtistsFixStage


def _ctx(tmp_path: Path, key: str | None = None) -> RunContext:
    cfg = MusicConfig(
        vault_root=tmp_path,
        inbox=tmp_path / "INBOX",
        staging=tmp_path / "STAGING",
        quarantine=tmp_path / "QUARANTINE",
        runs_root=tmp_path / "RUNS",
        meta_dir=tmp_path / "MetaData",
        alac_library=tmp_path / "Libraries" / "ALAC_Library",
        alac_archive=tmp_path / "Libraries" / "ALAC-Archival",
        db_path=tmp_path / "musaeus.db",
        acousticid_api_key=key,
    )
    cfg.ensure_dirs()
    return RunContext.new(cfg, open_db(cfg.db_path), dry_run=False)


def _alac(path: Path, seconds: int = 8, damaged: bool = False) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [
            "ffmpeg",
            "-v",
            "error",
            "-y",
            "-f",
            "lavfi",
            "-i",
            f"sine=frequency=440:duration={seconds}:sample_rate=44100",
            "-ac",
            "2",
            "-c:a",
            "alac",
            str(path),
        ],
        check=True,
    )
    if damaged:
        # Right-sized damage inside the stream: the shape of The Who master.
        b = bytearray(path.read_bytes())
        rnd = random.Random(7)
        for off in range(int(len(b) * 0.45), int(len(b) * 0.60), 997):
            b[off : off + 64] = bytes(rnd.randrange(256) for _ in range(64))
        path.write_bytes(bytes(b))
    return path


# ── damage and named ─────────────────────────────────────────────────────────


def test_damage_the_fixture_is_the_incidents_shape(tmp_path):
    bad = _alac(tmp_path / "bad.m4a", damaged=True)
    r = subprocess.run(
        ["ffmpeg", "-v", "error", "-i", str(bad), "-vn", "-map", "0:a:0", "-f", "null", "-"],
        capture_output=True,
        text=True,
    )
    assert r.returncode == 0 and r.stderr.strip(), "must decode with errors yet exit 0"


def test_damage_sentinel_does_not_pass_audio_that_decodes_with_errors(tmp_path):
    ctx = _ctx(tmp_path)
    bad = _alac(ctx.inbox / "The Who - Cut My Hair.m4a", damaged=True)
    upsert_archive(ctx.conn, {"file_path": str(bad), "status": "PENDING"})
    ctx.conn.commit()
    result = SentinelStage().run(ctx)
    (ah,) = ctx.conn.execute("SELECT audio_hash FROM archive").fetchone()
    assert not ah, "a damaged file was given an identity and sent on to the library"
    assert any("Cut My Hair" in e for e in result.errors), result.errors


def test_named_an_undecodable_file_is_named_in_the_report(tmp_path):
    ctx = _ctx(tmp_path)
    junk = ctx.inbox / "Diana Ross - The Boss.m4a"
    junk.write_bytes(b"not audio at all" * 100)
    upsert_archive(ctx.conn, {"file_path": str(junk), "status": "PENDING"})
    ctx.conn.commit()
    result = SentinelStage().run(ctx)
    assert any("The Boss" in e for e in result.errors), result.errors


# ── keeper ───────────────────────────────────────────────────────────────────


def test_keeper_equally_good_copies_keep_the_one_already_filed(tmp_path):
    ctx = _ctx(tmp_path)
    master = ctx.config.alac_archive / "Rock" / "Dion" / "A" / "Dion - Song.m4a"
    arrival = ctx.inbox / "Dion - Song.m4a"
    for path, size, filed in ((master, 1000, True), (arrival, 1010, False)):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"x" * size)
        upsert_archive(
            ctx.conn,
            {
                "file_path": str(path),
                "status": "CATALOGUED",
                "artist": "Dion",
                "title": "Song",
                "album": "A",
                "audio_hash": "same",
                "codec": "alac",
                "bitrate": 900_000,
                "size_bytes": size,
            },
        )
        if filed:
            ctx.conn.execute(
                "UPDATE archive SET finalized_at = datetime('now') WHERE file_path = ?",
                (str(path),),
            )
    ctx.conn.commit()
    DupeResolverStage().run(ctx)
    assert master.is_file(), "the filed master was swapped for an identical new copy"
    assert not arrival.exists()


# ── rv ───────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("marker", ["RV 532", "RV. 532", "RV.532"])
def test_rv_a_dotted_catalogue_number_is_read(marker):
    comp, _ = composer_for(
        "Renaissance Chamber Orchestra", f"Concerto For 2 Mandolins In G Major, {marker}: II.", {}
    )
    assert comp == "Antonio Vivaldi"


# ── va ───────────────────────────────────────────────────────────────────────

RONETTES = {"id": "r1", "title": "Be My Baby", "artists": [{"name": "The Ronettes"}]}
POSEY = {"id": "r2", "title": "Be My Baby", "artists": [{"name": "Sandy Posey"}]}


VA_CASES = {
    "clear": (RONETTES,),
    "disagreeing": (RONETTES, POSEY),
}
VA_WANT = {"clear": "The Ronettes", "disagreeing": "Various Artists"}


@pytest.mark.parametrize("case", sorted(VA_CASES))
def test_va_a_various_artists_track_is_named_by_its_sound_only_when_clear(
    tmp_path, monkeypatch, case
):
    recordings, want = list(VA_CASES[case]), VA_WANT[case]
    monkeypatch.setattr(acoustid_mod, "_fpcalc", lambda p: (160.0, "FP"))
    monkeypatch.setattr(
        acoustid_mod,
        "_acousticid_query",
        lambda fp, d, key: [{"score": 0.95, "recordings": recordings}],
    )
    ctx = _ctx(tmp_path, key="test-key")
    ctx.set("various_artists_no_mb", True)
    p = ctx.inbox / "Various Artists - Be My Baby.m4a"
    p.write_bytes(b"x")
    upsert_archive(
        ctx.conn,
        {
            "file_path": str(p),
            "status": "CATALOGUED",
            "artist": "Various Artists",
            "title": "Be My Baby",
            "audio_hash": "h",
        },
    )
    ctx.conn.commit()
    VariousArtistsFixStage().run(ctx)
    (artist,) = ctx.conn.execute("SELECT artist FROM archive").fetchone()
    assert artist == want


# ── adopt ────────────────────────────────────────────────────────────────────


def test_adopt_a_file_an_interrupted_finalize_already_moved(tmp_path):
    ctx = _ctx(tmp_path)
    src = ctx.inbox / "Sep5" / "George Thorogood - Bad To The Bone.m4a"
    rel = library_relpath("George Thorogood", None, "Rock", "Album", "Bad To The Bone", ".m4a")
    dst = _alac(ctx.config.alac_archive / rel, seconds=2)
    upsert_archive(
        ctx.conn,
        {
            "file_path": str(src),
            "status": "CATALOGUED",
            "artist": "George Thorogood",
            "title": "Bad To The Bone",
            "album": "Album",
            "genre": "Rock",
            "audio_hash": audio_hash(dst),
        },
    )
    ctx.conn.execute(
        "UPDATE archive SET canonicalized_at = datetime('now'), canon_action = 'PASSTHROUGH'"
    )
    ctx.conn.commit()
    journal = ctx.config.runs_root / "recovery" / "finalize_run_OLD" / "journal.jsonl"
    journal.parent.mkdir(parents=True)
    journal.write_text(
        json.dumps(
            {
                "operation_kind": "move",
                "status": "planned",
                "operation_id": "op1",
                "detail": {
                    "relative_path": str(src.relative_to(ctx.config.vault_root)),
                    "moved_to": str(dst.relative_to(ctx.config.vault_root)),
                    "source_released": False,
                },
            }
        )
        + "\n"
    )
    result = FinalizeStage().execute(ctx)
    fp, fin = ctx.conn.execute("SELECT file_path, finalized_at FROM archive").fetchone()
    assert (fp, bool(fin)) == (str(dst), True), result.errors
    assert not any("missing on disk" in e for e in result.errors)


def test_adopt_never_takes_a_different_recording_at_that_path(tmp_path):
    ctx = _ctx(tmp_path)
    src = ctx.inbox / "Sep5" / "George Thorogood - Bad To The Bone.m4a"
    rel = library_relpath("George Thorogood", None, "Rock", "Album", "Bad To The Bone", ".m4a")
    dst = _alac(ctx.config.alac_archive / rel, seconds=2)
    upsert_archive(
        ctx.conn,
        {
            "file_path": str(src),
            "status": "CATALOGUED",
            "artist": "George Thorogood",
            "title": "Bad To The Bone",
            "album": "Album",
            "genre": "Rock",
            "audio_hash": "some-other-recording",
        },
    )
    ctx.conn.execute(
        "UPDATE archive SET canonicalized_at = datetime('now'), canon_action = 'PASSTHROUGH'"
    )
    ctx.conn.commit()
    journal = ctx.config.runs_root / "recovery" / "finalize_run_OLD" / "journal.jsonl"
    journal.parent.mkdir(parents=True)
    journal.write_text(
        json.dumps(
            {
                "operation_kind": "move",
                "status": "planned",
                "operation_id": "op1",
                "detail": {
                    "relative_path": str(src.relative_to(ctx.config.vault_root)),
                    "moved_to": str(dst.relative_to(ctx.config.vault_root)),
                },
            }
        )
        + "\n"
    )
    FinalizeStage().execute(ctx)
    (fp,) = ctx.conn.execute("SELECT file_path FROM archive").fetchone()
    assert fp == str(src), "a file with different audio was adopted"


# ── art ──────────────────────────────────────────────────────────────────────


def test_art_a_repeat_line_after_a_picture_error_is_not_audio_damage():
    stderr = (
        "[mjpeg @ 0x563c6c9d5b80] unable to decode APP fields: Invalid data found\n"
        "    Last message repeated 1 times\n"
    )
    assert audio_relevant_stderr(stderr, 0) == ""


def test_art_a_repeat_line_after_an_audio_error_is_still_kept():
    stderr = (
        "[alac @ 0x1] invalid samples per frame: 310155645\n    Last message repeated 4 times\n"
    )
    assert "repeated" in audio_relevant_stderr(stderr, 0)


# ── dot ──────────────────────────────────────────────────────────────────────


def test_dot_sanitize_keeps_a_names_final_dot(tmp_path):
    ctx = _ctx(tmp_path)
    p = ctx.inbox / "a.m4a"
    p.write_bytes(b"x")
    upsert_archive(
        ctx.conn,
        {
            "file_path": str(p),
            "status": "CATALOGUED",
            "artist": "Run-D.M.C.",
            "title": "Walk This Way",
        },
    )
    ctx.conn.commit()
    SanitizeStage().run(ctx)
    (artist,) = ctx.conn.execute("SELECT artist FROM archive").fetchone()
    assert artist == "Run-D.M.C."
    # The PATH still drops it -- that is where Windows cares.
    assert library_relpath("Run-D.M.C.", None, "Hip Hop", "A", "T", ".m4a").parts[1] == "Run-D.M.C"
