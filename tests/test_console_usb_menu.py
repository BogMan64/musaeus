"""The console's "Transfer to USB" entry.

transfer_to_usb.py's own docstring lays out five safety gates (denylist,
typed confirmation, TTY check, root check, --execute gate) and is explicit
that none of them may be re-implemented or bypassed elsewhere. This menu
is a front door only: it shells out to that exact script, unmodified, and
never calls subprocess with --execute unless the operator additionally
types "yes" here. These tests monkeypatch subprocess.run to a recorder so
nothing real ever executes.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from musaeus.config import MusicConfig
from musaeus.console import Console


@pytest.fixture
def cfg(tmp_path: Path) -> MusicConfig:
    return MusicConfig(
        vault_root=tmp_path, inbox=tmp_path / "INBOX", staging=tmp_path / "STAGING",
        quarantine=tmp_path / "QUARANTINE", runs_root=tmp_path / "RUNS",
        meta_dir=tmp_path / "MetaData", alac_library=tmp_path / "ALAC-Library",
        db_path=tmp_path / "musaeus.db",
    )


def _console(cfg: MusicConfig) -> Console:
    con = Console()
    con._config = cfg
    return con


class _Recorder:
    def __init__(self) -> None:
        self.calls: list[list[str]] = []

    def __call__(self, cmd, *a, **k):
        self.calls.append(list(cmd))

        class _Result:
            returncode = 0

        return _Result()


class TestUSBMenu:
    def test_back_does_nothing(self, cfg, monkeypatch, capsys) -> None:
        con = _console(cfg)
        rec = _Recorder()
        monkeypatch.setattr("subprocess.run", rec)
        monkeypatch.setattr("builtins.input", lambda *a, **k: "2")
        con._usb_menu()
        assert rec.calls == []

    def test_dry_run_always_happens_first(self, cfg, monkeypatch, capsys) -> None:
        con = _console(cfg)
        rec = _Recorder()
        monkeypatch.setattr("subprocess.run", rec)
        responses = iter(["0", "/dev/sdz", "no"])
        monkeypatch.setattr("builtins.input", lambda *a, **k: next(responses))
        con._usb_menu()
        assert len(rec.calls) == 1
        assert "--execute" not in rec.calls[0]
        assert "--library" in rec.calls[0] and "alac" in rec.calls[0]
        assert "--device" in rec.calls[0] and "/dev/sdz" in rec.calls[0]

    def test_declining_the_yes_prompt_stops_before_execute(self, cfg, monkeypatch, capsys) -> None:
        con = _console(cfg)
        rec = _Recorder()
        monkeypatch.setattr("subprocess.run", rec)
        responses = iter(["1", "/dev/sdz", "not yes"])
        monkeypatch.setattr("builtins.input", lambda *a, **k: next(responses))
        con._usb_menu()
        out = capsys.readouterr().out
        assert len(rec.calls) == 1, "must not proceed to a second (execute) call"
        assert "Stopped" in out

    def test_typing_yes_proceeds_to_execute(self, cfg, monkeypatch, capsys) -> None:
        con = _console(cfg)
        rec = _Recorder()
        monkeypatch.setattr("subprocess.run", rec)
        monkeypatch.setattr("os.geteuid", lambda: 0)
        responses = iter(["1", "/dev/sdz", "yes"])
        monkeypatch.setattr("builtins.input", lambda *a, **k: next(responses))
        con._usb_menu()
        assert len(rec.calls) == 2
        assert "--execute" in rec.calls[1]
        assert "--library" in rec.calls[1] and "car" in rec.calls[1]

    def test_non_root_gets_sudo_prefixed_not_silently_skipped(self, cfg, monkeypatch, capsys) -> None:
        con = _console(cfg)
        rec = _Recorder()
        monkeypatch.setattr("subprocess.run", rec)
        monkeypatch.setattr("os.geteuid", lambda: 1000)
        responses = iter(["0", "/dev/sdz", "yes"])
        monkeypatch.setattr("builtins.input", lambda *a, **k: next(responses))
        con._usb_menu()
        assert len(rec.calls) == 2
        assert rec.calls[1][0] == "sudo"
        assert "--execute" in rec.calls[1]

    def test_root_does_not_get_an_unnecessary_sudo_prefix(self, cfg, monkeypatch, capsys) -> None:
        con = _console(cfg)
        rec = _Recorder()
        monkeypatch.setattr("subprocess.run", rec)
        monkeypatch.setattr("os.geteuid", lambda: 0)
        responses = iter(["0", "/dev/sdz", "yes"])
        monkeypatch.setattr("builtins.input", lambda *a, **k: next(responses))
        con._usb_menu()
        assert rec.calls[1][0] != "sudo"

    def test_blank_device_omits_the_device_flag(self, cfg, monkeypatch, capsys) -> None:
        con = _console(cfg)
        rec = _Recorder()
        monkeypatch.setattr("subprocess.run", rec)
        responses = iter(["0", "", "no"])
        monkeypatch.setattr("builtins.input", lambda *a, **k: next(responses))
        con._usb_menu()
        assert "--device" not in rec.calls[0]
