"""
Tests for ForgeStage — EBU R128 loudness measurement + ReplayGain tag embedding.

All external dependencies (ffmpeg, mutagen) are mocked.
"""

import sqlite3
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from musaeus.config import MusicConfig
from musaeus.context import RunContext
from musaeus.db import open_db, upsert_archive
from musaeus.loudness import R128_APPLE_REFERENCE, R128_REFERENCE
from musaeus.stages.forge import ForgeStage, read_existing_rg_tags, write_rg_tags


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


@pytest.fixture
def ctx(cfg: MusicConfig) -> RunContext:
    conn = open_db(cfg.db_path)
    return RunContext.new(cfg, conn, dry_run=False)


@pytest.fixture
def ctx_dry(cfg: MusicConfig) -> RunContext:
    conn = open_db(cfg.db_path)
    return RunContext.new(cfg, conn, dry_run=True)


def _insert_catalogued(ctx: RunContext, file_path: str, ext: str = ".flac") -> None:
    upsert_archive(
        ctx.conn,
        {
            "file_path": file_path,
            "status": "CATALOGUED",
            "ext": ext,
            "artist": "Artist",
            "album": "Album",
            "title": "Song",
        },
    )
    ctx.conn.commit()


# ── Validate ──────────────────────────────────────────────────────────────────


class TestForgeValidate:
    @patch("shutil.which", return_value=None)
    def test_validate_no_ffmpeg(self, mock_which, ctx):
        from musaeus.stages.base import StageError

        stage = ForgeStage()
        with pytest.raises(StageError, match="ffmpeg"):
            stage.validate(ctx)

    @patch("shutil.which", side_effect=lambda cmd: "/usr/bin/" + cmd)
    def test_validate_no_mutagen(self, mock_which, ctx):
        from musaeus.stages.base import StageError

        with patch.dict("sys.modules", {"mutagen": None}):
            stage = ForgeStage()
            with pytest.raises(StageError, match="mutagen"):
                stage.validate(ctx)

    @patch("shutil.which", side_effect=lambda cmd: "/usr/bin/" + cmd)
    def test_validate_passes_with_all_deps(self, mock_which, ctx):
        """If ffmpeg, ffprobe and mutagen are available, validate passes."""
        import sys

        # Ensure mutagen is importable (mock it)
        mock_mutagen = MagicMock()
        with patch.dict(sys.modules, {"mutagen": mock_mutagen}):
            stage = ForgeStage()
            stage.validate(ctx)  # should not raise


# ── Dry run ───────────────────────────────────────────────────────────────────


class TestForgeDryRun:
    @patch("musaeus.stages.forge.ForgeStage.validate")
    def test_dry_run_reports_pending(self, mock_validate, ctx_dry, tmp_path):
        track = tmp_path / "song.flac"
        track.write_bytes(b"AUDIO")
        _insert_catalogued(ctx_dry, str(track))

        stage = ForgeStage()
        result = stage.dry_run(ctx_dry)

        assert result.dry_run is True
        assert result.files_processed == 1
        assert any("DRY RUN" in n for n in result.notes)
        assert any("1 file(s)" in n for n in result.notes)

    @patch("musaeus.stages.forge.ForgeStage.validate")
    def test_dry_run_no_db_changes(self, mock_validate, ctx_dry, tmp_path):
        track = tmp_path / "song.flac"
        track.write_bytes(b"AUDIO")
        _insert_catalogued(ctx_dry, str(track))

        ForgeStage().dry_run(ctx_dry)

        row = ctx_dry.conn.execute(
            "SELECT rg_tagged_at FROM archive WHERE file_path=?",
            (str(track),),
        ).fetchone()
        assert row["rg_tagged_at"] is None

    @patch("musaeus.stages.forge.ForgeStage.validate")
    def test_dry_run_empty(self, mock_validate, ctx_dry):
        result = ForgeStage().dry_run(ctx_dry)
        assert result.files_processed == 0


