"""A process cannot pick up edits to its own source; it must notice.

On 2026-08-25 a fix to ClassicalComposerStage was written while a console
launched 2h45m earlier was still open. The next live run executed the old
code and stranded more files outside the vault, and the batch after that
was launched into the same stale process. Nothing in the system said a
word -- the code on disk was correct, and the run reported success.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from musaeus.safety import source_watch
from musaeus.safety.source_watch import SourceWatch, auto_restart_enabled


@pytest.fixture
def pkg(tmp_path: Path) -> Path:
    (tmp_path / "sub").mkdir()
    (tmp_path / "a.py").write_text("x = 1\n")
    (tmp_path / "sub" / "b.py").write_text("y = 2\n")
    (tmp_path / "notes.txt").write_text("not source\n")
    return tmp_path


class TestDriftDetection:
    def test_an_untouched_tree_does_not_drift(self, pkg):
        assert not SourceWatch(pkg).drifted()

    def test_an_edit_drifts(self, pkg):
        w = SourceWatch(pkg)
        (pkg / "a.py").write_text("x = 2\n")
        assert w.drifted()
        assert [p.name for p in w.drifted_files()] == ["a.py"]

    def test_a_new_file_drifts(self, pkg):
        w = SourceWatch(pkg)
        (pkg / "c.py").write_text("z = 3\n")
        assert w.drifted()

    def test_a_deleted_file_drifts(self, pkg):
        w = SourceWatch(pkg)
        (pkg / "sub" / "b.py").unlink()
        assert w.drifted()

    def test_a_touch_without_a_content_change_does_not_drift(self, pkg):
        # mtime-based detection would cry drift after any checkout or copy.
        w = SourceWatch(pkg)
        import os
        os.utime(pkg / "a.py", (0, 0))
        assert not w.drifted()

    def test_non_python_files_are_ignored(self, pkg):
        w = SourceWatch(pkg)
        (pkg / "notes.txt").write_text("changed\n")
        assert not w.drifted()

    def test_pycache_is_ignored(self, pkg):
        # Writing .pyc is a side effect of importing, not a code change.
        w = SourceWatch(pkg)
        cache = pkg / "__pycache__"
        cache.mkdir()
        (cache / "a.cpython-311.pyc").write_bytes(b"\x00\x01")
        (cache / "stale.py").write_text("junk = 1\n")
        assert not w.drifted()


class TestTheEscapeHatch:
    @pytest.mark.parametrize("val", ["0", "false", "no", "NO"])
    def test_it_can_be_disabled(self, monkeypatch, val):
        monkeypatch.setenv(source_watch.AUTO_RESTART_ENV, val)
        assert not auto_restart_enabled()

    def test_it_is_on_by_default(self, monkeypatch):
        monkeypatch.delenv(source_watch.AUTO_RESTART_ENV, raising=False)
        assert auto_restart_enabled()


class TestTheConsoleGate:
    def _console(self, monkeypatch, drifted: bool):
        from musaeus.console import Console

        c = Console.__new__(Console)  # no __init__: it fingerprints the real tree

        class _W:
            def __init__(self):
                self.restarted = False

            def drifted(self):
                return drifted

            def drifted_files(self):
                return [Path("musaeus/stages/classical_composer.py")]

            def restart(self):
                self.restarted = True

        c._source = _W()
        return c

    def test_a_clean_tree_proceeds(self, monkeypatch):
        c = self._console(monkeypatch, drifted=False)
        assert c._check_source_drift() is True
        assert not c._source.restarted

    def test_drift_restarts_by_default(self, monkeypatch):
        monkeypatch.delenv(source_watch.AUTO_RESTART_ENV, raising=False)
        c = self._console(monkeypatch, drifted=True)
        c._check_source_drift()
        assert c._source.restarted, "should have replaced the process"

    def test_drift_refuses_when_restart_is_disabled(self, monkeypatch):
        monkeypatch.setenv(source_watch.AUTO_RESTART_ENV, "0")
        c = self._console(monkeypatch, drifted=True)
        # The fallback: refuse rather than run the old code.
        assert c._check_source_drift() is False
        assert not c._source.restarted
