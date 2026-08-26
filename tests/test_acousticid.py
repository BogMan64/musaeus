"""
AcousticIDStage — a no-answer must not settle a row.

This stage had never executed: 386 lines, wired into DEFAULT_PIPELINE,
fpcalc installed, key set, 0 events ever, and no test file. What it had
was finding #15, one level down from mb_enrich's (f84e643).

_acousticid_lookup collapsed three outcomes into one. A transport failure
raised and was swallowed by a bare `except Exception`; a service error and
a genuine miss both returned None. The caller then fell through to a
single UPDATE that wrote chromaprint, acousticid_recording and
acousticid_checked_at together -- and selection was on `chromaprint IS
NULL`. So one timeout wrote a fingerprint, no recording, and removed the
track from the queue permanently. Not "marked as checked": structurally
unreachable, past any force flag.

The assertions here are on WHICH ROWS A LATER RUN WOULD STILL SELECT, not
on counters. Counters are what pass vacuously: the old code reported
errors=0 while retiring rows for ever, and a socket-blocked suite reports
matched=0 for a stage that is working and for one that is not.

fpcalc is local, so fingerprinting is exercised for real against
ffmpeg-generated audio. Only the HTTP layer is faked, and it is faked at
urlopen rather than at _acousticid_lookup, so the lookup's own parsing and
branching -- where the three states are actually decided -- is under test.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import urllib.error
from pathlib import Path

import pytest

from musaeus.config import MusicConfig
from musaeus.context import RunContext
from musaeus.db import open_db, upsert_archive
from musaeus.stages import acousticid as acoustic_mod
from musaeus.stages.acousticid import AcousticIDStage

pytestmark = pytest.mark.skipif(
    not (shutil.which("ffmpeg") and shutil.which("fpcalc")),
    reason="ffmpeg/fpcalc not available",
)

_DURATION_S = 35  # must exceed _MIN_DURATION_S (30)


@pytest.fixture
def cfg(tmp_path: Path) -> MusicConfig:
    return MusicConfig(
        vault_root=tmp_path,
        inbox=tmp_path / "INBOX",
        staging=tmp_path / "STAGING",
        quarantine=tmp_path / "QUARANTINE",
        runs_root=tmp_path / "RUNS",
        meta_dir=tmp_path / "MetaData",
        alac_library=tmp_path / "ALAC-Library",
        db_path=tmp_path / "musaeus.db",
        acousticid_api_key="test-key",
    )


class _FakeResponse:
    def __init__(self, payload: dict):
        self._payload = json.dumps(payload).encode("utf-8")

    def read(self) -> bytes:
        return self._payload

    def __enter__(self):
        return self

    def __exit__(self, *_a):
        return False


def _serve(monkeypatch, payload: dict | None = None, raises: Exception | None = None):
    """Fake the HTTP layer only. _network_check is neutralised separately so
    the suite's socket block does not pre-empt the branch under test."""
    monkeypatch.setattr(acoustic_mod, "_network_check", lambda *_a, **_k: None)

    def _fake_urlopen(*_a, **_k):
        if raises is not None:
            raise raises
        return _FakeResponse(payload or {})

    monkeypatch.setattr(acoustic_mod, "urlopen", _fake_urlopen)


def _setup(cfg: MusicConfig):
    cfg.ensure_dirs()
    src = cfg.inbox / "track.flac"
    src.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["ffmpeg", "-y", "-f", "lavfi", "-i", f"sine=frequency=440:duration={_DURATION_S}",
         "-c:a", "flac", str(src)],
        capture_output=True, check=True,
    )
    conn = open_db(cfg.db_path)
    ctx = RunContext.new(cfg, conn, dry_run=False)
    upsert_archive(conn, {
        "file_path": str(src), "filename": src.name, "ext": ".flac",
        "status": "CATALOGUED", "codec": "flac", "artist": "A", "title": "T",
        "duration": float(_DURATION_S), "channels": 1, "sample_rate": 44100,
    })
    conn.commit()
    return conn, ctx, src


