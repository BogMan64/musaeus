"""The real-phone check copies one song into its own folder and takes it out.

Here the "phone" is a plain folder; the point of the script is to run
against the real one (scripts/iphone_real_check.py). This guards what it
may touch: only `_musaeus_check`, never anything VLC already holds.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "iphone_real_check.py"


@pytest.fixture
def rc_mod(monkeypatch):
    spec = importlib.util.spec_from_file_location("iphone_real_check", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    monkeypatch.setattr(mod.s, "_phones", lambda: ["udid1"])
    monkeypatch.setattr(mod.s, "_mount", lambda mount, udid=None: True)
    monkeypatch.setattr(mod.s, "_unmount", lambda mount: None)
    return mod


def _edition(tmp_path: Path) -> Path:
    edition = tmp_path / "edition" / "Rock" / "A" / "B"
    edition.mkdir(parents=True)
    (edition / "big.m4a").write_bytes(b"x" * 500)
    (edition / "small.m4a").write_bytes(b"y" * 50)
    return tmp_path / "edition"


def test_one_song_goes_on_and_comes_off_and_nothing_else_is_touched(rc_mod, tmp_path, capsys):
    phone = tmp_path / "phone"
    (phone / "Rock").mkdir(parents=True)
    (phone / "Rock" / "kept.m4a").write_bytes(b"k")
    assert rc_mod.check(_edition(tmp_path), phone) == 0
    assert "passed" in capsys.readouterr().out
    assert sorted(p.relative_to(phone).as_posix() for p in phone.rglob("*")) == [
        "Rock",
        "Rock/kept.m4a",
    ]


def test_a_check_folder_left_on_the_phone_is_never_removed(rc_mod, tmp_path):
    phone = tmp_path / "phone"
    (phone / "_musaeus_check").mkdir(parents=True)
    (phone / "_musaeus_check" / "mine.m4a").write_bytes(b"m")
    assert rc_mod.check(_edition(tmp_path), phone) == 1
    assert (phone / "_musaeus_check" / "mine.m4a").is_file()


def test_a_failed_copy_fails_the_check(rc_mod, tmp_path, monkeypatch):
    monkeypatch.setattr(rc_mod.s, "_rsync", lambda src, mount, genres=(): 23)
    phone = tmp_path / "phone"
    phone.mkdir()
    assert rc_mod.check(_edition(tmp_path), phone) == 1
    assert not (phone / "_musaeus_check").exists()


def test_two_phones_are_refused(rc_mod, tmp_path, monkeypatch):
    monkeypatch.setattr(rc_mod.s, "_phones", lambda: ["a", "b"])
    assert rc_mod.check(_edition(tmp_path), tmp_path / "phone") == 1


def test_a_phone_left_mounted_fails_the_check(rc_mod, tmp_path, monkeypatch):
    """If the unmount fails the folder is not empty: rmdir refuses, and the
    check must not report success (local CodeRabbit)."""
    mount = tmp_path / "mnt"
    mount.mkdir()
    monkeypatch.setattr(rc_mod.tempfile, "mkdtemp", lambda prefix="": str(mount))
    monkeypatch.setattr(rc_mod, "check", lambda edition, m: (m / "still_there").touch() or 0)
    assert rc_mod.main() == 1
    assert (mount / "still_there").is_file()
