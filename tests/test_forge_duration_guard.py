"""
ForgeStage must refuse absurdly long files before spawning ffmpeg.

BPMStage was bounded on 2026-08-23 after a 12-hour "sound bath" OOM-killed
an entire run. Forge was left unguarded the same day: loudness.py scales its
ffmpeg timeout to 30% of duration (capped 600 s), so Forge degrades by
*timing out* rather than OOM-ing — survivable, but ~20 min burned across two
retries to produce nothing.

The point of the guard is that the decision comes from what Scholar already
recorded, before any subprocess starts. A test that only asserted "no LUFS
was written" would pass whether ffmpeg ran or not, so what is asserted here
is that measure_loudness is never called.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from musaeus.config import MusicConfig
from musaeus.context import RunContext
from musaeus.db import open_db, upsert_archive
from musaeus.stages.forge import _MAX_LOUDNESS_SECONDS, ForgeStage


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
    return RunContext.new(cfg, open_db(cfg.db_path), dry_run=False)


def _catalogue(ctx: RunContext, path: Path, duration: float) -> None:
    upsert_archive(
        ctx.conn,
        {
            "file_path": str(path),
            "status": "CATALOGUED",
            "ext": path.suffix,
            "artist": "Artist",
            "album": "Album",
            "title": "Song",
            "duration": duration,
        },
    )
    ctx.conn.commit()


def _run_one(ctx: RunContext, path: Path, measured=("ok",)):
    """Call _process_one with ffmpeg and the tag reader stubbed out."""
    with (
        patch("musaeus.stages.forge.read_existing_rg_tags", return_value=None),
        patch("musaeus.stages.forge.measure_loudness") as measure,
        patch("musaeus.stages.forge.write_rg_tags", return_value=True),
    ):
        measure.return_value = (-14.0, -1.0, measured[0])
        status = ForgeStage()._process_one(
            ctx, str(path), dry_run=False, target_lufs=-16.0
        )
    return status, measure


class TestLongFilesAreRefusedBeforeFFmpeg:
    def test_twelve_hour_file_is_skipped_without_measuring(self, ctx, tmp_path):
        track = tmp_path / "sound bath.m4a"
        track.write_bytes(b"x")
        _catalogue(ctx, track, duration=12 * 60 * 60)

        status, measure = _run_one(ctx, track)

        assert status == "skip_too_long"
        measure.assert_not_called()

    def test_an_event_records_the_refusal(self, ctx, tmp_path):
        track = tmp_path / "hypnosis.m4a"
        track.write_bytes(b"x")
        _catalogue(ctx, track, duration=4 * 60 * 60)

        _run_one(ctx, track)

        row = ctx.conn.execute(
            "SELECT event_type, new_value FROM events WHERE event_type='FORGE_SKIPPED_TOO_LONG'"
        ).fetchone()
        assert row is not None
        assert "240 min" in row["new_value"]

    def test_a_skip_counts_as_skipped_not_errored(self, ctx, tmp_path):
        # run() treats any unrecognised status as an error, which would have
        # made a guarded file look like a failure.
        track = tmp_path / "long.m4a"
        track.write_bytes(b"x")
        _catalogue(ctx, track, duration=10 * 60 * 60)

        with (
            patch.object(ForgeStage, "validate"),
            patch("musaeus.stages.forge.read_existing_rg_tags", return_value=None),
            patch("musaeus.stages.forge.measure_loudness") as measure,
        ):
            result = ForgeStage().run(ctx)

        measure.assert_not_called()
        assert result.files_skipped == 1
        assert result.files_errored == 0


class TestRealMusicIsUnaffected:
    @pytest.mark.parametrize(
        ("name", "minutes"),
        [
            ("Miles Davis - Go Ahead John.m4a", 28.5),  # longest in the library
            ("Allman Brothers - Whipping Post.m4a", 22.9),
            ("ordinary song.m4a", 3.5),
            ("just under the line.m4a", 44.9),
        ],
    )
    def test_long_but_genuine_tracks_still_measure(self, ctx, tmp_path, name, minutes):
        track = tmp_path / name
        track.write_bytes(b"x")
        _catalogue(ctx, track, duration=minutes * 60)

        status, measure = _run_one(ctx, track)

        assert status == "ok"
        measure.assert_called_once()

    def test_missing_duration_does_not_trigger_the_guard(self, ctx, tmp_path):
        # An unmeasured row must not be refused on a null — that would skip
        # real music silently, which is worse than the timeout being guarded.
        track = tmp_path / "unknown length.m4a"
        track.write_bytes(b"x")
        _catalogue(ctx, track, duration=None)

        status, measure = _run_one(ctx, track)

        assert status == "ok"
        measure.assert_called_once()


class TestExistingTagsWinOverTheGuard:
    def test_long_file_with_replaygain_tags_still_yields_them(self, ctx, tmp_path):
        # The shortcut costs no ffmpeg, so length is irrelevant to it. Placing
        # the guard first would throw away values we already have.
        track = tmp_path / "long but tagged.m4a"
        track.write_bytes(b"x")
        _catalogue(ctx, track, duration=6 * 60 * 60)

        existing = {"lufs": -13.0, "lufs_tp": -1.2, "rg_gain": 2.0, "rg_peak": 0.9}
        with (
            patch("musaeus.stages.forge.read_existing_rg_tags", return_value=existing),
            patch("musaeus.stages.forge.measure_loudness") as measure,
        ):
            status = ForgeStage()._process_one(
                ctx, str(track), dry_run=False, target_lufs=-16.0
            )

        assert status == "tag_shortcut"
        measure.assert_not_called()


def test_ceiling_matches_the_bpm_stage():
    """Both stages refuse the same material for different reasons. If one
    moves without the other, a file becomes analysable by one stage and not
    the other, which is harder to reason about than a single number."""
    from musaeus.stages.bpm import _MAX_ANALYSIS_SECONDS

    assert _MAX_LOUDNESS_SECONDS == _MAX_ANALYSIS_SECONDS == 45 * 60