def _would_reselect(conn) -> int:
    """Rows a LATER run would still ask about. This is the real assertion:
    a settled row is one this returns 0 for, and a deferred row is one it
    returns 1 for. Counters cannot tell those apart."""
    return conn.execute(
        "SELECT COUNT(*) FROM archive "
        "WHERE status='CATALOGUED' AND acousticid_checked_at IS NULL"
    ).fetchone()[0]


def _row(conn):
    return conn.execute(
        "SELECT chromaprint, acousticid_recording, acousticid_checked_at FROM archive"
    ).fetchone()


_MATCH = {"status": "ok", "results": [
    {"score": 0.95, "recordings": [{"id": "rec-abc-123"}]}]}
_NO_MATCH = {"status": "ok", "results": []}
_LOW_SCORE = {"status": "ok", "results": [
    {"score": 0.42, "recordings": [{"id": "rec-should-be-ignored"}]}]}


class TestAnswersSettleTheRow:
    def test_a_match_settles_and_records_the_recording(self, cfg, monkeypatch):
        conn, ctx, _ = _setup(cfg)
        _serve(monkeypatch, _MATCH)

        result = AcousticIDStage().run(ctx)

        assert result.files_processed > 0, "vacuous: nothing was processed"
        row = _row(conn)
        assert row["acousticid_recording"] == "rec-abc-123"
        assert row["acousticid_checked_at"] is not None
        assert _would_reselect(conn) == 0
        conn.close()

    def test_a_definitive_no_match_still_settles(self, cfg, monkeypatch):
        """The marker's whole purpose. This must keep passing."""
        conn, ctx, _ = _setup(cfg)
        _serve(monkeypatch, _NO_MATCH)

        result = AcousticIDStage().run(ctx)

        assert result.files_processed > 0
        row = _row(conn)
        assert row["acousticid_recording"] is None
        assert row["acousticid_checked_at"] is not None, "an answer of 'no' is still an answer"
        assert _would_reselect(conn) == 0
        conn.close()

    def test_a_low_score_is_a_no_match_not_a_failure(self, cfg, monkeypatch):
        conn, ctx, _ = _setup(cfg)
        _serve(monkeypatch, _LOW_SCORE)

        AcousticIDStage().run(ctx)

        row = _row(conn)
        assert row["acousticid_recording"] is None
        assert row["acousticid_checked_at"] is not None
        assert _would_reselect(conn) == 0
        conn.close()


class TestNoAnswerDoesNotSettle:
    """Finding #15. Each of these fails on the pre-fix stage."""

    @pytest.mark.parametrize(
        "failure",
        [
            urllib.error.URLError("dns failure"),
            urllib.error.HTTPError("u", 503, "Service Unavailable", {}, None),
            TimeoutError("read timed out"),
            ValueError("Expecting value: line 1 column 1"),  # unparseable body
        ],
        ids=["dns", "http-503", "timeout", "unparseable"],
    )
    def test_transport_failure_leaves_the_row_selectable(self, cfg, monkeypatch, failure):
        conn, ctx, _ = _setup(cfg)
        _serve(monkeypatch, raises=failure)

        result = AcousticIDStage().run(ctx)

        assert result.files_processed > 0, "vacuous: nothing was processed"
        row = _row(conn)
        assert row["acousticid_checked_at"] is None, (
            "a network failure was recorded as an answer; this row can never be asked again"
        )
        assert row["chromaprint"] is not None, (
            "the fingerprint is a local fact and should be kept even when the lookup fails"
        )
        assert _would_reselect(conn) == 1
        conn.close()

    def test_service_error_status_is_not_an_answer(self, cfg, monkeypatch):
        conn, ctx, _ = _setup(cfg)
        _serve(monkeypatch, {"status": "error", "error": {"message": "invalid api key"}})

        AcousticIDStage().run(ctx)

        assert _row(conn)["acousticid_checked_at"] is None
        assert _would_reselect(conn) == 1
        conn.close()

    def test_missing_api_key_does_not_settle_anything(self, cfg, monkeypatch):
        """Worst case of the old shape: no lookup is even attempted, yet
        every row was retired."""
        cfg.acousticid_api_key = None
        conn, ctx, _ = _setup(cfg)

        AcousticIDStage().run(ctx)

        row = _row(conn)
        assert row["chromaprint"] is not None, "fingerprints are still worth storing"
        assert row["acousticid_checked_at"] is None
        assert _would_reselect(conn) == 1
        conn.close()


