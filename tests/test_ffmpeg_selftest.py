"""The version-gated ffmpeg damage-detector compatibility canary."""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from musaeus.stages import preflight
from musaeus.stages.base import BaseStage


def _context(disposable_vault):
    disposable_vault.cfg.ensure_dirs()
    conn = disposable_vault.open_db()
    return conn, disposable_vault.new_context(conn)


def test_unchanged_version_does_not_run_the_canary(disposable_vault, monkeypatch):
    conn, ctx = _context(disposable_vault)
    try:
        version = "ffmpeg version already-tested"
        state = ctx.runs_root / "ffmpeg_selftest.json"
        state.write_text(
            json.dumps({"version": version, "result": "pass", "date": "2026-01-01T00:00:00+00:00"}),
            encoding="utf-8",
        )
        monkeypatch.setattr(preflight.shutil, "which", lambda _cmd: "ffmpeg")
        monkeypatch.setattr(preflight.PreflightStage, "_ffmpeg_version", staticmethod(lambda: version))

        def should_not_run():
            raise AssertionError("an unchanged ffmpeg version must not run the canary")

        monkeypatch.setattr(preflight.PreflightStage, "_run_ffmpeg_selftest", staticmethod(should_not_run))
        ok, warn, fail = [], [], []
        preflight.PreflightStage()._check_ffmpeg_selftest(ctx, ok, warn, fail)
        assert any("already passed" in line for line in ok)
        assert not warn and not fail
    finally:
        conn.close()


def test_windows_pointer_without_0x_is_still_damage(monkeypatch):
    """Windows ffmpeg omits 0x before its context pointer."""
    from musaeus import loudness
    from musaeus.stages import corrupt

    monkeypatch.setattr(corrupt, "audio_stream_index", lambda _path: 0)
    stderr = (
        "[mov,mp4,m4a,3gp,3g2,mj2 @ 0000018f5e7c2a40] [error] "
        "stream 0, offset 0xeaa50: partial file\n"
    )
    assert loudness._decode_damage(stderr, Path("cut.m4a"))


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg unavailable")
def test_changed_version_runs_real_canary_and_saves_result(disposable_vault, monkeypatch):
    conn, ctx = _context(disposable_vault)
    try:
        version = preflight.PreflightStage._ffmpeg_version()
        assert version is not None
        state = ctx.runs_root / "ffmpeg_selftest.json"
        state.write_text(
            json.dumps({"version": "ffmpeg version old", "result": "pass", "date": "2026-01-01"}),
            encoding="utf-8",
        )
        monkeypatch.setattr(preflight.PreflightStage, "_ffmpeg_version", staticmethod(lambda: version))

        ok, warn, fail = [], [], []
        preflight.PreflightStage()._check_ffmpeg_selftest(ctx, ok, warn, fail)

        assert any("passed" in line for line in ok)
        assert not warn and not fail
        saved = json.loads(state.read_text(encoding="utf-8"))
        assert saved["version"] == version
        assert saved["result"] == "pass"
        assert saved["date"]
    finally:
        conn.close()


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg unavailable")
def test_clean_cut_detector_fails_writes_handoff_and_does_not_save_version(
    disposable_vault, monkeypatch
):
    conn, ctx = _context(disposable_vault)
    try:
        version = preflight.PreflightStage._ffmpeg_version()
        assert version is not None
        real_measure = preflight.loudness.measure_loudness

        def calls_cut_file_clean(path):
            measured = real_measure(path)
            return (measured[0], measured[1], "ok") if path.name == "cut.m4a" else measured

        monkeypatch.setattr(preflight.PreflightStage, "_ffmpeg_version", staticmethod(lambda: version))
        monkeypatch.setattr(preflight.loudness, "measure_loudness", calls_cut_file_clean)

        stage = preflight.PreflightStage()
        ok, warn, fail = [], [], []
        stage._check_ffmpeg_selftest(ctx, ok, warn, fail)

        assert stage.ffmpeg_selftest_failed
        assert any("damage checks cannot be trusted" in line for line in fail)
        assert not (ctx.runs_root / "ffmpeg_selftest.json").exists()
        notes = list((ctx.runs_root / "HANDOFFS").glob("ffmpeg_selftest_FAILED_*.txt"))
        assert len(notes) == 1
        note = notes[0].read_text(encoding="utf-8")
        assert note.startswith("Paste this note into any AI chat")
        assert version in note
        assert "loudness did not mark the cut ALAC damaged" in note
        assert "Exact stderr from ffmpeg decoding the cut ALAC:" in note
        assert note.split("Exact stderr from ffmpeg decoding the cut ALAC:\n", 1)[1].strip()
    finally:
        conn.close()


def test_selftest_failure_stops_pipeline_before_next_stage(disposable_vault, monkeypatch):
    import musaeus.cli as cli

    called = []

    def forced_failure(self, _ctx, _ok, _warn, fail):
        fail.append("forced ffmpeg self-test failure")
        self.ffmpeg_selftest_failed = True

    class WouldChangeFiles(BaseStage):
        NAME = "would_change_files"

        def validate(self, ctx):
            pass

        def run(self, ctx):
            called.append(True)
            return self._make_result()

        def dry_run(self, ctx):
            return self._make_result(dry_run=True)

    monkeypatch.setattr(cli, "get_config", lambda: disposable_vault.cfg)
    monkeypatch.setattr(cli, "_RESUME_FILE", disposable_vault.config_home / "resume.json")
    monkeypatch.setattr(preflight.PreflightStage, "_check_ffmpeg_selftest", forced_failure)

    assert cli._run_pipeline_inner([preflight.PreflightStage, WouldChangeFiles], dry_run=False) == 1
    assert not called
