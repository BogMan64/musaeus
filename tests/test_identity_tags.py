"""Identity must reach the FILE, and the write must be proven, not assumed.

Every MBID this project has fetched lived in musaeus.db alone -- 806 in a
single batch on 2026-08-26 -- while the project asserts everywhere else
that the file is the durable record.

The reason each write is read back off disk is forge.py:73, silent-no-op
#2: a dotted "com.apple.iTunes.NAME" key is accepted by mutagen's MP4 as a
dict key but cannot be serialised, so save() succeeds and writes nothing.
Not one M4A carried an R128 tag despite 12,279 FORGE_TAG events. That trap
is REPRODUCED below against the installed mutagen rather than taken on
trust -- if a future version starts raising instead, the test says so.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from musaeus.identity_tags import (
    IDENTITY_FIELDS,
    _m4a_key,
    read_identity,
    write_identity,
)

MBID = "4ef7a9e2-2cf5-483a-8616-ef7791a98026"
AID = "e1b2c3d4-0000-4444-8888-abcdefabcdef"


def _encode(path: Path, codec: str) -> bool:
    """A real encoded file -- a stub of bytes proves nothing about tagging."""
    if not shutil.which("ffmpeg"):
        return False
    r = subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi",
         "-i", "sine=frequency=440:duration=1", "-c:a", codec, str(path)],
        capture_output=True,
    )
    return r.returncode == 0 and path.exists()


@pytest.fixture
def m4a(tmp_path) -> Path:
    p = tmp_path / "probe.m4a"
    if not _encode(p, "alac"):
        pytest.skip("ffmpeg unavailable")
    return p


@pytest.fixture
def flac(tmp_path) -> Path:
    p = tmp_path / "probe.flac"
    if not _encode(p, "flac"):
        pytest.skip("ffmpeg unavailable")
    return p


class TestTheTagSurvivesTheWrite:
    def test_m4a_round_trip(self, m4a):
        ok, detail = write_identity(m4a, {"mb_artist_id": MBID, "acousticid_recording": AID})
        assert ok, detail
        got = read_identity(m4a)
        assert got["mb_artist_id"] == MBID
        assert got["acousticid_recording"] == AID

    def test_flac_round_trip(self, flac):
        ok, detail = write_identity(flac, {"mb_artist_id": MBID})
        assert ok, detail
        assert read_identity(flac)["mb_artist_id"] == MBID

    def test_a_file_with_no_identity_reads_empty(self, m4a):
        assert read_identity(m4a) == {}

    def test_writing_nothing_is_not_a_failure(self, m4a):
        ok, _ = write_identity(m4a, {})
        assert ok

    def test_an_unsupported_container_reports_failure(self, tmp_path):
        p = tmp_path / "x.wav"
        p.write_bytes(b"RIFF")
        ok, detail = write_identity(p, {"mb_artist_id": MBID})
        assert not ok
        assert "unsupported" in detail


class TestTheTrapIsStillReal:
    """Reproduces silent-no-op #2 against the installed mutagen."""

    def test_the_dotted_key_writes_nothing_and_does_not_raise(self, m4a):
        from mutagen.mp4 import MP4

        a = MP4(str(m4a))
        if a.tags is None:
            a.add_tags()
        a.tags["com.apple.iTunes.MusicBrainz Artist Id"] = [MBID]
        a.save()  # must NOT raise -- that is what made it silent

        reloaded = MP4(str(m4a))
        assert not [k for k in (reloaded.tags or {}) if "MusicBrainz" in k], (
            "mutagen now persists the dotted key -- the historical trap has "
            "changed shape; re-read forge.py:73 before trusting this module"
        )

    def test_our_freeform_key_is_not_the_dotted_form(self):
        key = _m4a_key(IDENTITY_FIELDS["mb_artist_id"])
        assert key.startswith("----:com.apple.iTunes:")
        assert not key.startswith("com.apple.iTunes.")


class TestVerificationActuallyVerifies:
    def test_a_write_that_does_not_land_is_reported_as_failure(self, m4a, monkeypatch):
        # Neuter save() so the tag never reaches disk. The writer must
        # notice by re-reading, not by trusting its own return value.
        from mutagen.mp4 import MP4

        monkeypatch.setattr(MP4, "save", lambda self, *a, **k: None)
        ok, detail = write_identity(m4a, {"mb_artist_id": MBID})
        assert not ok, "a write that never landed must not report success"
        assert "did not survive" in detail
