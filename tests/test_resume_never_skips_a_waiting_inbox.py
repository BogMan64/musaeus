"""A resume must not walk past files waiting in the INBOX.

The resume marker survives a FAILED run, not only an interrupted one. On
2026-09-20 a run ended `sentinel: FAILED` on one undecodable file, leaving
"28 stages done" behind. The next run found the marker, saw no TTY,
auto-resumed, printed

    ⏭  IngestStage (already done)

and walked past **2,061 freshly staged files**. It reported success and did
nothing. Nothing errored and nothing was lost -- the only symptom was a
library that did not grow, which is invisible unless somebody asks.

Auto-resume is correct for its purpose: an interrupted overnight run should
not restart from zero. It is wrong the moment new work has arrived since.
INBOX holding audio is exactly that signal, because the inbox exists to be
drained by IngestStage.
"""

from __future__ import annotations

import pytest

from musaeus.cli import _resume_would_skip_new_work


@pytest.fixture
def inbox(tmp_path):
    d = tmp_path / "INBOX"
    d.mkdir()
    return d


class TestResumeGuard:
    def test_a_waiting_inbox_blocks_a_resume_that_skips_ingest(self, inbox):
        (inbox / "track.m4a").write_bytes(b"not really audio")
        assert _resume_would_skip_new_work(["PreflightStage", "IngestStage"], inbox) is True

    def test_an_empty_inbox_allows_the_resume(self, inbox):
        """The normal case: an overnight run died mid-way with nothing new
        staged since. Restarting from zero would throw away hours."""
        assert _resume_would_skip_new_work(["PreflightStage", "IngestStage"], inbox) is False

    def test_a_resume_that_has_not_reached_ingest_is_fine(self, inbox):
        """If IngestStage has not run yet, resuming still ingests -- the
        waiting files are picked up, so there is nothing to guard against."""
        (inbox / "track.m4a").write_bytes(b"not really audio")
        assert _resume_would_skip_new_work(["PreflightStage"], inbox) is False

    def test_a_non_audio_file_is_not_work(self, inbox):
        """Manifests, logs and stray dotfiles live in the inbox too. Only
        audio means there is ingesting to do."""
        (inbox / "notes.txt").write_text("hello")
        (inbox / ".hidden").write_text("")
        assert _resume_would_skip_new_work(["IngestStage"], inbox) is False

    def test_audio_in_a_subfolder_still_counts(self, inbox):
        """Grey stages by copying folders in; ingest walks recursively, so
        the guard must too, or a whole batch is skipped."""
        sub = inbox / "batch_01"
        sub.mkdir()
        (sub / "track.m4a").write_bytes(b"not really audio")
        assert _resume_would_skip_new_work(["IngestStage"], inbox) is True

    def test_a_missing_inbox_does_not_raise(self, tmp_path):
        """A vault without an INBOX yet must not crash the resume check --
        failing closed here would block every resume on a fresh install."""
        assert _resume_would_skip_new_work(["IngestStage"], tmp_path / "nope") is False
