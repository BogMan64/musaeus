"""strip_track.py removes the track number from a file's tags, and proves it.

Grey, 2026-11-27: "Strip them. and have tagger stop writting them back".

Every test here generates its own audio in tmp_path. No test reads or writes a
real music file, and every test that runs the CLI gives it its own vault under
tmp_path, because a --apply run places the masters lock there.
"""

from __future__ import annotations

import json
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
    """Zero of 401 sampled masters carry a disc number, and 5,091 edition
    copies lost a comment their master still has -- neither is junk."""
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
