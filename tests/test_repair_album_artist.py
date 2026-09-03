"""The albumartist repair pass, exercised against real M4A files.

Every test that claims a tag was written reads it back off disk in a fresh
handle. Asserting that `write_albumartist` was CALLED is the assertion
shape that let Forge report 12,279 writes while writing nothing, so it is
not used here.
"""

from __future__ import annotations

import importlib.util
import json
import shutil
import subprocess
from pathlib import Path

import pytest

_SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "repair_album_artist.py"
_spec = importlib.util.spec_from_file_location("repair_album_artist", _SCRIPT)
assert _spec and _spec.loader
raa = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(raa)

pytestmark = pytest.mark.skipif(
    not shutil.which("ffmpeg"), reason="ffmpeg needed to mint a real m4a"
)


def _make_m4a(path: Path, **tags: str) -> Path:
    """A real, tiny, tagged ALAC file -- not a fixture pretending to be one."""
    path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
         "-f", "lavfi", "-i", "anullsrc=r=44100:cl=stereo", "-t", "0.3",
         "-c:a", "alac", str(path)],
        check=True,
    )
    if tags:
        from mutagen.mp4 import MP4

        audio = MP4(str(path))
        if audio.tags is None:
            audio.add_tags()
        for key, val in (("artist", "\xa9ART"), ("albumartist", "aART"),
                         ("album", "\xa9alb"), ("genre", "\xa9gen")):
            if key in tags:
                audio[val] = [tags[key]]
        audio.save()
    return path


def _albumartist_on_disk(path: Path) -> str:
    from mutagen.mp4 import MP4

    v = (MP4(str(path)).tags or {}).get("aART")
    return str(v[0]) if v else ""


# ── reading ───────────────────────────────────────────────────────────────────


def test_reads_the_four_fields_it_decides_on(tmp_path):
    p = _make_m4a(tmp_path / "a.m4a", artist="Abba", albumartist="ABBA",
                  album="Gold", genre="Classic Pop")
    assert raa.read_fields(p) == {
        "artist": "Abba", "albumartist": "ABBA",
        "album": "Gold", "genre": "Classic Pop",
    }


def test_missing_tags_read_as_empty_not_none(tmp_path):
    p = _make_m4a(tmp_path / "a.m4a", artist="Solo")
    assert raa.read_fields(p) == {
        "artist": "Solo", "albumartist": "", "album": "", "genre": ""
    }


def test_an_unreadable_file_is_none_rather_than_a_guess(tmp_path):
    bad = tmp_path / "not-audio.m4a"
    bad.write_bytes(b"this is not an mp4")
    assert raa.read_fields(bad) is None


# ── writing ───────────────────────────────────────────────────────────────────


def test_the_write_actually_lands_on_disk(tmp_path):
    p = _make_m4a(tmp_path / "a.m4a", artist="50 Cent", albumartist="50 Cent, Nate Dogg")
    ok, detail = raa.write_albumartist(p, "50 Cent")
    assert ok, detail
    assert _albumartist_on_disk(p) == "50 Cent"


def test_the_write_leaves_the_audio_stream_alone(tmp_path):
    """audio_hash is of decoded PCM, so a tag write must not disturb it."""
    from musaeus.hasher import audio_hash

    p = _make_m4a(tmp_path / "a.m4a", artist="Abba", albumartist="ABBA")
    before = audio_hash(p)
    assert raa.write_albumartist(p, "Abba")[0]
    assert audio_hash(p) == before


def test_a_silent_no_op_write_is_caught_by_the_read_back(tmp_path, monkeypatch):
    """The guard the whole script hangs on, given the failure it exists for.

    Silent-no-op #2: mutagen's `save()` returned clean and wrote nothing, and
    12,279 FORGE_TAG events were recorded for tags no file carried. Here
    `save()` is made a no-op directly. Without the read-back this returns
    True and the journal records a change that never happened; with it, the
    fresh handle finds the old value still on disk and the write fails loudly.
    """
    from mutagen.mp4 import MP4

    p = _make_m4a(tmp_path / "a.m4a", artist="50 Cent", albumartist="50 Cent, Nate Dogg")
    monkeypatch.setattr(MP4, "save", lambda self, *a, **k: None)

    ok, detail = raa.write_albumartist(p, "50 Cent")
    assert not ok, "a write that did not land must not report success"
    assert "did not survive" in detail
    assert _albumartist_on_disk(p) == "50 Cent, Nate Dogg"