class TestFingerprintReuse:
    def test_a_retry_does_not_recompute_the_fingerprint(self, cfg, monkeypatch):
        """Now that a no-answer stays selectable, the retry must not pay
        fpcalc over the whole library again."""
        conn, ctx, src = _setup(cfg)
        calls = {"n": 0}
        real = acoustic_mod._fpcalc

        def _counting(path):
            calls["n"] += 1
            return real(path)

        monkeypatch.setattr(acoustic_mod, "_fpcalc", _counting)

        _serve(monkeypatch, raises=urllib.error.URLError("down"))
        AcousticIDStage().run(ctx)
        first = _row(conn)["chromaprint"]
        assert calls["n"] == 1

        _serve(monkeypatch, _MATCH)
        AcousticIDStage().run(ctx)

        assert calls["n"] == 1, "fpcalc was re-run for a fingerprint already stored"
        row = _row(conn)
        assert row["chromaprint"] == first
        assert row["acousticid_recording"] == "rec-abc-123"
        assert _would_reselect(conn) == 0
        conn.close()


class TestVerifyEffect:
    def test_a_stored_fingerprint_that_does_not_match_the_audio_is_caught(
        self, cfg, monkeypatch
    ):
        conn, ctx, _ = _setup(cfg)
        _serve(monkeypatch, _MATCH)
        result = AcousticIDStage().run(ctx)

        assert AcousticIDStage().verify_effect(ctx, result) == []

        conn.execute("UPDATE archive SET chromaprint='not-the-real-fingerprint'")
        conn.commit()
        problems = AcousticIDStage().verify_effect(ctx, result)

        assert problems and "does not match the audio" in problems[0]
        conn.close()

    def test_a_marked_row_without_a_fingerprint_is_incoherent(self, cfg, monkeypatch):
        conn, ctx, _ = _setup(cfg)
        _serve(monkeypatch, _MATCH)
        result = AcousticIDStage().run(ctx)

        conn.execute("UPDATE archive SET chromaprint=NULL")
        conn.commit()
        problems = AcousticIDStage().verify_effect(ctx, result)

        assert any("no fingerprint" in p for p in problems)
        conn.close()


class TestShortFileFingerprinting:
    """fpcalc's exit code is not its verdict.

    It exits non-zero for any input shorter than its ~120s read window
    while still writing a complete fingerprint to stdout. The stage used
    to raise on rc != 0, discarding a good fingerprint for every track
    between _MIN_DURATION_S and ~120s -- 59 of 2,028 rows (2.9%) in the
    2026-08-26 batch. The fixture audio here is deliberately short enough
    to sit in that band.
    """

    def test_fpcalc_returns_a_fingerprint_despite_a_nonzero_exit(self, cfg):
        cfg.ensure_dirs()
        src = cfg.inbox / "short.flac"
        src.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(
            ["ffmpeg", "-y", "-f", "lavfi", "-i", "sine=frequency=440:duration=35",
             "-c:a", "flac", str(src)],
            capture_output=True, check=True,
        )
        raw = subprocess.run(
            [shutil.which("fpcalc"), "-json", str(src)], capture_output=True, text=True
        )
        assert raw.returncode != 0, (
            "premise: this fixture is meant to sit under fpcalc's read window; "
            "if fpcalc stopped doing this, the guard below is no longer needed"
        )

        duration, fingerprint = acoustic_mod._fpcalc(str(src))
        assert fingerprint, "a usable fingerprint was discarded on the strength of an exit code"
        assert duration == pytest.approx(35, abs=1)

    def test_a_genuinely_unreadable_file_still_raises(self, cfg):
        cfg.ensure_dirs()
        junk = cfg.inbox / "notaudio.flac"
        junk.parent.mkdir(parents=True, exist_ok=True)
        junk.write_bytes(b"this is not audio")
        with pytest.raises(ValueError):
            acoustic_mod._fpcalc(str(junk))