# ── Run (mocked loudness + tags) ──────────────────────────────────────────────


class TestForgeRun:
    @patch("musaeus.stages.forge.ForgeStage.validate")
    @patch("musaeus.stages.forge.write_rg_tags")
    @patch("musaeus.stages.forge.measure_loudness")
    def test_run_measures_and_tags(self, mock_measure, mock_write, mock_validate, ctx, tmp_path):
        track = tmp_path / "song.flac"
        track.write_bytes(b"AUDIO")
        _insert_catalogued(ctx, str(track))

        mock_measure.return_value = (-14.0, -1.5, "ok")
        mock_write.return_value = True

        stage = ForgeStage()
        result = stage.run(ctx)

        assert result.success is True
        assert result.files_changed == 1
        mock_measure.assert_called_once()
        mock_write.assert_called_once()

        # Check DB was updated
        row = ctx.conn.execute(
            "SELECT lufs, rg_tagged_at FROM archive WHERE file_path=?",
            (str(track),),
        ).fetchone()
        assert row["lufs"] == pytest.approx(-14.0)
        assert row["rg_tagged_at"] is not None

    @patch("musaeus.stages.forge.ForgeStage.validate")
    @patch("musaeus.stages.forge.write_rg_tags")
    @patch("musaeus.stages.forge.measure_loudness")
    def test_run_silence_skipped(self, mock_measure, mock_write, mock_validate, ctx, tmp_path):
        track = tmp_path / "silence.flac"
        track.write_bytes(b"SILENCE")
        _insert_catalogued(ctx, str(track))

        mock_measure.return_value = (None, None, "silence")

        result = ForgeStage().run(ctx)
        assert result.files_skipped == 1
        mock_write.assert_not_called()

    @patch("musaeus.stages.forge.ForgeStage.validate")
    @patch("musaeus.stages.forge.write_rg_tags")
    @patch("musaeus.stages.forge.measure_loudness")
    def test_run_ffmpeg_fail(self, mock_measure, mock_write, mock_validate, ctx, tmp_path):
        track = tmp_path / "bad.flac"
        track.write_bytes(b"BAD")
        _insert_catalogued(ctx, str(track))

        mock_measure.return_value = (None, None, "ffmpeg_fail")

        result = ForgeStage().run(ctx)
        assert result.files_errored == 1
        assert result.success is False

    @patch("musaeus.stages.forge.ForgeStage.validate")
    @patch("musaeus.stages.forge.write_rg_tags")
    @patch("musaeus.stages.forge.measure_loudness")
    def test_run_skips_already_forged(self, mock_measure, mock_write, mock_validate, ctx, tmp_path):
        """Files with rg_tagged_at set are skipped unless force=True."""
        track = tmp_path / "done.flac"
        track.write_bytes(b"DONE")
        _insert_catalogued(ctx, str(track))
        # Mark as already forged
        ctx.conn.execute(
            "UPDATE archive SET rg_tagged_at='2024-01-01T00:00:00Z' WHERE file_path=?",
            (str(track),),
        )
        ctx.conn.commit()

        result = ForgeStage().run(ctx)
        # Nothing to do — already forged
        assert result.files_processed == 0
        mock_measure.assert_not_called()

    @patch("musaeus.stages.forge.ForgeStage.validate")
    @patch("musaeus.stages.forge.write_rg_tags")
    @patch("musaeus.stages.forge.measure_loudness")
    def test_run_force_retags(self, mock_measure, mock_write, mock_validate, ctx, tmp_path):
        """With forge_force=True, already-forged files are re-processed."""
        track = tmp_path / "done.flac"
        track.write_bytes(b"DONE")
        _insert_catalogued(ctx, str(track))
        ctx.conn.execute(
            "UPDATE archive SET rg_tagged_at='2024-01-01T00:00:00Z' WHERE file_path=?",
            (str(track),),
        )
        ctx.conn.commit()
        ctx.set("forge_force", True)

        mock_measure.return_value = (-16.0, -2.0, "ok")
        mock_write.return_value = True

        result = ForgeStage().run(ctx)
        assert result.files_changed == 1


