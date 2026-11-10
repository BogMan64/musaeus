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

import sys
from pathlib import Path

import pytest

from musaeus.config import MusicConfig
from musaeus.console import Console


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


def test_on_windows_the_menu_says_so_and_touches_nothing(cfg, monkeypatch, capsys) -> None:
    """The transfer is Linux-only (wipefs, parted, mkfs, sudo); on Windows the
    menu called os.geteuid after the prompts and crashed (Windows port)."""
    con = _console(cfg)
    rec = _Recorder()
    monkeypatch.setattr("subprocess.run", rec)
    asked: list[str] = []
    monkeypatch.setattr("builtins.input", lambda prompt="": asked.append(prompt) or "")
    monkeypatch.setattr(sys, "platform", "win32")

    con._usb_menu()

    assert rec.calls == []
    assert "Linux only" in capsys.readouterr().out
    # It waits for Enter, so the main menu does not scroll the message away.
    assert len(asked) == 1 and "Press Enter" in asked[0]


@pytest.mark.skipif(sys.platform == "win32", reason="the USB transfer is Linux-only")
class TestUSBMenu:
    def test_back_does_nothing(self, cfg, monkeypatch, capsys) -> None:
        con = _console(cfg)
        rec = _Recorder()
        monkeypatch.setattr("subprocess.run", rec)
        monkeypatch.setattr("builtins.input", lambda *a, **k: "4")  # 4 = Back
        con._usb_menu()
        assert rec.calls == []

    def test_dry_run_always_happens_first(self, cfg, monkeypatch, capsys) -> None:
        con = _console(cfg)
        rec = _Recorder()
        monkeypatch.setattr("subprocess.run", rec)
        responses = iter(["1", "/dev/sdz", "no"])  # 1 = ALAC
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
        responses = iter(["2", "/dev/sdz", "not yes"])  # 2 = Car
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
        responses = iter(["2", "/dev/sdz", "yes"])  # 2 = Car
        monkeypatch.setattr("builtins.input", lambda *a, **k: next(responses))
        con._usb_menu()
        assert len(rec.calls) == 2
        assert "--execute" in rec.calls[1]
        assert "--library" in rec.calls[1] and "car" in rec.calls[1]

    def test_non_root_gets_sudo_prefixed_not_silently_skipped(
        self, cfg, monkeypatch, capsys
    ) -> None:
        con = _console(cfg)
        rec = _Recorder()
        monkeypatch.setattr("subprocess.run", rec)
        monkeypatch.setattr("os.geteuid", lambda: 1000)
        responses = iter(["1", "/dev/sdz", "yes"])  # 1 = ALAC
        monkeypatch.setattr("builtins.input", lambda *a, **k: next(responses))
        con._usb_menu()
        assert len(rec.calls) == 2
        assert rec.calls[1][0] == "sudo"
        assert rec.calls[1][1] == "-E", "plain sudo resets HOME and breaks config file lookup"
        assert "--execute" in rec.calls[1]

    def test_root_does_not_get_an_unnecessary_sudo_prefix(self, cfg, monkeypatch, capsys) -> None:
        con = _console(cfg)
        rec = _Recorder()
        monkeypatch.setattr("subprocess.run", rec)
        monkeypatch.setattr("os.geteuid", lambda: 0)
        responses = iter(["1", "/dev/sdz", "yes"])  # 1 = ALAC
        monkeypatch.setattr("builtins.input", lambda *a, **k: next(responses))
        con._usb_menu()
        assert rec.calls[1][0] != "sudo"

    def test_blank_device_omits_the_device_flag(self, cfg, monkeypatch, capsys) -> None:
        con = _console(cfg)
        rec = _Recorder()
        monkeypatch.setattr("subprocess.run", rec)
        responses = iter(["1", "", "no"])  # 1 = ALAC
        monkeypatch.setattr("builtins.input", lambda *a, **k: next(responses))
        con._usb_menu()
        assert "--device" not in rec.calls[0]


def test_the_entry_is_hidden_when_the_script_is_not_installed(cfg, monkeypatch, tmp_path) -> None:
    # Codex review of #219: the Debian package has no scripts/, so the
    # entry led only to "Script not found".
    import musaeus.console as console

    seen: list[str] = []
    monkeypatch.setattr(console, "_USB_SCRIPT", tmp_path / "missing.py", raising=False)
    monkeypatch.setattr(
        console, "_choose", lambda prompt, labels, *a, **k: seen.extend(labels) or "x"
    )
    _console(cfg)._main_menu()
    assert "Quit" in seen
    assert "Transfer to USB / iPhone…" not in seen


def test_the_iphone_entry_runs_iphone_sync(cfg, monkeypatch) -> None:
    """Grey 11-09: iPhone sync belongs in the menu, beside the USB transfer."""
    import dataclasses

    import musaeus.iphone_sync as iphone_sync

    seen = []
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(iphone_sync, "sync", lambda src, mount: seen.append((src, mount)) or 0)
    responses = iter(["3", ""])  # 3 = iPhone, then Enter to go back
    monkeypatch.setattr("builtins.input", lambda *a, **k: next(responses))
    con = _console(dataclasses.replace(cfg, iphone_library=cfg.vault_root / "iPHONE_Library"))
    con._usb_menu()
    assert seen == [(cfg.vault_root / "iPHONE_Library", Path.home() / "iphone")]
