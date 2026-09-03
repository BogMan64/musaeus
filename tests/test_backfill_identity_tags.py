"""The identity backfill, exercised against real M4A files and a real cache.

Every claim that a tag was written is checked by reading it back off disk.
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

_SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "backfill_identity_tags.py"
_spec = importlib.util.spec_from_file_location("backfill_identity_tags", _SCRIPT)
assert _spec and _spec.loader
bit = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(bit)

from musaeus.identity_tags import read_identity  # noqa: E402

pytestmark = pytest.mark.skipif(
    not shutil.which("ffmpeg"), reason="ffmpeg needed to mint a real m4a"
)

MBID_A = "0fe5df51-ba65-4784-ba85-7d024a107a8a"
MBID_B = "f37c537b-3557-4031-bfd6-ab63ced32854"


def _make_m4a(path: Path, artist: str = "") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
         "-f", "lavfi", "-i", "anullsrc=r=44100:cl=stereo", "-t", "0.3",
         "-c:a", "alac", str(path)],
        check=True,
    )
    if artist:
        from mutagen.mp4 import MP4

        a = MP4(str(path))
        if a.tags is None:
            a.add_tags()
        a["\xa9ART"] = [artist]
        a.save()
    return path


def _make_cache(path: Path, rows: list[tuple[str, str, int]]) -> Path:
    con = sqlite3.connect(path)
    con.execute(
        "CREATE TABLE mb_artist (artist_key TEXT PRIMARY KEY, mbid TEXT, "
        "mb_name TEXT, found INTEGER NOT NULL, looked_up TEXT)"
    )
    con.executemany(
        "INSERT INTO mb_artist (artist_key, mbid, mb_name, found) VALUES (?,?,?,?)",
        [(k, m, k, f) for k, m, f in rows],
    )
    con.commit()
    con.close()
    return path


# ── cache loading ─────────────────────────────────────────────────────────────


def test_cache_loads_hits_and_settled_misses(tmp_path):
    c = _make_cache(tmp_path / "c.db", [("24kgoldn", MBID_A, 1), ("nobody", "", 0)])
    assert bit.load_cache(c) == {"24kgoldn": (MBID_A, 1), "nobody": ("", 0)}


def test_a_missing_cache_is_empty_not_an_error(tmp_path):
    assert bit.load_cache(tmp_path / "nope.db") == {}


# ── planning ──────────────────────────────────────────────────────────────────


def test_a_cache_hit_is_planned(tmp_path):
    lib = tmp_path / "lib"
    _make_m4a(lib / "1.m4a", "24kGoldn")
    cache = bit.load_cache(_make_cache(tmp_path / "c.db", [("24kgoldn", MBID_A, 1)]))
    planned, unresolved, tally = bit.scan(lib, cache, None)
    assert len(planned) == 1
    assert planned[0]["mb_artist_id"] == MBID_A
    assert tally["from cache"] == 1
    assert not unresolved


def test_a_settled_miss_is_an_answer_and_is_never_planned(tmp_path):
    """found=0 means MusicBrainz said no. Re-querying it is finding #13."""
    lib = tmp_path / "lib"
    _make_m4a(lib / "1.m4a", "Nobody At All")
    cache = bit.load_cache(_make_cache(tmp_path / "c.db", [("nobody at all", "", 0)]))
    planned, unresolved, tally = bit.scan(lib, cache, None)
    assert planned == []
    assert unresolved == {}, "a settled miss must not be queued for the network"
    assert tally["cached miss -- AcoustID's job"] == 1


def test_an_uncached_artist_is_queued_for_the_network(tmp_path):
    lib = tmp_path / "lib"
    p = _make_m4a(lib / "1.m4a", "Never Heard Of")
    cache = bit.load_cache(_make_cache(tmp_path / "c.db", []))
    planned, unresolved, tally = bit.scan(lib, cache, None)
    assert planned == []
    assert unresolved == {"Never Heard Of": [p]}
    assert tally["needs a network lookup"] == 1


def test_an_already_tagged_file_is_skipped(tmp_path):
    from musaeus.identity_tags import write_identity

    lib = tmp_path / "lib"
    p = _make_m4a(lib / "1.m4a", "24kGoldn")
    assert write_identity(p, {"mb_artist_id": MBID_A})[0]
    cache = bit.load_cache(_make_cache(tmp_path / "c.db", [("24kgoldn", MBID_A, 1)]))
    planned, _, tally = bit.scan(lib, cache, None)
    assert planned == []
    assert tally["already tagged on disk"] == 1


def test_a_file_with_no_artist_is_counted_not_guessed_at(tmp_path):
    lib = tmp_path / "lib"
    _make_m4a(lib / "1.m4a")
    planned, unresolved, tally = bit.scan(lib, {}, None)
    assert planned == [] and unresolved == {}
    assert tally["no artist to look up"] == 1


def test_limit_caps_the_plan_but_not_the_count(tmp_path):
    lib = tmp_path / "lib"
    for i in range(4):
        _make_m4a(lib / f"{i}.m4a", "10cc")
    cache = bit.load_cache(_make_cache(tmp_path / "c.db", [("10cc", MBID_B, 1)]))
    planned, _, tally = bit.scan(lib, cache, 2)
    assert len(planned) == 2
    assert tally["from cache"] == 4


# ── online resolution: the three states ───────────────────────────────────────


def test_no_answer_is_not_recorded_as_missing(tmp_path, monkeypatch):
    """LookupUnavailable must leave the artist for a later run (finding #15)."""
    from musaeus.stages import mb_enrich

    p = _make_m4a(tmp_path / "1.m4a", "Someone")

    def boom(name):
        raise mb_enrich.LookupUnavailable("connection reset")

    monkeypatch.setattr(mb_enrich, "_search_artist", boom)
    planned, tally = bit.resolve_online({"Someone": [p]})
    assert planned == []
    assert tally["NO ANSWER -- will retry on a later run"] == 1
    assert not tally["MB answered: no such artist"]