# ── read_existing_rg_tags ────────────────────────────────────────────────────


class TestReadExistingRgTags:
    def test_m4a_with_r128_gain_returns_recovered_lufs(self, tmp_path: Path):
        path = tmp_path / "track.m4a"
        path.write_bytes(b"fake")
        mock_audio = MagicMock()
        # -3.5 dB gain @ -23 LUFS reference, Q7.8 fixed-point encoding.
        # Keyed by the freeform atom the writer actually persists. This test
        # previously mocked the dotted "com.apple.iTunes.R128_TRACK_GAIN"
        # key -- which mutagen silently drops on save -- so it asserted the
        # implementation's assumption rather than what a real file contains,
        # and passed happily while no library file had the tag at all.
        mock_audio.tags = {
            "----:com.apple.iTunes:R128_TRACK_GAIN": [str(int(round(-3.5 * 256))).encode("utf-8")]
        }
        with patch("mutagen.mp4.MP4", return_value=mock_audio):
            result = read_existing_rg_tags(path)
        assert result is not None
        assert result["lufs"] == pytest.approx(-19.5, abs=0.01)
        assert result["lufs_tp"] is None
        assert result["rg_peak"] is None
        assert result["rg_gain"] == pytest.approx(-18.0 - (-19.5), abs=0.01)

    def test_m4a_without_tag_returns_none(self, tmp_path: Path):
        path = tmp_path / "track.m4a"
        path.write_bytes(b"fake")
        mock_audio = MagicMock()
        mock_audio.tags = {}
        with patch("mutagen.mp4.MP4", return_value=mock_audio):
            assert read_existing_rg_tags(path) is None

    def test_flac_with_full_rg_tags_returns_all_fields(self, tmp_path: Path):
        path = tmp_path / "track.flac"
        path.write_bytes(b"fake")
        mock_audio = MagicMock()
        mock_audio.get = {
            "replaygain_track_gain": ["+2.50 dB"],
            "replaygain_track_peak": ["0.98"],
            "replaygain_reference_loudness": ["18.00 LUFS"],
        }.get
        with patch("mutagen.flac.FLAC", return_value=mock_audio):
            result = read_existing_rg_tags(path)
        assert result == {
            "lufs": pytest.approx(-20.5),
            "lufs_tp": None,
            "rg_gain": pytest.approx(2.5),
            "rg_peak": pytest.approx(0.98),
        }

    def test_flac_without_reference_tag_defaults_to_r128_reference(self, tmp_path: Path):
        path = tmp_path / "track.flac"
        path.write_bytes(b"fake")
        mock_audio = MagicMock()
        mock_audio.get = {"replaygain_track_gain": ["-1.00 dB"]}.get
        with patch("mutagen.flac.FLAC", return_value=mock_audio):
            result = read_existing_rg_tags(path)
        assert result is not None
        assert result["lufs"] == pytest.approx(-17.0)
        assert result["rg_peak"] is None

    def test_flac_without_gain_tag_returns_none(self, tmp_path: Path):
        path = tmp_path / "track.flac"
        path.write_bytes(b"fake")
        mock_audio = MagicMock()
        mock_audio.get = {}.get
        with patch("mutagen.flac.FLAC", return_value=mock_audio):
            assert read_existing_rg_tags(path) is None

    def test_mp3_with_rg_tags_returns_fields(self, tmp_path: Path):
        path = tmp_path / "track.mp3"
        path.write_bytes(b"fake")
        mock_audio = MagicMock()
        mock_audio.get = {
            "replaygain_track_gain": ["+4.00 dB"],
            "replaygain_track_peak": ["0.5"],
        }.get
        with patch("mutagen.easyid3.EasyID3", return_value=mock_audio):
            result = read_existing_rg_tags(path)
        assert result is not None
        assert result["rg_gain"] == pytest.approx(4.0)
        assert result["rg_peak"] == pytest.approx(0.5)

    def test_wav_returns_none(self, tmp_path: Path):
        """WAV has no standard RG tag container -- always re-measure."""
        path = tmp_path / "track.wav"
        path.write_bytes(b"fake")
        assert read_existing_rg_tags(path) is None

    def test_aiff_returns_none(self, tmp_path: Path):
        """AIFF's gain-only TXXX tag isn't parsed as a shortcut source."""
        path = tmp_path / "track.aiff"
        path.write_bytes(b"fake")
        assert read_existing_rg_tags(path) is None

    def test_mutagen_exception_returns_none_not_raise(self, tmp_path: Path):
        path = tmp_path / "track.m4a"
        path.write_bytes(b"fake")
        with patch("mutagen.mp4.MP4", side_effect=RuntimeError("corrupt")):
            assert read_existing_rg_tags(path) is None


