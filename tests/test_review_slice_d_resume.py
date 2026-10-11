"""Review of slice D (#146), findings 1-4: what a run counts as done.

1. The resume guard looked only for *.m4a in INBOX: a FLAC or MP3 batch was
   skipped by an auto-resume ("IngestStage already done"), exit 0.
2. Resume skipped stages by name: after a failed stage was retried, later
   stages that had succeeded the first time were skipped and never saw the
   retry's work.
3. A stage whose effect check found problems still counted as done.
4. A check that crashed read as "no claim", and the run as clean.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from musaeus import cli
from musaeus.stages.base import BaseStage


@pytest.fixture
def resume_file(tmp_path, monkeypatch):
    f = tmp_path / "resume_state.json"
    monkeypatch.setattr(cli, "_RESUME_FILE", f)
    return f


@pytest.mark.parametrize("name", ["song.flac", "SONG.FLAC", "track.mp3", "take.wav"])
def test_any_audio_in_the_inbox_stops_an_auto_resume(tmp_path, name):
    inbox = tmp_path / "INBOX"
    inbox.mkdir()
    (inbox / name).write_bytes(b"x")
    assert cli._resume_would_skip_new_work(["IngestStage"], inbox)


def test_a_resume_reruns_everything_after_the_first_stage_not_done(resume_file):
    stages = ["Ingest", "Sentinel", "Scholar", "Finalize"]
    cli._save_resume(["Ingest", "Scholar", "Finalize"], stages)  # Sentinel failed
    assert cli._load_resume(stages) == ["Ingest"]


def test_a_resume_from_another_vault_skips_nothing(resume_file):
    stages = ["Ingest", "Sentinel"]
    cli._save_resume(["Ingest"], stages, "/test/vault/musaeus.db")
    assert cli._load_resume(stages, "/real/vault/musaeus.db") is None


class _Stage(BaseStage):
    NAME = "test-stage"

    def __init__(self, verify):
        self._verify = verify

    def validate(self, ctx): ...

    def dry_run(self, ctx):
        return self._make_result(dry_run=True)

    def run(self, ctx):
        r = self._make_result(dry_run=False)
        r.files_changed = 5
        return r

    def verify_effect(self, ctx, result):
        return self._verify()


@pytest.fixture
def ctx():
    events = []
    return SimpleNamespace(
        dry_run=False, record_stage=lambda _r: None, conn=None,
        log_event=lambda *a, **k: events.append((a, k)), events=events,
    )  # fmt: skip


def test_an_unconfirmed_effect_is_not_done(ctx):
    r = _Stage(lambda: ["no sampled file has the change"]).execute(ctx)
    assert r.success is False, "an unverified stage counted as done"
    assert any(a[0] == "STAGE_UNVERIFIED" for a, _ in ctx.events)


def test_a_check_that_crashed_is_counted(ctx):
    def boom():
        raise OSError("unreadable sample")

    r = _Stage(boom).execute(ctx)
    assert any("effect check failed to run" in e for e in r.errors), "read as no claim"
