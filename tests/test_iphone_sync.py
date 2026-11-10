"""`musaeus iphone-sync` (Grey, 2026-11-09): the iPhone steps, run by MUSAEUS.

The phone tools are replaced here; a plain folder stands in for VLC's
Documents folder. What matters: the size comes from the phone's real free
space, VLC is cleared only on a typed "yes", and nothing is cleared or copied
unless the folder really is the phone's mount.
"""

from __future__ import annotations

import math
import shutil
from pathlib import Path

import pytest

from musaeus import iphone_sync as s

FREE = 25_000_000_000


@pytest.fixture
def phone(tmp_path, monkeypatch):
    src = tmp_path / "iPHONE_Library"
    (src / "Toto" / "IV").mkdir(parents=True)
    (src / "Toto" / "IV" / "Toto - Rosanna.m4a").write_bytes(b"a" * 1000)
    (src / "Toto" / "IV" / "half.m4a.edition_tmp").write_bytes(b"x")
    mount = tmp_path / "iphone"
    mount.mkdir()
    (mount / "Old - Song.m4a").write_bytes(b"o" * 500)
    st = {"mounted": False, "unmounts": 0, "builds": [], "rsyncs": 0, "free": FREE}

    def mount_(m, udid=None):
        st["mounted"] = True
        return True

    def unmount(m):
        st["mounted"] = False
        st["unmounts"] += 1

    def rsync(a, b, genres=()):
        assert st["mounted"], "copied while the phone was not mounted"
        st["rsyncs"] += 1
        if not genres:
            shutil.copytree(
                a, b, dirs_exist_ok=True, ignore=shutil.ignore_patterns("*.edition_tmp")
            )
        for f in s._files(a, genres) if genres else ():
            (b / f.relative_to(a)).parent.mkdir(parents=True, exist_ok=True)
            shutil.copy(f, b / f.relative_to(a))
        return 0

    monkeypatch.setattr(s.shutil, "which", lambda tool: f"/usr/bin/{tool}")
    monkeypatch.setattr(s, "_attached", lambda: True)
    monkeypatch.setattr(s, "_phones", lambda: ["PHONE1"], raising=False)
    monkeypatch.setattr(s, "_paired", lambda: True)
    monkeypatch.setattr(s, "_free_bytes", lambda: st["free"])
    monkeypatch.setattr(s, "_mount", mount_)
    monkeypatch.setattr(s, "_unmount", unmount)
    monkeypatch.setattr(s, "_build", lambda gb, genres=(): st["builds"].append((gb, genres)) or 0)
    monkeypatch.setattr(s, "_rsync", rsync)
    return src, mount, st


def test_yes_sizes_the_edition_from_the_phone_then_clears_and_copies(phone):
    src, mount, st = phone
    assert s.sync(src, mount, ask=lambda q: "yes") == 0
    assert st["builds"] == [(math.floor((FREE + 500 - s.HEADROOM) / 1e7) / 100, ())]
    assert not (mount / "Old - Song.m4a").exists(), "VLC was not cleared"
    assert (mount / "Toto" / "IV" / "Toto - Rosanna.m4a").is_file()
    assert not st["mounted"] and st["unmounts"] == 2


def test_anything_but_yes_deletes_nothing_and_does_not_rebuild(phone):
    src, mount, st = phone
    assert s.sync(src, mount, ask=lambda q: "y") == 0
    assert (mount / "Old - Song.m4a").is_file(), "deleted without a typed yes"
    assert st["builds"] == []
    assert (mount / "Toto" / "IV" / "Toto - Rosanna.m4a").is_file()


def test_without_yes_a_copy_that_does_not_fit_is_not_started(phone):
    src, mount, st = phone
    st["free"] = s.HEADROOM + 999  # the 1000-byte song does not fit
    assert s.sync(src, mount, ask=lambda q: "") == 1
    assert st["rsyncs"] == 0 and (mount / "Old - Song.m4a").is_file()
    assert not st["mounted"]


def test_a_failed_build_leaves_the_phone_alone(phone, monkeypatch):
    src, mount, st = phone
    monkeypatch.setattr(s, "_build", lambda gb, genres=(): 2)
    assert s.sync(src, mount, ask=lambda q: "yes") == 2
    assert (mount / "Old - Song.m4a").is_file() and st["rsyncs"] == 0