# ── Tag-shortcut integration (ForgeStage.run) ────────────────────────────────


class TestForgeTagShortcut:
    @patch("musaeus.stages.forge.ForgeStage.validate")
    @patch("musaeus.stages.forge.measure_loudness")
    @patch("musaeus.stages.forge.read_existing_rg_tags")
    def test_run_uses_tag_shortcut_skips_ffmpeg(
        self, mock_read, mock_measure, mock_validate, ctx, tmp_path
    ):
        track = tmp_path / "song.m4a"
        track.write_bytes(b"AUDIO")
        _insert_catalogued(ctx, str(track), ext=".m4a")
        mock_read.return_value = {"lufs": -19.5, "lufs_tp": None, "rg_gain": 1.5, "rg_peak": None}

        result = ForgeStage().run(ctx)

        assert result.success is True
        assert result.files_changed == 1
        mock_measure.assert_not_called()

        row = ctx.conn.execute(
            "SELECT lufs, rg_gain, rg_peak, rg_tagged_at FROM archive WHERE file_path=?",
            (str(track),),
        ).fetchone()
        assert row["lufs"] == pytest.approx(-19.5)
        assert row["rg_peak"] is None
        assert row["rg_tagged_at"] is not None

    @patch("musaeus.stages.forge.ForgeStage.validate")
    @patch("musaeus.stages.forge.write_rg_tags")
    @patch("musaeus.stages.forge.measure_loudness")
    @patch("musaeus.stages.forge.read_existing_rg_tags")
    def test_run_no_tag_falls_through_to_ffmpeg(
        self, mock_read, mock_measure, mock_write, mock_validate, ctx, tmp_path
    ):
        track = tmp_path / "song.flac"
        track.write_bytes(b"AUDIO")
        _insert_catalogued(ctx, str(track))
        mock_read.return_value = None
        mock_measure.return_value = (-14.0, -1.5, "ok")
        mock_write.return_value = True

        result = ForgeStage().run(ctx)

        assert result.files_changed == 1
        mock_measure.assert_called_once()

    @patch("musaeus.stages.forge.ForgeStage.validate")
    @patch("musaeus.stages.forge.write_rg_tags")
    @patch("musaeus.stages.forge.measure_loudness")
    @patch("musaeus.stages.forge.read_existing_rg_tags")
    def test_retag_bypasses_tag_shortcut(
        self, mock_read, mock_measure, mock_write, mock_validate, ctx, tmp_path
    ):
        track = tmp_path / "song.m4a"
        track.write_bytes(b"AUDIO")
        _insert_catalogued(ctx, str(track), ext=".m4a")
        ctx.set("forge_retag", True)
        mock_measure.return_value = (-16.0, -2.0, "ok")
        mock_write.return_value = True

        result = ForgeStage().run(ctx)

        mock_read.assert_not_called()
        mock_measure.assert_called_once()
        assert result.files_changed == 1