def test_apply_does_not_count_a_silent_no_op_as_written(tmp_path, monkeypatch):
    """And the failure must reach the tally, not just the return value."""
    from mutagen.mp4 import MP4

    lib = tmp_path / "lib"
    _make_m4a(lib / "1.m4a", artist="50 Cent", albumartist="50 Cent, Nate Dogg")
    planned, _ = raa.scan(lib, None)
    monkeypatch.setattr(MP4, "save", lambda self, *a, **k: None)

    result = raa.apply(planned, tmp_path / "j.jsonl")
    assert result["FAILED"] == 1
    assert not result["written and verified"]


def test_a_write_that_cannot_happen_reports_failure(tmp_path):
    bad = tmp_path / "not-audio.m4a"
    bad.write_bytes(b"this is not an mp4")
    ok, detail = raa.write_albumartist(bad, "Anything")
    assert not ok
    assert detail


# ── scan ──────────────────────────────────────────────────────────────────────


def test_scan_plans_only_what_the_rule_allows(tmp_path):
    lib = tmp_path / "lib"
    _make_m4a(lib / "1.m4a", artist="50 Cent", albumartist="50 Cent, Nate Dogg")
    _make_m4a(lib / "2.m4a", artist="Art Blakey",
              albumartist="Art Blakey & The Jazz Messengers", album="Moanin'")
    _make_m4a(lib / "3.m4a", artist="Tlc", albumartist="TLC")
    _make_m4a(lib / "4.m4a", artist="Antonio Vivaldi",
              albumartist="Anne-Sophie Mutter", genre="Classical")
    _make_m4a(lib / "5.m4a", artist="Beatles, The", albumartist="Beatles, The")

    planned, tally = raa.scan(lib, None)
    assert [Path(r["path"]).name for r in planned] == ["1.m4a"]
    assert tally["files read"] == 5
    assert tally["WOULD REWRITE"] == 1
    assert tally["kept: collaboration credit on a real album"] == 1
    assert tally["kept: differs only by case -- the ARTIST is the damaged field"] == 1
    assert tally["kept: classical composer vs performer"] == 1
    assert tally["kept: already agrees"] == 1


def test_limit_caps_the_plan_but_not_the_count(tmp_path):
    """The tally must report the true total even when the plan is truncated."""
    lib = tmp_path / "lib"
    for i in range(5):
        _make_m4a(lib / f"{i}.m4a", artist="50 Cent", albumartist="50 Cent, Nate Dogg")
    planned, tally = raa.scan(lib, 2)
    assert len(planned) == 2
    assert tally["WOULD REWRITE"] == 5


# ── apply ─────────────────────────────────────────────────────────────────────


def test_apply_writes_every_planned_change_and_journals_it(tmp_path):
    lib = tmp_path / "lib"
    p = _make_m4a(lib / "1.m4a", artist="50 Cent", albumartist="50 Cent, Nate Dogg")
    planned, _ = raa.scan(lib, None)
    journal = tmp_path / "j.jsonl"

    result = raa.apply(planned, journal)
    assert result["written and verified"] == 1
    assert not result["FAILED"]
    assert _albumartist_on_disk(p) == "50 Cent"

    rec = json.loads(journal.read_text().strip())
    assert rec["old_albumartist"] == "50 Cent, Nate Dogg"
    assert rec["new_albumartist"] == "50 Cent"


