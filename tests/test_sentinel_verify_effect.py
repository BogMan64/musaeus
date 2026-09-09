"""Sentinel must confirm the hashes it computed describe the files they name.

This is the stage whose silent failure is worth the most. Every dedup
decision downstream -- EXACT here, NEAR in neardupe, the connected
components dupe_resolver archives files on -- is a statement about
audio_hash, and all of them are satisfied by a hash that is merely
PRESENT. A wrong hash is therefore never caught later; it is acted upon.

The sharpest case is the empty-stream digest. audio_hash() returns
h.hexdigest() whenever ffmpeg exits 0, including when it decoded nothing,
so a file yielding no PCM gets a well-formed 64-char hash of no audio --
and every such file gets the SAME one. Sentinel's definition of an EXACT
duplicate is identical audio_hash, so a batch of these is filed as one
giant duplicate group and dupe_resolver keeps one and archives the rest.
Nothing in the run reports an error: the hash was computed, the row was
updated, the count went up.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from unittest.mock import patch

import pytest

from musaeus.stages.sentinel import _EMPTY_STREAM_SHA256, SentinelStage

_GOOD = "a" * 64
_OTHER = "b" * 64


class _Ctx:
    def __init__(self, conn, run_id="run_test"):
        self.conn = conn
        self.run_id = run_id


class _Result:
    def __init__(self, files_changed=1):
        self.files_changed = files_changed


@pytest.fixture
def conn() -> sqlite3.Connection:
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    c.execute(
        "CREATE TABLE archive (file_path TEXT PRIMARY KEY, audio_hash TEXT, "
        "full_hash TEXT, status TEXT)"
    )
    c.execute(
        "CREATE TABLE events (id INTEGER PRIMARY KEY, run_id TEXT, stage TEXT, "
        "event_type TEXT, file_path TEXT)"
    )
    return c


def _hashed(conn, path, audio_hash=_GOOD, status="HASHED", full_hash="f" * 64, run="run_test"):
    """Record a file as this run hashed it: archive row + HASH_COMPUTED event."""
    conn.execute(
        "INSERT INTO archive (file_path, audio_hash, full_hash, status) VALUES (?,?,?,?)",
        (str(path), audio_hash, full_hash, status),
    )
    conn.execute(
        "INSERT INTO events (run_id, stage, event_type, file_path) VALUES (?,?,?,?)",
        (run, "sentinel", "HASH_COMPUTED", str(path)),
    )
    conn.commit()


def _track(tmp_path: Path, name="track.flac") -> Path:
    p = tmp_path / name
    p.write_bytes(b"audio")
    return p


# ── The happy path ────────────────────────────────────────────────────────────


def test_a_hash_that_re_derives_verifies(conn, tmp_path: Path) -> None:
    p = _track(tmp_path)
    _hashed(conn, p)
    with patch("musaeus.stages.sentinel.audio_hash_safe", return_value=(_GOOD, None)):
        assert SentinelStage().verify_effect(_Ctx(conn), _Result()) == []


# ── The dedup catastrophe ─────────────────────────────────────────────────────


def test_empty_stream_digest_is_reported(conn, tmp_path: Path) -> None:
    """ffmpeg exited 0 and decoded nothing. The hash is well-formed, shared
    by every such file, and reads as one EXACT duplicate group."""
    p = _track(tmp_path)
    _hashed(conn, p, audio_hash=_EMPTY_STREAM_SHA256)
    with patch(
        "musaeus.stages.sentinel.audio_hash_safe",
        return_value=(_EMPTY_STREAM_SHA256, None),
    ):
        problems = SentinelStage().verify_effect(_Ctx(conn), _Result())
    assert problems and "empty-stream digest" in problems[0]
    assert "duplicate group" in problems[0]


def test_empty_stream_is_caught_across_the_whole_batch_not_a_sample(conn, tmp_path: Path) -> None:
    """The sample is 3 files; the degenerate check must cover all of them.
    Finding this in a sample would be luck, and it only takes one row."""
    for i in range(20):
        h = _EMPTY_STREAM_SHA256 if i == 11 else f"{i:064d}"
        _hashed(conn, _track(tmp_path, f"t{i}.flac"), audio_hash=h)
    with patch("musaeus.stages.sentinel.audio_hash_safe", side_effect=lambda p: (None, "x")):
        problems = SentinelStage().verify_effect(_Ctx(conn), _Result())
    assert any("empty-stream digest" in p for p in problems)
    assert any("1 of 20" in p for p in problems)


# ── The update did not land ───────────────────────────────────────────────────


def test_logged_but_unstored_hash_is_reported(conn, tmp_path: Path) -> None:
    p = _track(tmp_path)
    _hashed(conn, p, audio_hash=None)
    problems = SentinelStage().verify_effect(_Ctx(conn), _Result())
    assert any("no audio_hash stored" in x for x in problems)


def test_a_hashed_row_left_pending_is_reported(conn, tmp_path: Path) -> None:
    """It will be re-selected and re-hashed on every run, for ever — hours of
    work that looks like progress."""
    p = _track(tmp_path)
    _hashed(conn, p, status="PENDING")
    with patch("musaeus.stages.sentinel.audio_hash_safe", return_value=(_GOOD, None)):
        problems = SentinelStage().verify_effect(_Ctx(conn), _Result())
    assert any("still PENDING" in x for x in problems)


# ── Asking the artifact ───────────────────────────────────────────────────────


def test_a_hash_that_does_not_describe_the_file_is_reported(conn, tmp_path: Path) -> None:
    """The only check that reads the file rather than the bookkeeping."""
    p = _track(tmp_path)
    _hashed(conn, p, audio_hash=_GOOD)
    with patch("musaeus.stages.sentinel.audio_hash_safe", return_value=(_OTHER, None)):
        problems = SentinelStage().verify_effect(_Ctx(conn), _Result())
    assert any("does not describe this file" in x for x in problems)


def test_a_hashed_file_that_vanished_is_reported(conn, tmp_path: Path) -> None:
    _hashed(conn, tmp_path / "gone.flac")
    problems = SentinelStage().verify_effect(_Ctx(conn), _Result())
    assert any("not on disk" in x for x in problems)


def test_the_timeout_fallback_is_not_cried_wolf_over(conn, tmp_path: Path) -> None:
    """audio_hash() falls back to a full-file hash on a real timeout, and
    says so. Re-deriving may take the other branch and disagree. That is
    working code, and verification that flags it gets switched off."""
    p = _track(tmp_path)
    same = "c" * 64
    _hashed(conn, p, audio_hash=same, full_hash=same)
    with patch("musaeus.stages.sentinel.audio_hash_safe", return_value=(_OTHER, None)) as m:
        assert SentinelStage().verify_effect(_Ctx(conn), _Result()) == []
    assert not m.called, "the fallback row should not be re-hashed at all"


# ── Silence is not evidence ───────────────────────────────────────────────────


def test_changes_claimed_with_no_events_is_a_problem_not_a_pass(conn) -> None:
    """Returning [] here would be the hollow 'verified' this hook exists to
    prevent: there was nothing to have looked at."""
    problems = SentinelStage().verify_effect(_Ctx(conn), _Result(files_changed=9))
    assert problems and "no HASH_COMPUTED" in problems[0]


def test_another_runs_hashes_are_not_this_run_s_evidence(conn, tmp_path: Path) -> None:
    _hashed(conn, _track(tmp_path), run="run_earlier")
    problems = SentinelStage().verify_effect(_Ctx(conn), _Result(files_changed=1))
    assert problems and "no HASH_COMPUTED" in problems[0]