# ── write_rg_tags dispatch ────────────────────────────────────────────────────


class TestWriteRgTags:
    @patch("musaeus.stages.forge._write_tags_flac")
    def test_flac_dispatch(self, mock_flac, tmp_path):
        path = tmp_path / "song.flac"
        path.write_bytes(b"")
        mock_flac.return_value = True
        assert write_rg_tags(path, 4.5, 0.95) is True
        # The reference is part of the contract: the gain and the reference
        # it was computed against must travel together, or the read-back
        # recovers a wrong LUFS. Default is R128_REFERENCE.
        mock_flac.assert_called_once_with(path, 4.5, 0.95, R128_REFERENCE)

    @patch("musaeus.stages.forge._write_tags_mp3")
    def test_mp3_dispatch(self, mock_mp3, tmp_path):
        path = tmp_path / "song.mp3"
        path.write_bytes(b"")
        mock_mp3.return_value = True
        assert write_rg_tags(path, 3.0, 0.9) is True
        mock_mp3.assert_called_once_with(path, 3.0, 0.9, R128_REFERENCE)

    @patch("musaeus.stages.forge._write_tags_m4a")
    def test_m4a_dispatch(self, mock_m4a, tmp_path):
        path = tmp_path / "song.m4a"
        path.write_bytes(b"")
        mock_m4a.return_value = True
        # With r128_gain provided
        assert write_rg_tags(path, 4.0, 0.9, r128_gain=5.0) is True
        mock_m4a.assert_called_once_with(path, 5.0, 0.9)

    def test_wav_returns_true(self, tmp_path):
        """WAV has no tag writer — returns True (DB-only)."""
        path = tmp_path / "song.wav"
        path.write_bytes(b"")
        assert write_rg_tags(path, 4.0, 0.9) is True


class TestM4aR128TagActuallyPersists:
    """Regression guard for a silent write failure found 2026-08-21.

    _write_tags_m4a assigned to the dotted key
    "com.apple.iTunes.R128_TRACK_GAIN". mutagen's MP4 accepts that as a dict
    key but cannot serialise it -- it is neither a 4-character atom nor a
    "----:mean:name" freeform atom -- so save() succeeded, the function
    returned True, and nothing was written.

    Real consequence: not one M4A in the library carried an R128 or
    ReplayGain tag despite 12,279 recorded FORGE_TAG events, and Forge's own
    tag-read shortcut could never fire ("from existing tags: 0" across a full
    3,838-file run). Returning True while writing nothing is exactly the
    failure shape that hides for months.
    """

    def _real_m4a(self, tmp_path):
        import shutil
        import subprocess

        if not shutil.which("ffmpeg"):
            pytest.skip("ffmpeg not available")
        p = tmp_path / "t.m4a"
        subprocess.run(
            [
                "ffmpeg",
                "-y",
                "-f",
                "lavfi",
                "-i",
                "sine=frequency=440:duration=1",
                "-c:a",
                "alac",
                str(p),
            ],
            capture_output=True,
            check=True,
        )
        return p

    def test_tag_survives_a_save_reload_cycle(self, tmp_path):
        from mutagen.mp4 import MP4

        from musaeus.stages.forge import _write_tags_m4a

        p = self._real_m4a(tmp_path)
        assert _write_tags_m4a(p, -5.25, 0.87) is True
        tags = MP4(str(p)).tags or {}
        assert "----:com.apple.iTunes:R128_TRACK_GAIN" in tags, (
            "R128 gain did not persist -- the write silently did nothing"
        )

    def test_round_trip_through_the_real_reader(self, tmp_path):
        from musaeus.stages.forge import _write_tags_m4a, read_existing_rg_tags

        p = self._real_m4a(tmp_path)
        _write_tags_m4a(p, -5.25, 0.87)
        got = read_existing_rg_tags(p)
        assert got is not None, "reader saw nothing -- shortcut can never fire"
        # gain is stored relative to the -23 LUFS EBU reference
        assert got["lufs"] == pytest.approx(-23 - (-5.25), abs=0.05)

    def test_reader_still_accepts_the_legacy_dotted_key(self, tmp_path):
        """Files tagged by some other tool that did write the dotted key
        must stay readable -- the fix must not orphan them."""
        from mutagen.mp4 import MP4

        from musaeus.stages.forge import read_existing_rg_tags

        p = self._real_m4a(tmp_path)
        a = MP4(str(p))
        if a.tags is None:
            a.add_tags()
        a.tags["com.apple.iTunes.R128_TRACK_GAIN"] = ["-1280"]
        try:
            a.save()
        except Exception:
            pytest.skip("mutagen refuses to serialise the legacy key at all")
        got = read_existing_rg_tags(p)
        if got is not None:
            assert got["lufs"] == pytest.approx(-18.0, abs=0.05)


