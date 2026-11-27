"""strip_track.py removes the track number from a file's tags, and proves it.

Grey, 2026-11-27: "Strip them. and have tagger stop writting them back".

Every test here generates its own audio in tmp_path. No test reads or writes a
real music file, and every test that runs the CLI gives it its own vault under
tmp_path, because a --apply run places the masters lock there.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
from mutagen.mp4 import MP4

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import strip_track as st  # noqa: E402

pytestmark = pytest.mark.skipif(not shutil.which("ffmpeg"), reason="ffmpeg not available")


def _make_m4a(path: Path, *, track: int | None = None, title: str = "Rosanna") -> None:
    cmd = ["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "sine=d=1", "-c:a", "alac"]
    cmd += ["-metadata", f"title={title}"]
    if track is not None:
        cmd += ["-metadata", f"track={track}"]
    cmd += [str(path)]
    subprocess.run(cmd, check=True, capture_output=True)


def _audio_md5(path: Path) -> str:
    r = subprocess.run(
        ["ffmpeg", "-v", "error", "-i", str(path), "-map", "0:a:0", "-c", "copy", "-f", "md5", "-"],
        capture_output=True,
        text=True,
        check=True,
    )
    return r.stdout.strip()


def _vault(tmp_path: Path, monkeypatch) -> Path:
    """Own vault + runs root, so no test can reach the real one."""
    monkeypatch.setenv("MUSAEUS_VAULT_ROOT", str(tmp_path / "vault"))
    monkeypatch.setenv("MUSAEUS_RUNS_ROOT", str(tmp_path / "RUNS"))
    return tmp_path / "vault"


def test_the_track_number_goes_and_the_audio_does_not(tmp_path):
    song = tmp_path / "song.m4a"
    _make_m4a(song, track=5)
    before = _audio_md5(song)

    assert st.strip_one(song, apply=True) is True

    tags = MP4(str(song)).tags
    assert "trkn" not in tags, "the track number is still there"
    assert tags["\xa9nam"] == ["Rosanna"], "the write changed more than the track number"
    assert _audio_md5(song) == before, "the audio bytes changed"


def test_the_disc_number_and_the_comment_are_left_alone(tmp_path):
    """Nine of the 7,975 masters carry a disc number, and 2,229 carry a
    comment (5,091 edition copies lost a comment their master still has) --
    neither is junk."""
    song = tmp_path / "song.m4a"
    _make_m4a(song, track=5)
    audio = MP4(str(song))
    audio.tags["disk"] = [(1, 2)]
    audio.tags["\xa9cmt"] = ["Keep me"]
    audio.save()

    assert st.strip_one(song, apply=True) is True

    tags = MP4(str(song)).tags
    assert "trkn" not in tags
    assert tags["disk"] == [(1, 2)]
    assert tags["\xa9cmt"] == ["Keep me"]


def test_a_dry_run_writes_nothing(tmp_path):
    song = tmp_path / "song.m4a"
    _make_m4a(song, track=7)
    before = song.stat().st_mtime_ns

    assert st.strip_one(song, apply=False) is True, "a dry run must still report the file"

    assert song.stat().st_mtime_ns == before, "a dry run rewrote the file"
    assert MP4(str(song)).tags["trkn"] == [(7, 0)]


def test_a_file_with_no_track_number_is_not_rewritten(tmp_path):
    song = tmp_path / "song.m4a"
    _make_m4a(song)
    before = song.stat().st_mtime_ns

    assert st.strip_one(song, apply=True) is False

    assert song.stat().st_mtime_ns == before, "an already-clean file was rewritten"


def test_undo_puts_the_number_back(tmp_path):
    song = tmp_path / "song.m4a"
    _make_m4a(song, track=361)
    journal = tmp_path / "journal.jsonl"
    journal.write_text(
        json.dumps({"path": str(song), "track": "361", "when": "2026-11-27T00:00:00+00:00"}) + "\n",
        encoding="utf-8",
    )
    assert st.strip_one(song, apply=True) is True
    assert "trkn" not in MP4(str(song)).tags

    counts = st.undo(journal)

    assert counts["restored"] == 1
    assert MP4(str(song)).tags["trkn"] == [(361, 0)]


def test_the_cli_refuses_a_locked_archive_before_touching_anything(tmp_path, monkeypatch, capsys):
    """The ARCHIVE is chattr +i on purpose. The run must say how many files
    are locked and change nothing -- not stop on file one of 7,975."""
    root = _vault(tmp_path, monkeypatch)
    song = root / "song.m4a"
    song.parent.mkdir(parents=True)
    _make_m4a(song, track=3)
    before = song.stat().st_mtime_ns

    monkeypatch.setattr("musaeus.immutability.is_immutable", lambda p: True)
    monkeypatch.setattr(sys, "argv", ["strip_track.py", "--root", str(root), "--apply"])

    assert st.main() == 2
    err = capsys.readouterr().err
    assert "NOT RUN" in err and "immutable" in err
    assert "sudo chattr -R -i" in err, "the run did not say how to unlock it"
    assert song.stat().st_mtime_ns == before, "a refused run still wrote"
    assert MP4(str(song)).tags["trkn"] == [(3, 0)]


def test_the_cli_apply_then_undo_end_to_end(tmp_path, monkeypatch, capsys):
    root = _vault(tmp_path, monkeypatch)
    numbered = root / "album" / "one.m4a"
    clean = root / "album" / "two.m4a"
    numbered.parent.mkdir(parents=True)
    _make_m4a(numbered, track=11)
    _make_m4a(clean)
    before = _audio_md5(numbered)
    journal = tmp_path / "journal.jsonl"

    monkeypatch.setattr(
        sys,
        "argv",
        [
            "strip_track.py",
            "--root",
            str(root),
            "--apply",
            "--journal",
            str(journal),
        ],
    )
    assert st.main() == 0
    out = capsys.readouterr().out
    assert "Audio files: 2" in out
    assert "With a track number: 1" in out
    assert "Track numbers removed: 1" in out
    assert "Not removed" not in out
    assert "trkn" not in MP4(str(numbered)).tags
    assert _audio_md5(numbered) == before
    assert [json.loads(line)["track"] for line in journal.read_text().splitlines()] == ["11"]

    monkeypatch.setattr(sys, "argv", ["strip_track.py", "--undo", str(journal)])
    assert st.main() == 0
    assert MP4(str(numbered)).tags["trkn"] == [(11, 0)]


def test_the_cli_dry_run_needs_no_apply(tmp_path, monkeypatch, capsys):
    root = _vault(tmp_path, monkeypatch)
    song = root / "song.m4a"
    song.parent.mkdir(parents=True)
    _make_m4a(song, track=2)
    before = song.stat().st_mtime_ns

    monkeypatch.setattr(sys, "argv", ["strip_track.py", "--root", str(root)])

    assert st.main() == 0
    assert "DRY RUN" in capsys.readouterr().out
    assert song.stat().st_mtime_ns == before


def test_a_file_that_cannot_be_read_is_not_called_clean(tmp_path):
    """ "No track number" and "could not be read" are different answers.

    A corrupt file used to be read as None, counted as clean, and the run then
    reported a library it never examined. Grey's rule: say what was not
    checked.
    """
    bad = tmp_path / "broken.m4a"
    bad.write_bytes(b"this is not an mp4")

    with pytest.raises(st.Unreadable):
        st.read_track(bad)


def test_the_cli_counts_an_unreadable_file_and_keeps_going(tmp_path, monkeypatch, capsys):
    root = _vault(tmp_path, monkeypatch)
    good = root / "good.m4a"
    root.mkdir(parents=True)
    _make_m4a(good, track=4)
    (root / "broken.m4a").write_bytes(b"this is not an mp4")
    before = good.stat().st_mtime_ns

    monkeypatch.setattr(sys, "argv", ["strip_track.py", "--root", str(root)])

    assert st.main() == 0, "one unreadable file must not stop the run"
    out = capsys.readouterr().out
    assert "With a track number: 1" in out
    assert "Unreadable: 1" in out and "NOT examined" in out
    assert "broken.m4a" in out
    assert good.stat().st_mtime_ns == before, "a dry run rewrote a readable file"


REPO = Path(__file__).resolve().parents[1]


def _cli(root: Path, tmp_path: Path, *args: str) -> subprocess.CompletedProcess[str]:
    """Run the script as a real process: a batch run takes the masters lock,
    and the lock belongs to the process, not to this test."""
    env = {k: v for k, v in os.environ.items() if not k.startswith("MUSAEUS_")}
    env.update(
        {
            "MUSAEUS_VAULT_ROOT": str(tmp_path / "vault"),
            "MUSAEUS_RUNS_ROOT": str(tmp_path / "RUNS"),
            "PYTHONPATH": str(REPO),
        }
    )
    return subprocess.run(
        [sys.executable, str(REPO / "scripts" / "strip_track.py"), "--root", str(root), *args],
        capture_output=True,
        text=True,
        env=env,
        timeout=180,
        check=False,
    )


def test_a_batch_stops_at_the_limit_and_the_next_run_carries_on(tmp_path):
    """Grey, 2026-11-27: "20 files first, fix any error in the code, run
    another 20 files, and repeat, then 100 files, same process".

    A batch must stop at its limit, say what is left, and leave the rest
    readable by the next run -- not silently stop at a file count.
    """
    root = tmp_path / "vault"
    root.mkdir(parents=True)
    songs = []
    for i in range(1, 6):
        p = root / f"song{i}.m4a"
        _make_m4a(p, track=i)
        songs.append(p)

    first = _cli(
        root, tmp_path, "--apply", "--max-strip", "2", "--journal", str(tmp_path / "j1.jsonl")
    )
    assert first.returncode == 0, first.stderr[-400:]
    assert "Track numbers removed: 2" in first.stdout
    assert "Still numbered after this run: 3" in first.stdout
    assert "Batch limit reached (2)" in first.stdout
    left = [p for p in songs if MP4(str(p)).tags.get("trkn")]
    assert len(left) == 3, "the batch removed more or fewer than its limit"

    second = _cli(
        root, tmp_path, "--apply", "--max-strip", "2", "--journal", str(tmp_path / "j2.jsonl")
    )
    assert second.returncode == 0, second.stderr[-400:]
    assert "Track numbers removed: 2" in second.stdout
    assert "Still numbered after this run: 1" in second.stdout
    assert len([p for p in songs if MP4(str(p)).tags.get("trkn")]) == 1

    third = _cli(
        root, tmp_path, "--apply", "--max-strip", "2", "--journal", str(tmp_path / "j3.jsonl")
    )
    assert third.returncode == 0, third.stderr[-400:]
    assert "Track numbers removed: 1" in third.stdout
    assert "Still numbered after this run: 0" in third.stdout
    assert not [p for p in songs if MP4(str(p)).tags.get("trkn")]

    # Every batch keeps its own journal, so any batch can be undone alone.
    for name, count in (("j1.jsonl", 2), ("j2.jsonl", 2), ("j3.jsonl", 1)):
        lines = (tmp_path / name).read_text().splitlines()
        assert len(lines) == count, f"{name} recorded {len(lines)} entries, not {count}"


def _make_mp3(path: Path, *, track: int | None = 5, title: str = "Rosanna") -> bool:
    """An MP3 with an ID3v2.3 tag, or False when this ffmpeg cannot make one."""
    cmd = ["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "sine=d=0.05", "-c:a", "libmp3lame"]
    cmd += ["-metadata", f"title={title}"]
    if track is not None:
        cmd += ["-metadata", f"track={track}"]
    cmd += [str(path)]
    r = subprocess.run(cmd, capture_output=True)
    return r.returncode == 0


def _frames(path: Path) -> set[str]:
    from mutagen.id3 import ID3

    major = open(path, "rb").read(5)[3]  # noqa: SIM115 - five bytes, then closed
    return {f.split(":")[0] for f in ID3(str(path), translate=False, v2_version=major)}


#: Frames that exist only in ID3v2.3. Reading a v2.3 file as v2.4 discards
#: them, so a save -- even one that only removed the track number -- commits
#: the loss (CodeRabbit on #340, 2026-11-27).
V23_ONLY = "RVAD"


def test_an_mp3_keeps_the_frames_its_own_version_defines(tmp_path):
    """A v2.3 MP3 keeps its RVAD frame when only the track number is removed.

    Measured with mutagen 1.46.0: EasyID3 read the v2.3 file as v2.4, dropped
    RVAD, and saved the loss. The journal only records the track number, so
    the frame would have been gone for good.
    """
    song = tmp_path / "song.mp3"
    if not _make_mp3(song):
        pytest.skip("this ffmpeg cannot encode MP3")

    from mutagen.id3 import ID3, RVAD, TXXX

    tag = ID3(str(song))
    tag.add(RVAD(desc="mix", channel=1, gain=1.5, peak=0.5))
    tag.add(TXXX(encoding=3, desc="KEEP-ME", text=["x"]))
    tag.save(v2_version=3)
    assert V23_ONLY in _frames(song), "the fixture did not keep its v2.3 frame"
    header_before = open(song, "rb").read(5)[3]  # noqa: SIM115 - five bytes

    assert st.strip_one(song, apply=True) is True

    assert "TRCK" not in _frames(song), "the track number is still there"
    assert V23_ONLY in _frames(song), "removing the track number dropped a v2.3 frame"
    assert open(song, "rb").read(5)[3] == header_before, "the ID3 version was rewritten"  # noqa: SIM115
    assert any("KEEP-ME" in f for f in _ID3_KEYS(song)), "an unrelated frame was lost"


def _ID3_KEYS(path: Path) -> set[str]:
    from mutagen.id3 import ID3

    major = open(path, "rb").read(5)[3]  # noqa: SIM115 - five bytes
    return set(ID3(str(path), translate=False, v2_version=major))


def test_undo_puts_the_number_back_without_dropping_v23_frames(tmp_path):
    song = tmp_path / "song.mp3"
    if not _make_mp3(song):
        pytest.skip("this ffmpeg cannot encode MP3")

    from mutagen.id3 import ID3, RVAD

    tag = ID3(str(song))
    tag.add(RVAD(desc="mix", channel=1, gain=1.5, peak=0.5))
    tag.save(v2_version=3)

    journal = tmp_path / "journal.jsonl"
    journal.write_text(
        json.dumps({"path": str(song), "track": "361", "when": "2026-11-27T00:00:00+00:00"}) + "\n",
        encoding="utf-8",
    )
    assert st.strip_one(song, apply=True) is True
    counts = st.undo(journal)

    assert counts["restored"] == 1
    assert V23_ONLY in _frames(song), "undo dropped a frame the file had"
    assert "TRCK" in _frames(song)


def test_a_zero_limit_selects_no_files(tmp_path):
    """--limit 0 appended one file before checking the limit, so an apply run
    could strip a file it was told not to touch (CodeRabbit on #340)."""
    (tmp_path / "a.m4a").write_bytes(b"")
    (tmp_path / "b.m4a").write_bytes(b"")

    found, _ = st.scan(tmp_path, 0)

    assert found == []


def test_the_cli_refuses_a_zero_limit(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["strip_track.py", "--limit", "0"])

    with pytest.raises(SystemExit) as exc:
        st.main()

    assert exc.value.code == 2
    assert "1 or more" in capsys.readouterr().err


def test_the_journal_records_an_absolute_path(tmp_path):
    """`--root music` recorded "music/song.m4a"; undo from another directory
    then looked for a different file (CodeRabbit on #340)."""
    vault = tmp_path / "vault"
    vault.mkdir()
    if not _make_mp3(vault / "song.mp3"):
        pytest.skip("this ffmpeg cannot encode MP3")
    journal = tmp_path / "journal.jsonl"

    env = {k: v for k, v in os.environ.items() if not k.startswith("MUSAEUS_")}
    env.update(
        {
            "MUSAEUS_VAULT_ROOT": str(tmp_path / "vault"),
            "MUSAEUS_RUNS_ROOT": str(tmp_path / "RUNS"),
            "PYTHONPATH": str(REPO),
        }
    )
    r = subprocess.run(
        [
            sys.executable,
            str(REPO / "scripts" / "strip_track.py"),
            "--root",
            "vault",  # relative, and the run happens in tmp_path
            "--apply",
            "--journal",
            str(journal),
        ],
        capture_output=True,
        text=True,
        cwd=tmp_path,
        env=env,
        timeout=180,
        check=False,
    )
    assert r.returncode == 0, r.stderr[-400:]

    recorded = json.loads(journal.read_text().splitlines()[0])["path"]
    assert Path(recorded).is_absolute(), f"the journal recorded {recorded!r}"
    assert Path(recorded) == (tmp_path / "vault" / "song.mp3").resolve()


def test_the_journal_is_synced_to_disk_before_the_file_is_touched(tmp_path, monkeypatch):
    """flush() writes to the OS, not to the disk. A crash could keep the
    stripped sound and lose the only record of the number."""
    seen: list[str] = []
    real_fsync = os.fsync

    def recorder(fd: int) -> None:
        seen.append("fsync")
        real_fsync(fd)

    monkeypatch.setattr(st.os, "fsync", recorder)
    song = tmp_path / "song.mp3"
    song.write_bytes(b"x")
    journal = tmp_path / "journal.jsonl"

    with open(journal, "w", encoding="utf-8") as fh:
        st.journal(fh, song, "5")

    assert seen == ["fsync"], "the journal line was not fsynced"
    assert json.loads(journal.read_text().splitlines()[0])["track"] == "5"


def test_undo_refuses_a_path_that_became_a_symlink(tmp_path):
    """is_file() follows a symlink, and write_beside would then replace the
    link with a rewritten copy of whatever it pointed at."""
    real = tmp_path / "real.mp3"
    if not _make_mp3(real, track=9):
        pytest.skip("this ffmpeg cannot encode MP3")
    link = tmp_path / "link.mp3"
    link.symlink_to(real)
    before = real.stat().st_mtime_ns
    journal = tmp_path / "journal.jsonl"
    journal.write_text(
        json.dumps({"path": str(link), "track": "9", "when": "2026-11-27T00:00:00+00:00"}) + "\n",
        encoding="utf-8",
    )

    counts = st.undo(journal)

    assert counts["restored"] == 0
    assert counts["symlink -- refused"] == 1
    assert link.is_symlink(), "undo replaced the symlink"
    assert real.stat().st_mtime_ns == before, "undo wrote through the symlink"


def test_undo_carries_on_after_a_missing_file(tmp_path):
    """counts["missing"]["path"] += 1 raised TypeError on the zero Counter
    value, so the first deleted file aborted the whole undo."""
    song = tmp_path / "song.mp3"
    if not _make_mp3(song, track=4):
        pytest.skip("this ffmpeg cannot encode MP3")
    assert st.strip_one(song, apply=True) is True
    gone = tmp_path / "gone.mp3"
    journal = tmp_path / "journal.jsonl"
    journal.write_text(
        json.dumps({"path": str(gone), "track": "1", "when": "2026-11-27T00:00:00+00:00"})
        + "\n"
        + json.dumps({"path": str(song), "track": "4", "when": "2026-11-27T00:00:00+00:00"})
        + "\n",
        encoding="utf-8",
    )

    counts = st.undo(journal)

    assert counts["missing"] == 1
    assert counts["restored"] == 1, "one missing file stopped the rest of the undo"
    assert "TRCK" in _frames(song)


def test_an_apply_run_with_no_batch_size_stops_at_twenty(tmp_path):
    """Grey, 2026-11-27: batches of 20 first. Without default=20 an apply run
    removed every track number it found -- all 1,402 at once."""
    root = tmp_path / "vault"
    root.mkdir(parents=True)
    songs = [root / f"s{i:02d}.m4a" for i in range(1, 22)]
    for i, song in enumerate(songs, start=1):
        _make_m4a(song, track=i)

    r = _cli(root, tmp_path, "--apply", "--journal", str(tmp_path / "j.jsonl"))

    assert r.returncode == 0, r.stderr[-400:]
    assert "Track numbers removed: 20" in r.stdout, r.stdout[-600:]
    assert "Still numbered after this run: 1" in r.stdout
    assert "Batch limit reached (20)" in r.stdout
    assert len([s for s in songs if MP4(str(s)).tags.get("trkn")]) == 1


def test_undo_takes_the_masters_lock(tmp_path):
    """Undo rewrites masters, and it returned before the lock was taken, so it
    could write while another job held the lock (CodeRabbit on #340)."""
    song = tmp_path / "song.m4a"
    _make_m4a(song, track=6)
    assert st.strip_one(song, apply=True) is True
    journal = tmp_path / "journal.jsonl"
    journal.write_text(
        json.dumps({"path": str(song), "track": "6", "when": "2026-11-27T00:00:00+00:00"}) + "\n",
        encoding="utf-8",
    )

    from musaeus.masters_lock import masters_lock

    runs = tmp_path / "RUNS"
    with masters_lock(runs, exclusive=True, what="test holder"):
        r = _cli(tmp_path, tmp_path, "--undo", str(journal))

    assert r.returncode == 75, f"undo ran while the masters were held: {r.stdout[-300:]}"
    assert "NOT RUN" in r.stderr
    assert "test holder" in r.stderr
    assert "trkn" not in MP4(str(song)).tags, "undo wrote while locked"


def test_a_dry_run_works_without_a_vault_configured(tmp_path, monkeypatch, capsys):
    """--root names the library; it does not supply a vault. A dry run needs
    no vault, so it must not be refused for the lack of one (CodeRabbit on
    #340)."""
    root = tmp_path / "music"
    root.mkdir()
    _make_m4a(root / "song.m4a", track=3)
    for key in ("MUSAEUS_VAULT_ROOT", "MUSAEUS_RUNS_ROOT", "MUSAEUS_DB_PATH"):
        monkeypatch.setenv(key, "")
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setattr(
        "musaeus.config._load_env", lambda: None
    )  # do not restore it from settings.env
    monkeypatch.setattr(sys, "argv", ["strip_track.py", "--root", str(root)])

    assert st.main() == 0
    assert "DRY RUN" in capsys.readouterr().out


def test_an_apply_without_a_vault_says_what_to_set(tmp_path, monkeypatch, capsys):
    """The old message said "Pass --root", which cannot fix a missing vault:
    the lock and the journal live under the vault's runs root."""
    root = tmp_path / "music"
    root.mkdir()
    _make_m4a(root / "song.m4a", track=3)
    for key in ("MUSAEUS_VAULT_ROOT", "MUSAEUS_RUNS_ROOT"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setattr("musaeus.config._load_env", lambda: None)
    monkeypatch.setattr(sys, "argv", ["strip_track.py", "--root", str(root), "--apply"])

    assert st.main() == 2
    err = capsys.readouterr().err
    assert "MUSAEUS_VAULT_ROOT" in err
    assert "Pass --root" not in err


def test_the_cli_refuses_to_overwrite_an_existing_journal(tmp_path):
    """Two batches sharing a --journal path: the second truncated the first
    batch's recovery records, and those files stayed stripped (CodeRabbit on
    #340)."""
    root = tmp_path / "vault"
    root.mkdir(parents=True)
    _make_m4a(root / "song.m4a", track=8)
    journal = tmp_path / "j.jsonl"
    journal.write_text('{"path": "/elsewhere/old.m4a", "track": "3"}\n', encoding="utf-8")
    before = journal.read_text()

    r = _cli(root, tmp_path, "--apply", "--journal", str(journal))

    assert r.returncode == 2, f"an existing journal was accepted: {r.stdout[-300:]}"
    assert "already exists" in r.stderr
    assert journal.read_text() == before, "the first batch's records were destroyed"
    assert "trkn" in MP4(str(root / "song.m4a")).tags
