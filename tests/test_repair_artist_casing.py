"""Artist-casing repair: the three filters, and the writes they gate.

Real M4A files, real cache rows, every write read back off disk.
"""

from __future__ import annotations

import importlib.util
import json
import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

_SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "repair_artist_casing.py"
_spec = importlib.util.spec_from_file_location("repair_artist_casing", _SCRIPT)
assert _spec and _spec.loader
rac = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(rac)

pytestmark = pytest.mark.skipif(
    not shutil.which("ffmpeg"), reason="ffmpeg needed to mint a real m4a"
)


def _make_m4a(path: Path, artist: str = "", albumartist: str = "") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
         "-f", "lavfi", "-i", "anullsrc=r=44100:cl=stereo", "-t", "0.3",
         "-c:a", "alac", str(path)],
        check=True,
    )
    from mutagen.mp4 import MP4

    a = MP4(str(path))
    if a.tags is None:
        a.add_tags()
    if artist:
        a["\xa9ART"] = [artist]
    if albumartist:
        a["aART"] = [albumartist]
    a.save()
    return path


def _tags(path: Path) -> tuple[str, str]:
    from mutagen.mp4 import MP4

    t = MP4(str(path)).tags or {}
    g = lambda k: str((t.get(k) or [""])[0])  # noqa: E731
    return g("\xa9ART"), g("aART")


def _cache(path: Path, rows: list[tuple[str, str, int]]) -> Path:
    con = sqlite3.connect(path)
    con.execute(
        "CREATE TABLE mb_artist (artist_key TEXT PRIMARY KEY, mbid TEXT, "
        "mb_name TEXT, found INTEGER NOT NULL, looked_up TEXT)"
    )
    con.executemany(
        "INSERT INTO mb_artist (artist_key, mbid, mb_name, found) VALUES (?,?,?,?)",
        [(k, "x", n, f) for k, n, f in rows],
    )
    con.commit()
    con.close()
    return path


# ── filter 1: same name only ──────────────────────────────────────────────────


def test_adopts_musicbrainz_casing():
    assert rac.canonical_spelling("Tlc", "TLC") == "TLC"
    assert rac.canonical_spelling("Paul Mccartney", "Paul McCartney") == "Paul McCartney"
    assert rac.canonical_spelling("N.w.a", "N.W.A") == "N.W.A"
    assert rac.canonical_spelling("K.d. Lang", "k.d. lang") == "k.d. lang"


def test_a_different_artist_is_never_adopted():
    """Folding must gate this -- otherwise a bad cache row renames the library."""
    assert rac.canonical_spelling("Red", "Red Hot Chili Peppers") is None
    assert rac.canonical_spelling("Beatles, The", "The Rolling Stones") is None


def test_nothing_to_do_cases():
    assert rac.canonical_spelling("TLC", "TLC") is None
    assert rac.canonical_spelling("", "TLC") is None
    assert rac.canonical_spelling("Tlc", None) is None
    assert rac.canonical_spelling("Tlc", "") is None


# ── filter 2: our punctuation, their casing ───────────────────────────────────


def test_a_typography_only_difference_is_refused():
    """sanitize_path_component flattens these, so adopting them drifts tag vs path."""
    assert rac.canonical_spelling("Guns N' Roses", "Guns N’ Roses") is None
    assert rac.canonical_spelling("Olivia Newton-John", "Olivia Newton‐John") is None
    assert rac.canonical_spelling(
        "Bachman-Turner Overdrive", "Bachman–Turner Overdrive"
    ) is None


def test_casing_still_wins_through_typography():
    """'Vanessa‐mae' -> 'Vanessa-Mae': the case changed AND the dash flattens."""
    assert rac.canonical_spelling("Vanessa‐mae", "Vanessa‐Mae") == "Vanessa-Mae"


def test_ascii_punctuation_maps_the_whole_set():
    assert rac.ascii_punctuation("a’b") == "a'b"
    assert rac.ascii_punctuation("a‐b–c—d") == "a-b-c-d"
    assert rac.ascii_punctuation("“x”") == '"x"'


# ── filter 3: nothing a filesystem forbids ────────────────────────────────────


def test_a_name_with_a_forbidden_character_is_refused():
    """MusicBrainz really does publish these, and a path cannot hold them."""
    assert rac.canonical_spelling("Nsync", "*NSYNC") is None
    assert rac.canonical_spelling(
        "Eddie -Cleanhead- Vinson", 'Eddie "Cleanhead" Vinson'
    ) is None


# ── scan ──────────────────────────────────────────────────────────────────────


def test_scan_plans_a_repair_and_carries_albumartist(tmp_path):
    lib = tmp_path / "lib"
    _make_m4a(lib / "1.m4a", artist="Tlc", albumartist="Tlc")
    names = rac.load_mb_names(_cache(tmp_path / "c.db", [("tlc", "TLC", 1)]))
    planned, tally = rac.scan(lib, names, None)
    assert len(planned) == 1
    assert planned[0]["new_artist"] == "TLC"
    assert planned[0]["new_albumartist"] == "TLC"
    assert tally["WOULD REPAIR"] == 1