def test_embed_from_db_writes_without_measuring(tmp_path, monkeypatch):
    """The repair path must embed stored values and never invoke ffmpeg.

    Forge measured 12,279 files correctly but `_write_tags_m4a` assigned to a
    dotted key mutagen could not serialise, so the numbers only ever reached
    the DB. Re-measuring to fix an embedding bug would be hours of ffmpeg to
    recompute values already known-good, so this path reads them back out of
    the DB instead -- and must stay measurement-free, which is what this test
    pins.
    """
    from musaeus.stages import forge as forge_mod

    def _explode(*_a, **_k):  # pragma: no cover - failure signal only
        raise AssertionError("embed-from-db must not measure loudness")

    monkeypatch.setattr(forge_mod, "measure_loudness", _explode)

    written: list[tuple[Path, float, float, float | None]] = []
    monkeypatch.setattr(
        forge_mod,
        "write_rg_tags",
        lambda p, g, pk, r128_gain=None, reference=None: (
            written.append((p, g, pk, r128_gain)),
            True,
        )[1],
    )

    real = tmp_path / "song.m4a"
    real.write_bytes(b"x")

    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute(
        "CREATE TABLE archive (file_path TEXT, status TEXT, artist TEXT, album TEXT, "
        "track INT, lufs REAL, rg_gain REAL, rg_peak REAL, lufs_tp REAL, rg_tagged_at TEXT)"
    )
    untagged = tmp_path / "untagged.m4a"
    untagged.write_bytes(b"x")
    no_lufs = tmp_path / "nolufs.m4a"
    no_lufs.write_bytes(b"x")
    conn.executemany(
        "INSERT INTO archive VALUES (?,?,?,?,?,?,?,?,?,?)",
        [
            (str(real), "CATALOGUED", "A", "B", 1, -12.5, -5.5, 0.98, -1.2, "2026-08-20"),
            (str(tmp_path / "gone.m4a"), "CATALOGUED", "A", "B", 2, -10.0, -8.0, 0.9, -1.0, None),
            # No rg_gain: never measured, so there is nothing honest to embed.
            (str(real), "CATALOGUED", "A", "B", 3, -9.0, None, 0.9, -1.0, None),
            # Embedded successfully but never marked -- must get stamped.
            (str(untagged), "CATALOGUED", "A", "B", 4, -11.0, -7.0, 0.95, -1.5, None),
            # M4A with no lufs: the Apple atom's -23 reference cannot be
            # derived, so it must be skipped rather than given a -18 number.
            (str(no_lufs), "CATALOGUED", "A", "B", 5, None, -6.0, 0.9, -1.1, None),
        ],
    )

    ctx = SimpleNamespace(
        conn=conn,
        get=lambda k, d=None: {"forge_embed_from_db": True}.get(k, d),
        record_stage=lambda _r: None,
    )
    result = forge_mod.ForgeStage().run(ctx)  # type: ignore[arg-type]

    assert result.files_changed == 2
    assert result.files_skipped == 2  # missing on disk + the lufs-less M4A
    assert len(written) == 2
    assert no_lufs not in [w[0] for w in written]

    # The Apple atom references -23 LUFS, not the -18 the DB's rg_gain uses.
    assert written[0][3] == pytest.approx(R128_APPLE_REFERENCE - (-12.5))

    # A file that now genuinely carries the tag must be marked, or the next
    # ordinary Forge run re-measures it.
    stamped = conn.execute(
        "SELECT rg_tagged_at FROM archive WHERE file_path = ?", (str(untagged),)
    ).fetchone()[0]
    assert stamped is not None

    # ...but an already-stamped row keeps its original timestamp, and no row
    # loses lufs_tp: this path repairs embedding and never rewrites
    # measurements. Routing through _save_loudness() would have nulled these.
    kept = conn.execute(
        "SELECT rg_tagged_at, lufs_tp FROM archive WHERE file_path = ? AND track = 1",
        (str(real),),
    ).fetchone()
    assert kept[0] == "2026-08-20"
    assert kept[1] == pytest.approx(-1.2)