def test_answered_no_is_distinct_from_no_answer(tmp_path, monkeypatch):
    from musaeus.stages import mb_enrich

    p = _make_m4a(tmp_path / "1.m4a", "Someone")
    monkeypatch.setattr(mb_enrich, "_search_artist", lambda name: None)
    planned, tally = bit.resolve_online({"Someone": [p]})
    assert planned == []
    assert tally["MB answered: no such artist"] == 1
    assert not tally["NO ANSWER -- will retry on a later run"]


def test_a_match_plans_every_file_by_that_artist(tmp_path, monkeypatch):
    from musaeus.stages import mb_enrich

    ps = [_make_m4a(tmp_path / f"{i}.m4a", "Someone") for i in range(3)]
    monkeypatch.setattr(mb_enrich, "_search_artist", lambda name: (MBID_A, "Someone"))
    planned, tally = bit.resolve_online({"Someone": ps})
    assert len(planned) == 3
    assert tally["found online"] == 3
    assert all(r["mb_artist_id"] == MBID_A for r in planned)


# ── apply / undo ──────────────────────────────────────────────────────────────


def test_apply_writes_the_id_and_it_is_on_disk(tmp_path):
    lib = tmp_path / "lib"
    p = _make_m4a(lib / "1.m4a", "24kGoldn")
    cache = bit.load_cache(_make_cache(tmp_path / "c.db", [("24kgoldn", MBID_A, 1)]))
    planned, _, _ = bit.scan(lib, cache, None)
    result = bit.apply(planned, tmp_path / "j.jsonl")
    assert result["written and verified"] == 1
    assert read_identity(p)["mb_artist_id"] == MBID_A


def test_a_silent_no_op_write_is_caught(tmp_path, monkeypatch):
    """write_identity read-backs; prove it, don't assume it."""
    from mutagen.mp4 import MP4

    lib = tmp_path / "lib"
    _make_m4a(lib / "1.m4a", "24kGoldn")
    cache = bit.load_cache(_make_cache(tmp_path / "c.db", [("24kgoldn", MBID_A, 1)]))
    planned, _, _ = bit.scan(lib, cache, None)
    monkeypatch.setattr(MP4, "save", lambda self, *a, **k: None)
    result = bit.apply(planned, tmp_path / "j.jsonl")
    assert result["FAILED"] == 1
    assert not result["written and verified"]


def test_the_journal_is_durable_before_the_file_is_touched(tmp_path, monkeypatch):
    lib = tmp_path / "lib"
    _make_m4a(lib / "1.m4a", "24kGoldn")
    cache = bit.load_cache(_make_cache(tmp_path / "c.db", [("24kgoldn", MBID_A, 1)]))
    planned, _, _ = bit.scan(lib, cache, None)
    journal = tmp_path / "j.jsonl"
    monkeypatch.setattr(bit, "write_identity", lambda p, v: (_ for _ in ()).throw(
        KeyboardInterrupt("power cut")))
    with pytest.raises(KeyboardInterrupt):
        bit.apply(planned, journal)
    assert json.loads(journal.read_text().strip())["mb_artist_id"] == MBID_A


def test_undo_removes_the_tag(tmp_path):
    lib = tmp_path / "lib"
    p = _make_m4a(lib / "1.m4a", "24kGoldn")
    cache = bit.load_cache(_make_cache(tmp_path / "c.db", [("24kgoldn", MBID_A, 1)]))
    planned, _, _ = bit.scan(lib, cache, None)
    journal = tmp_path / "j.jsonl"
    bit.apply(planned, journal)
    assert bit.undo(journal)["removed"] == 1
    assert not read_identity(p).get("mb_artist_id")


def test_undo_refuses_a_different_id(tmp_path):
    from musaeus.identity_tags import write_identity

    lib = tmp_path / "lib"
    p = _make_m4a(lib / "1.m4a", "24kGoldn")
    cache = bit.load_cache(_make_cache(tmp_path / "c.db", [("24kgoldn", MBID_A, 1)]))
    planned, _, _ = bit.scan(lib, cache, None)
    journal = tmp_path / "j.jsonl"
    bit.apply(planned, journal)
    write_identity(p, {"mb_artist_id": MBID_B})
    assert bit.undo(journal)["REFUSED: a different id is there now"] == 1
    assert read_identity(p)["mb_artist_id"] == MBID_B


def test_undo_is_idempotent(tmp_path):
    lib = tmp_path / "lib"
    _make_m4a(lib / "1.m4a", "24kGoldn")
    cache = bit.load_cache(_make_cache(tmp_path / "c.db", [("24kgoldn", MBID_A, 1)]))
    planned, _, _ = bit.scan(lib, cache, None)
    journal = tmp_path / "j.jsonl"
    bit.apply(planned, journal)
    bit.undo(journal)
    assert bit.undo(journal)["already back"] == 1


def test_dry_run_writes_nothing(tmp_path):
    lib = tmp_path / "lib"
    p = _make_m4a(lib / "1.m4a", "24kGoldn")
    c = _make_cache(tmp_path / "c.db", [("24kgoldn", MBID_A, 1)])
    out = subprocess.run(
        [sys.executable, str(_SCRIPT), "--root", str(lib), "--cache", str(c)],
        capture_output=True, text=True,
        env={**__import__("os").environ,
             "MUSAEUS_VAULT_ROOT": str(tmp_path)},
    )
    assert "DRY RUN" in out.stdout, out.stdout + out.stderr
    assert not read_identity(p).get("mb_artist_id")
