"""Is every program MUSAEUS shells out to actually installed?

Absence is not the same for all of them, which is why this does not report
one number:

  ffmpeg / ffprobe   nothing works at all without these
  fpcalc             the AcoustID stage quietly does nothing. It was
                     installed on this machine all along and the stage
                     STILL had produced zero rows -- the sort of gap that
                     only turns up when somebody goes looking.
  ifuse / idevice_id the iPhone edition builds perfectly and then has no
                     way onto the phone, which is a bad moment to find out.

A missing optional tool is a note carrying the command that fixes it, not a
failure. MUSAEUS runs fine without ifuse if you never touch an iPhone.
"""

from __future__ import annotations

import pytest

from musaeus.config import MusicConfig
from musaeus.doctor import _EXTERNAL_TOOLS, Report, _external_tools_present


@pytest.fixture
def cfg(tmp_path) -> MusicConfig:
    return MusicConfig(
        vault_root=tmp_path,
        inbox=tmp_path / "INBOX",
        staging=tmp_path / "S",
        quarantine=tmp_path / "Q",
        runs_root=tmp_path / "RUNS",
        meta_dir=tmp_path / "MetaData",
        alac_library=tmp_path / "L",
        db_path=tmp_path / "musaeus.db",
    )


def _only(rep: Report):
    found = [f for f in rep.findings if f.check == "external tools"]
    assert len(found) == 1, f"expected one finding, got {found}"
    return found[0]


class TestWhenEverythingIsInstalled:
    def test_it_reports_ok(self, cfg, monkeypatch):
        monkeypatch.setattr("musaeus.doctor.shutil.which", lambda n: f"/usr/bin/{n}")
        rep = Report()
        _external_tools_present(cfg, rep)
        assert _only(rep).level == "ok"


class TestAMissingOptionalToolIsANote:
    def test_absent_ifuse_warns_and_says_how_to_fix_it(self, cfg, monkeypatch):
        monkeypatch.setattr(
            "musaeus.doctor.shutil.which", lambda n: None if n == "ifuse" else f"/usr/bin/{n}"
        )
        rep = Report()
        _external_tools_present(cfg, rep)
        f = _only(rep)
        assert f.level == "warn"
        assert "ifuse" in f.detail
        assert "apt install ifuse" in f.detail, "a warning with no remedy is just nagging"

    def test_absent_fpcalc_names_the_right_package(self, cfg, monkeypatch):
        """fpcalc does not come from a package called fpcalc, which is exactly
        why the command belongs in the message."""
        monkeypatch.setattr(
            "musaeus.doctor.shutil.which", lambda n: None if n == "fpcalc" else f"/usr/bin/{n}"
        )
        rep = Report()
        _external_tools_present(cfg, rep)
        assert "libchromaprint-tools" in _only(rep).detail

    def test_it_counts_them(self, cfg, monkeypatch):
        absent = {"ifuse", "fpcalc", "rsync"}
        monkeypatch.setattr(
            "musaeus.doctor.shutil.which", lambda n: None if n in absent else f"/usr/bin/{n}"
        )
        rep = Report()
        _external_tools_present(cfg, rep)
        assert _only(rep).count == 3


class TestAMissingRequiredToolIsLouder:
    def test_absent_ffmpeg_is_reported_separately(self, cfg, monkeypatch):
        """ "nothing works without them" must not be buried in a list that also
        mentions an iPhone utility."""
        monkeypatch.setattr(
            "musaeus.doctor.shutil.which",
            lambda n: None if n in ("ffmpeg", "ifuse") else f"/usr/bin/{n}",
        )
        rep = Report()
        _external_tools_present(cfg, rep)
        f = _only(rep)
        assert f.level == "warn"
        assert "nothing works without them" in f.detail
        assert "ffmpeg" in f.detail
        assert "ifuse" not in f.detail, "the required message must not be diluted"


class TestTheTableItself:
    def test_every_entry_is_well_formed(self):
        for name, why, pkg, required in _EXTERNAL_TOOLS:
            assert name and why and pkg
            assert isinstance(required, bool)

    def test_ffmpeg_and_ffprobe_are_required(self):
        req = {n for n, _, _, r in _EXTERNAL_TOOLS if r}
        assert {"ffmpeg", "ffprobe"} <= req

    def test_the_iphone_tools_are_listed(self):
        """Grey asked for these by name after finding they were already
        installed by luck rather than by check."""
        names = {n for n, _, _, _ in _EXTERNAL_TOOLS}
        assert {"ifuse", "idevice_id"} <= names

    def test_the_iphone_tools_are_optional(self):
        opt = {n for n, _, _, r in _EXTERNAL_TOOLS if not r}
        assert {"ifuse", "idevice_id", "fpcalc"} <= opt
