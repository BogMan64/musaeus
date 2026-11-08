"""Missing-tool hints on Windows (Windows port, 2026-11-08).

Windows has no apt. Preflight printed 'Fix: sudo apt install ...' there and,
on a yes, tried to run 'sudo apt-get install'. Doctor listed iPhone and
backup tools whose features are Linux-only for now. Linux keeps its text.
"""

from __future__ import annotations

import sys

import pytest

from musaeus.config import MusicConfig
from musaeus.doctor import Report, _external_tools_present
from musaeus.install_hint import install_hint
from musaeus.stages import preflight
from musaeus.stages.preflight import PreflightStage


def test_linux_keeps_the_apt_command(monkeypatch):
    monkeypatch.setattr(sys, "platform", "linux")
    assert install_hint("fdkaac") == "sudo apt install fdkaac"


def test_windows_says_download_and_path_not_apt(monkeypatch):
    monkeypatch.setattr(sys, "platform", "win32")
    hint = install_hint("libchromaprint-tools")
    assert "apt" not in hint and "sudo" not in hint
    assert "fpcalc" in hint and "PATH" in hint


def test_preflight_on_windows_never_offers_apt(monkeypatch):
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(preflight.shutil, "which", lambda cmd: None)
    ran: list[list[str]] = []
    monkeypatch.setattr(preflight.subprocess, "run", lambda cmd, **k: ran.append(cmd))
    monkeypatch.setattr(PreflightStage, "_confirm", staticmethod(lambda *a, **k: True))
    stage = PreflightStage()
    ok: list[str] = []
    warn: list[str] = []
    fail: list[str] = []

    stage._check_commands(ok, warn, fail)
    stage._offer_installs(ok, warn, fail)

    assert fail and not any("apt" in f for f in fail)
    assert not any(cmd[:2] == ["sudo", "apt-get"] for cmd in ran)


@pytest.fixture
def cfg(tmp_path) -> MusicConfig:
    return MusicConfig(
        vault_root=tmp_path, inbox=tmp_path / "INBOX", staging=tmp_path / "S",
        quarantine=tmp_path / "Q", runs_root=tmp_path / "RUNS", meta_dir=tmp_path / "MetaData",
        alac_library=tmp_path / "L", db_path=tmp_path / "musaeus.db",
    )  # fmt: skip


def test_doctor_on_windows_skips_the_linux_only_tools(cfg, monkeypatch):
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(
        "musaeus.doctor.shutil.which",
        lambda n: None if n in ("ifuse", "idevice_id", "rsync") else f"C:\\tools\\{n}.exe",
    )
    rep = Report()
    _external_tools_present(cfg, rep)
    (f,) = [f for f in rep.findings if f.check == "external tools"]
    assert f.level == "ok", f.detail