def test_the_journal_record_is_durable_before_the_file_is_touched(tmp_path, monkeypatch):
    """A crash mid-write must still leave the old value recoverable.

    Simulated by failing the write: the journal must already hold the record.
    """
    lib = tmp_path / "lib"
    _make_m4a(lib / "1.m4a", artist="50 Cent", albumartist="50 Cent, Nate Dogg")
    planned, _ = raa.scan(lib, None)
    journal = tmp_path / "j.jsonl"

    monkeypatch.setattr(raa, "write_albumartist", lambda p, v: (_ for _ in ()).throw(
        KeyboardInterrupt("power cut")))
    with pytest.raises(KeyboardInterrupt):
        raa.apply(planned, journal)

    rec = json.loads(journal.read_text().strip())
    assert rec["old_albumartist"] == "50 Cent, Nate Dogg"


def test_a_failed_write_is_counted_not_swallowed(tmp_path, monkeypatch):
    lib = tmp_path / "lib"
    _make_m4a(lib / "1.m4a", artist="50 Cent", albumartist="50 Cent, Nate Dogg")
    planned, _ = raa.scan(lib, None)
    monkeypatch.setattr(raa, "write_albumartist", lambda p, v: (False, "nope"))
    result = raa.apply(planned, tmp_path / "j.jsonl")
    assert result["FAILED"] == 1
    assert not result["written and verified"]


# ── undo ──────────────────────────────────────────────────────────────────────


def test_undo_restores_the_original_value_on_disk(tmp_path):
    lib = tmp_path / "lib"
    p = _make_m4a(lib / "1.m4a", artist="50 Cent", albumartist="50 Cent, Nate Dogg")
    planned, _ = raa.scan(lib, None)
    journal = tmp_path / "j.jsonl"
    raa.apply(planned, journal)
    assert _albumartist_on_disk(p) == "50 Cent"

    result = raa.undo(journal)
    assert result["restored"] == 1
    assert _albumartist_on_disk(p) == "50 Cent, Nate Dogg"


def test_undo_is_idempotent(tmp_path):
    lib = tmp_path / "lib"
    p = _make_m4a(lib / "1.m4a", artist="50 Cent", albumartist="50 Cent, Nate Dogg")
    planned, _ = raa.scan(lib, None)
    journal = tmp_path / "j.jsonl"
    raa.apply(planned, journal)
    raa.undo(journal)
    assert raa.undo(journal)["already back"] == 1
    assert _albumartist_on_disk(p) == "50 Cent, Nate Dogg"


def test_undo_refuses_a_file_something_else_changed(tmp_path):
    """Clobbering a later edit would be the same mistake in the other direction."""
    lib = tmp_path / "lib"
    p = _make_m4a(lib / "1.m4a", artist="50 Cent", albumartist="50 Cent, Nate Dogg")
    planned, _ = raa.scan(lib, None)
    journal = tmp_path / "j.jsonl"
    raa.apply(planned, journal)

    raa.write_albumartist(p, "Someone Else Entirely")
    result = raa.undo(journal)
    assert result["REFUSED: changed by something else"] == 1
    assert _albumartist_on_disk(p) == "Someone Else Entirely"


def test_undo_skips_a_file_that_is_gone(tmp_path):
    lib = tmp_path / "lib"
    p = _make_m4a(lib / "1.m4a", artist="50 Cent", albumartist="50 Cent, Nate Dogg")
    planned, _ = raa.scan(lib, None)
    journal = tmp_path / "j.jsonl"
    raa.apply(planned, journal)
    p.unlink()
    assert raa.undo(journal)["skipped: file is gone"] == 1


# ── end to end through the CLI ────────────────────────────────────────────────


def test_dry_run_writes_nothing(tmp_path):
    lib = tmp_path / "lib"
    p = _make_m4a(lib / "1.m4a", artist="50 Cent", albumartist="50 Cent, Nate Dogg")
    out = subprocess.run(
        [sys.executable, str(_SCRIPT), "--root", str(lib)],
        capture_output=True, text=True, check=True,
    )
    assert "DRY RUN" in out.stdout
    assert _albumartist_on_disk(p) == "50 Cent, Nate Dogg"


import sys  # noqa: E402  (used by the CLI test above)