def test_too_little_room_is_refused_before_any_build(phone):
    src, mount, st = phone
    st["free"] = s.HEADROOM - 10_000
    assert s.sync(src, mount, ask=lambda q: "yes") == 1
    assert st["builds"] == [] and (mount / "Old - Song.m4a").is_file()


def test_a_folder_that_is_not_the_phone_is_never_cleared(phone, monkeypatch):
    """ifuse reported success but nothing is mounted: the folder is the PC's."""
    src, mount, st = phone
    monkeypatch.undo()
    calls = []
    monkeypatch.setattr(s.shutil, "which", lambda tool: f"/usr/bin/{tool}")
    monkeypatch.setattr(s, "_attached", lambda: True)
    monkeypatch.setattr(s, "_phones", lambda: ["PHONE1"], raising=False)
    monkeypatch.setattr(s, "_paired", lambda: True)
    monkeypatch.setattr(s, "_free_bytes", lambda: FREE)
    monkeypatch.setattr(s, "_build", lambda gb, genres=(): calls.append("build") or 0)
    monkeypatch.setattr(s, "_rsync", lambda a, b, genres=(): calls.append("rsync") or 0)

    class _Done:
        returncode, stdout, stderr = 0, "", ""

    monkeypatch.setattr(s, "_run", lambda cmd, timeout=60: calls.append(cmd[0]) or _Done())
    empty = mount.parent / "empty_mount"
    assert s.sync(src, empty, ask=lambda q: "yes") == 1
    assert "build" not in calls and "rsync" not in calls
    # A plain folder with files in it is refused before ifuse is even run.
    calls.clear()
    assert s._mount(mount) is False
    assert "ifuse" not in calls and (mount / "Old - Song.m4a").is_file()


def test_the_command_is_wired_into_the_cli():
    import subprocess
    import sys

    r = subprocess.run(
        [sys.executable, "-m", "musaeus", "iphone-sync", "--help"],
        capture_output=True, text=True, cwd=Path(__file__).resolve().parents[1], timeout=60,
    )  # fmt: skip
    assert r.returncode == 0 and "--mount" in r.stdout


def test_an_edition_too_big_for_the_phone_is_not_started_and_vlc_is_kept(phone):
    """CodeRabbit on #310: a build can leave the edition over its budget. VLC
    must not be cleared for a copy that cannot fit."""
    src, mount, st = phone
    st["free"] = s.HEADROOM + 1000 - 500  # after clearing: room for 1000 bytes, the edition is 1001
    (src / "Toto" / "IV" / "extra.m4a").write_bytes(b"e")
    assert s.sync(src, mount, ask=lambda q: "yes") == 1
    assert (mount / "Old - Song.m4a").is_file(), "VLC cleared for a copy that cannot fit"
    assert st["rsyncs"] == 0 and not st["mounted"]


def test_a_dead_mount_left_by_an_earlier_run_is_unmounted_first(tmp_path, monkeypatch, capsys):
    """11-09: a failed unmount left ifuse running; once the phone locked, the
    folder gave an I/O error and iphone-sync crashed with a traceback."""
    mount = tmp_path / "iphone"
    dead = {"on": True}
    calls = []
    real_mkdir = Path.mkdir

    def mkdir(self, *a, **k):
        if self == mount and dead["on"]:
            raise OSError(5, "Input/output error")
        return real_mkdir(self, *a, **k)

    def run(cmd, timeout=60):
        calls.append(cmd[0])
        if cmd[0] == "fusermount":
            dead["on"] = False

        class _Done:
            returncode, stdout, stderr = 0, "", ""

        return _Done()

    monkeypatch.setattr(Path, "mkdir", mkdir)
    monkeypatch.setattr(s, "_run", run)
    s._mount(mount)
    assert calls[:2] == ["fusermount", "ifuse"]

    # Still stuck after the unmount: a plain message, no traceback.
    calls.clear()
    dead["on"] = True
    monkeypatch.setattr(s, "_run", lambda cmd, timeout=60: calls.append(cmd[0]) or _Fail())
    assert s._mount(mount) is False
    assert "ifuse" not in calls
    assert f"fusermount -u {mount}" in capsys.readouterr().out


class _Fail:
    returncode, stdout, stderr = 1, "", "Device or resource busy"