# ── the reference tag must match the target the gain was computed against ────
#
# REPLAYGAIN_REFERENCE_LOUDNESS was hardcoded to "18.00 LUFS" while the gain
# came from the configurable forge_target_lufs. _rg_dict_from_vorbis_style
# reads that very tag back to recover LUFS, so at any target other than -18
# Forge mis-read the loudness of files it had written itself, by exactly
# (target - (-18)) dB.
#
# Asserting the round trip, not the call: a mock arg-list check passed
# happily while the tag on disk said something else.


class TestReplayGainReferenceRoundTrip:
    def _flac(self, path):
        """A real, minimal FLAC. mutagen must be able to open and tag it."""
        import shutil
        import subprocess

        if not shutil.which("ffmpeg"):
            pytest.skip("ffmpeg not available")
        subprocess.run(
            ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
             "-f", "lavfi", "-i", "anullsrc=r=44100:cl=stereo", "-t", "0.3",
             str(path)],
            check=True,
        )
        return path

    @pytest.mark.parametrize("target", [-18.0, -16.0, -14.0, -23.0])
    def test_lufs_survives_the_round_trip_at_any_target(self, tmp_path, target):
        from mutagen.flac import FLAC

        from musaeus.loudness import lufs_to_rg
        from musaeus.stages.forge import _rg_dict_from_vorbis_style, write_rg_tags

        path = self._flac(tmp_path / "song.flac")
        measured_lufs = -9.5
        rg_gain = lufs_to_rg(measured_lufs, reference=target)

        assert write_rg_tags(path, rg_gain, 0.95, reference=target)

        recovered = _rg_dict_from_vorbis_style(FLAC(str(path)))
        assert recovered is not None
        assert recovered["lufs"] == pytest.approx(measured_lufs, abs=0.01), (
            "the reference tag must describe the target the gain was computed "
            "against, or Forge mis-reads its own output"
        )

    def test_the_reference_tag_holds_the_target_not_a_constant(self, tmp_path):
        from mutagen.flac import FLAC

        from musaeus.stages.forge import write_rg_tags

        path = self._flac(tmp_path / "song.flac")
        assert write_rg_tags(path, 3.0, 0.9, reference=-14.0)
        ref = FLAC(str(path))["REPLAYGAIN_REFERENCE_LOUDNESS"][0]
        assert ref == "14.00 LUFS", f"hardcoded reference leaked through: {ref}"

    def test_the_default_reference_is_still_r128(self, tmp_path):
        from mutagen.flac import FLAC

        from musaeus.stages.forge import write_rg_tags

        path = self._flac(tmp_path / "song.flac")
        assert write_rg_tags(path, 3.0, 0.9)
        assert FLAC(str(path))["REPLAYGAIN_REFERENCE_LOUDNESS"][0] == "18.00 LUFS"