def test_a_differing_albumartist_is_left_out_of_the_plan(tmp_path):
    """Only the agreement established on 08-29 is maintained, nothing more."""
    lib = tmp_path / "lib"
    _make_m4a(lib / "1.m4a", artist="Tlc", albumartist="TLC")
    names = rac.load_mb_names(_cache(tmp_path / "c.db", [("tlc", "TLC", 1)]))
    planned, _ = rac.scan(lib, names, None)
    assert "new_albumartist" not in planned[0]


def test_an_uncached_artist_is_left_alone(tmp_path):
    lib = tmp_path / "lib"
    _make_m4a(lib / "1.m4a", artist="Nobody")
    planned, tally = rac.scan(lib, {}, None)
    assert planned == []
    assert tally["no cached MusicBrainz name"] == 1


def test_limit_caps_the_plan_but_not_the_count(tmp_path):
    lib = tmp_path / "lib"
    for i in range(4):
        _make_m4a(lib / f"{i}.m4a", artist="Tlc")
    names = rac.load_mb_names(_cache(tmp_path / "c.db", [("tlc", "TLC", 1)]))
    planned, tally = rac.scan(lib, names, 2)
    assert len(planned) == 2
    assert tally["WOULD REPAIR"] == 4


# ── apply / undo ──────────────────────────────────────────────────────────────


def test_apply_writes_both_fields_to_disk(tmp_path):
    lib = tmp_path / "lib"
    p = _make_m4a(lib / "1.m4a", artist="Tlc", albumartist="Tlc")
    names = rac.load_mb_names(_cache(tmp_path / "c.db", [("tlc", "TLC", 1)]))
    planned, _ = rac.scan(lib, names, None)
    assert rac.apply(planned, tmp_path / "j.jsonl")["written and verified"] == 1
    assert _tags(p) == ("TLC", "TLC")


def test_a_silent_no_op_write_is_caught(tmp_path, monkeypatch):
    from mutagen.mp4 import MP4

    lib = tmp_path / "lib"
    _make_m4a(lib / "1.m4a", artist="Tlc")
    names = rac.load_mb_names(_cache(tmp_path / "c.db", [("tlc", "TLC", 1)]))
    planned, _ = rac.scan(lib, names, None)
    monkeypatch.setattr(MP4, "save", lambda self, *a, **k: None)
    result = rac.apply(planned, tmp_path / "j.jsonl")
    assert result["FAILED"] == 1
    assert not result["written and verified"]


def test_the_journal_is_durable_before_the_file_is_touched(tmp_path, monkeypatch):
    lib = tmp_path / "lib"
    _make_m4a(lib / "1.m4a", artist="Tlc")
    names = rac.load_mb_names(_cache(tmp_path / "c.db", [("tlc", "TLC", 1)]))
    planned, _ = rac.scan(lib, names, None)
    journal = tmp_path / "j.jsonl"
    monkeypatch.setattr(rac, "write_fields", lambda p, v: (_ for _ in ()).throw(
        KeyboardInterrupt("power cut")))
    with pytest.raises(KeyboardInterrupt):
        rac.apply(planned, journal)
    assert json.loads(journal.read_text().strip())["old_artist"] == "Tlc"


def test_undo_restores_both_fields(tmp_path):
    lib = tmp_path / "lib"
    p = _make_m4a(lib / "1.m4a", artist="Tlc", albumartist="Tlc")
    names = rac.load_mb_names(_cache(tmp_path / "c.db", [("tlc", "TLC", 1)]))
    planned, _ = rac.scan(lib, names, None)
    journal = tmp_path / "j.jsonl"
    rac.apply(planned, journal)
    assert rac.undo(journal)["restored"] == 1
    assert _tags(p) == ("Tlc", "Tlc")


def test_undo_is_idempotent(tmp_path):
    lib = tmp_path / "lib"
    _make_m4a(lib / "1.m4a", artist="Tlc")
    names = rac.load_mb_names(_cache(tmp_path / "c.db", [("tlc", "TLC", 1)]))
    planned, _ = rac.scan(lib, names, None)
    journal = tmp_path / "j.jsonl"
    rac.apply(planned, journal)
    rac.undo(journal)
    assert rac.undo(journal)["already back"] == 1


def test_undo_refuses_a_file_something_else_changed(tmp_path):
    lib = tmp_path / "lib"
    p = _make_m4a(lib / "1.m4a", artist="Tlc")
    names = rac.load_mb_names(_cache(tmp_path / "c.db", [("tlc", "TLC", 1)]))
    planned, _ = rac.scan(lib, names, None)
    journal = tmp_path / "j.jsonl"
    rac.apply(planned, journal)
    rac.write_fields(p, {"artist": "Something Else"})
    assert rac.undo(journal)["REFUSED: changed by something else"] == 1
    assert _tags(p)[0] == "Something Else"


def test_dry_run_writes_nothing(tmp_path):
    lib = tmp_path / "lib"
    p = _make_m4a(lib / "1.m4a", artist="Tlc")
    c = _cache(tmp_path / "c.db", [("tlc", "TLC", 1)])
    out = subprocess.run(
        [sys.executable, str(_SCRIPT), "--root", str(lib), "--cache", str(c)],
        capture_output=True, text=True,
        env={**__import__("os").environ, "MUSAEUS_VAULT_ROOT": str(tmp_path)},
    )
    assert "DRY RUN" in out.stdout, out.stdout + out.stderr
    assert _tags(p)[0] == "Tlc"