def test_a_failed_unmount_is_reported(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(s, "_run", lambda cmd, timeout=60: _Fail())
    monkeypatch.setattr(s.os.path, "ismount", lambda p: True)
    s._unmount(tmp_path)
    out = capsys.readouterr().out
    assert "Device or resource busy" in out and "fusermount -u" in out


def test_only_a_dead_mount_is_unmounted(tmp_path, monkeypatch):
    """CodeRabbit CLI: a permission error is not a dead mount; never unmount for it."""
    mount = tmp_path / "iphone"
    calls = []

    def mkdir(self, *a, **k):
        raise PermissionError(13, "Permission denied")

    monkeypatch.setattr(Path, "mkdir", mkdir)
    monkeypatch.setattr(s, "_run", lambda cmd, timeout=60: calls.append(cmd[0]) or _Fail())
    with pytest.raises(PermissionError):
        s._mount(mount)
    assert calls == []


def test_an_unmount_that_hangs_is_reported_not_raised(tmp_path, monkeypatch, capsys):
    def hang(cmd, timeout=60):
        raise s.subprocess.TimeoutExpired(cmd, timeout)

    monkeypatch.setattr(s, "_run", hang)
    s._unmount(tmp_path)
    assert "Could not run fusermount" in capsys.readouterr().out


def test_the_copy_does_not_ask_the_phone_to_set_permissions(tmp_path, monkeypatch):
    """11-09: rsync -a failed every file on ifuse (mkstemp ... Function not
    implemented (38)): -p chmods the temp file and ifuse has no chmod."""
    seen = []
    monkeypatch.setattr(s.subprocess, "run", lambda cmd, *a, **k: seen.append(cmd) or _Fail())
    s._rsync(tmp_path / "src", tmp_path / "mnt")
    flags = [f for f in seen[0] if f.startswith("-") and not f.startswith("--")]
    assert flags == ["-rt"]
    assert not {"--perms", "--archive", "-p"} & set(seen[0])


def _two_genres(src):
    """Toto is Rock (in Rock.m3u8); ABBA is Pop (in Pop.m3u8)."""
    (src / "ABBA" / "Gold").mkdir(parents=True)
    (src / "ABBA" / "Gold" / "ABBA - SOS.m4a").write_bytes(b"p" * 700)
    (src / "Playlists").mkdir()
    (src / "Playlists" / "Rock.m3u8").write_text(
        "#EXTM3U\n#EXTINF:-1,Toto - Rosanna\n../Toto/IV/Toto - Rosanna.m4a\n", encoding="utf-8"
    )
    (src / "Playlists" / "Pop.m3u8").write_text(
        "#EXTM3U\n../ABBA/Gold/ABBA - SOS.m4a\n", encoding="utf-8"
    )


def test_a_genre_pick_with_yes_replaces_vlc_with_that_genre_only(phone):
    """Grey 11-10: choose the genres; yes replaces what VLC holds."""
    src, mount, st = phone
    _two_genres(src)
    assert s.sync(src, mount, ask=lambda q: "yes", genres=("Rock",)) == 0
    assert st["builds"][0][1] == ("Rock",), "the build was not told the genre"
    assert not (mount / "Old - Song.m4a").exists(), "VLC was not cleared"
    assert (mount / "Toto" / "IV" / "Toto - Rosanna.m4a").is_file()
    assert (mount / "Playlists" / "Rock.m3u8").is_file()
    assert not (mount / "ABBA").exists(), "a song of another genre went on the phone"
    assert not (mount / "Playlists" / "Pop.m3u8").exists()


def test_a_genre_with_no_songs_in_the_edition_leaves_the_phone_alone(phone):
    src, mount, st = phone
    _two_genres(src)
    assert s.sync(src, mount, ask=lambda q: "no", genres=("Jazz",)) == 1
    assert st["rsyncs"] == 0 and (mount / "Old - Song.m4a").is_file()


def test_the_fit_check_counts_only_the_picked_genre(phone):
    src, mount, st = phone
    _two_genres(src)
    st["free"] = s.HEADROOM + 1500  # Toto (1000) fits; Toto + ABBA (1700) would not
    assert s.sync(src, mount, ask=lambda q: "no", genres=("Rock",)) == 0
    assert (mount / "Toto" / "IV" / "Toto - Rosanna.m4a").is_file()


@pytest.mark.skipif(shutil.which("rsync") is None, reason="needs rsync")
def test_real_rsync_copies_only_the_listed_genre(tmp_path):
    """The real rsync, not a stand-in: 11-09 a stand-in hid that -a fails on ifuse."""
    src, dest = tmp_path / "lib", tmp_path / "phone"
    (src / "Toto" / "IV").mkdir(parents=True)
    (src / "Toto" / "IV" / "Toto - Rosanna.m4a").write_bytes(b"a" * 1000)
    dest.mkdir()
    _two_genres(src)
    # Fable on #315: rsync takes a --files-from line starting with "#" as a comment.
    (src / "#1 Hits" / "A").mkdir(parents=True)
    (src / "#1 Hits" / "A" / "s.m4a").write_bytes(b"h")
    with (src / "Playlists" / "Rock.m3u8").open("a", encoding="utf-8") as f:
        f.write("../#1 Hits/A/s.m4a\n")
    assert s._rsync(src, dest, ("Rock",)) == 0
    got = sorted(p.relative_to(dest).as_posix() for p in dest.rglob("*") if p.is_file())
    assert got == ["#1 Hits/A/s.m4a", "Playlists/Rock.m3u8", "Toto/IV/Toto - Rosanna.m4a"]


def test_genre_counts_reads_the_catalogue(tmp_path):
    import sqlite3

    db = tmp_path / "m.db"
    c = sqlite3.connect(db)
    c.execute("CREATE TABLE archive (genre TEXT, status TEXT)")
    c.executemany(
        "INSERT INTO archive VALUES (?, ?)",
        [("Rock", "CATALOGUED"), ("Rock ", "CATALOGUED"), ("Pop", "CATALOGUED"),
         ("Jazz", "QUARANTINED"), ("", "CATALOGUED"), (None, "CATALOGUED")],
    )  # fmt: skip
    c.commit()
    c.close()
    assert s.genre_counts(db) == {"Rock": 2, "Pop": 1}


def test_a_two_genre_tag_counts_as_its_first_genre(tmp_path):
    """CodeRabbit on #315: "Pop, Rock" is listed in Pop.m3u8 (its first genre),
    so the menu must offer Pop for it, not a "Pop, Rock" no playlist holds."""
    import sqlite3

    db = tmp_path / "m.db"
    c = sqlite3.connect(db)
    c.execute("CREATE TABLE archive (genre TEXT, status TEXT)")
    c.executemany("INSERT INTO archive VALUES (?, 'CATALOGUED')", [("Pop, Rock",), ("Pop",)])
    c.commit()
    c.close()
    assert s.genre_counts(db) == {"Pop": 2}


def _musaeus(tmp_path, *args):
    import os
    import subprocess
    import sys

    env = {
        **os.environ,
        "HOME": str(tmp_path),  # never the real ~/.config/musaeus
        "MUSAEUS_VAULT_ROOT": str(tmp_path),
        "MUSAEUS_DB_PATH": str(tmp_path / "m.db"),
    }
    return subprocess.run(
        [sys.executable, "-m", "musaeus", *args], capture_output=True, text=True, env=env,
        stdin=subprocess.DEVNULL, cwd=Path(__file__).resolve().parents[1], timeout=120,
    )  # fmt: skip


def test_an_unknown_genre_is_refused_before_the_phone_is_touched(tmp_path):
    import sqlite3

    c = sqlite3.connect(tmp_path / "m.db")
    c.execute("CREATE TABLE archive (genre TEXT, status TEXT)")
    c.execute("INSERT INTO archive VALUES ('Rock', 'CATALOGUED')")
    c.commit()
    c.close()
    r = _musaeus(tmp_path, "iphone-sync", "--genre", "Rokc")
    assert r.returncode == 2 and "no such genre: Rokc" in r.stderr and "Rock" in r.stderr


def test_edition_build_refuses_a_genre_without_a_budget(tmp_path):
    r = _musaeus(tmp_path, "edition-build", "car", "--genre", "Rock")
    assert r.returncode == 2 and "--genre needs --budget-gb" in r.stderr


def test_without_a_genre_iphone_sync_does_not_read_the_catalogue(tmp_path, monkeypatch):
    """CodeRabbit CLI: the genre check must not make a plain sync need the DB."""
    import sys

    import musaeus.cli as cli

    seen = []
    monkeypatch.setattr(s, "genre_counts", lambda db: seen.append(db) or {})
    monkeypatch.setattr(s, "sync", lambda src, mount, genres=(): 0)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("MUSAEUS_VAULT_ROOT", str(tmp_path))
    monkeypatch.setattr(sys, "argv", ["musaeus", "iphone-sync"])
    with pytest.raises(SystemExit) as e:
        cli.main()
    assert e.value.code == 0 and seen == []


def test_a_picked_genre_with_no_songs_is_named(phone, capsys):
    """Fable on #315: Rock + Jazz with no Jazz song copied Rock and said nothing."""
    src, mount, st = phone
    _two_genres(src)
    assert s.sync(src, mount, ask=lambda q: "no", genres=("Rock", "Jazz")) == 0
    assert "No Jazz song is in the iPhone edition." in capsys.readouterr().out
    assert (mount / "Toto" / "IV" / "Toto - Rosanna.m4a").is_file()


def _flaky(phone, monkeypatch, codes):
    """rsync returns *codes* in turn; the phone fixture's mount/unmount count."""
    src, mount, st = phone
    tries = []

    def rsync(a, b, genres=()):
        assert st["mounted"], "copied while the phone was not mounted"
        tries.append(st["mounted"])
        return codes[len(tries) - 1]

    monkeypatch.setattr(s, "_rsync", rsync)
    monkeypatch.setattr("time.sleep", lambda secs: None)
    return src, mount, st, tries


def test_a_dropped_link_is_reconnected_and_the_copy_carries_on(phone, monkeypatch, capsys):
    """11-09 and 11-10: the phone dropped mid-copy twice; the sync just stopped."""
    src, mount, st, tries = _flaky(phone, monkeypatch, [11, 23, 0])
    assert s.sync(src, mount, ask=lambda q: "no") == 0
    assert len(tries) == 3
    out = capsys.readouterr().out
    assert "Reconnecting (2 of 3)" in out and "Done:" in out


def test_ctrl_c_is_not_retried(phone, monkeypatch):
    src, mount, st, tries = _flaky(phone, monkeypatch, [20, 0])
    assert s.sync(src, mount, ask=lambda q: "no") == 20
    assert len(tries) == 1 and not st["mounted"]


def test_it_gives_up_after_the_last_reconnect(phone, monkeypatch, capsys):
    src, mount, st, tries = _flaky(phone, monkeypatch, [11] * 9)
    assert s.sync(src, mount, ask=lambda q: "no") == 11
    assert len(tries) == 1 + s.RECONNECTS and not st["mounted"]
    assert "Run again and type no" in capsys.readouterr().out


def test_a_phone_that_does_not_come_back_stops_the_retries(phone, monkeypatch, capsys):
    src, mount, st, tries = _flaky(phone, monkeypatch, [11, 0])
    phones = iter([["PHONE1"], []])  # there at the start, gone after the drop
    monkeypatch.setattr(s, "_phones", lambda: next(phones))
    assert s.sync(src, mount, ask=lambda q: "no") == 11
    assert len(tries) == 1
    assert "The phone is not back" in capsys.readouterr().out


def test_a_reconnect_never_fills_another_phone(phone, monkeypatch, capsys):
    """CodeRabbit CLI: after a drop, ifuse took the first phone on the cable,
    which could be another one."""
    src, mount, st, tries = _flaky(phone, monkeypatch, [11, 0])
    seen = iter([["PHONE1"], ["PHONE2"]])
    monkeypatch.setattr(s, "_phones", lambda: next(seen), raising=False)
    assert s.sync(src, mount, ask=lambda q: "no") == 11
    assert len(tries) == 1 and "The phone is not back" in capsys.readouterr().out


def test_two_phones_on_the_cable_are_refused(phone, monkeypatch, capsys):
    src, mount, st = phone
    monkeypatch.setattr(s, "_phones", lambda: ["PHONE1", "PHONE2"], raising=False)
    assert s.sync(src, mount, ask=lambda q: "yes") == 1
    assert st["builds"] == [] and (mount / "Old - Song.m4a").is_file()
    assert "More than one phone" in capsys.readouterr().out


def test_ifuse_is_told_which_phone(tmp_path, monkeypatch):
    calls = []

    class _Done:
        returncode, stdout, stderr = 0, "", ""

    monkeypatch.setattr(s, "_run", lambda cmd, timeout=60: calls.append(cmd) or _Done())
    s._mount(tmp_path / "iphone", "PHONE1")
    assert calls[0][:3] == ["ifuse", "-u", "PHONE1"]
