"""The marker must mean "verified on disk", never "we tried".

Selection is on identity_tagged_at IS NULL, so a marker written on a write
that did not land removes the row from the queue permanently. That is the
same failure mb_enrich suffered twice in one day (2026-08-26): once by
never marking a genuine miss, once by marking a network failure as
settled. Here the rule is that only a proven write settles a row.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from musaeus.config import MusicConfig
from musaeus.context import RunContext
from musaeus.db import open_db, upsert_archive
from musaeus.identity_tags import read_identity
from musaeus.stages.identity_tag import IdentityTagStage

MBID = "4ef7a9e2-2cf5-483a-8616-ef7791a98026"


def _encode(path: Path) -> bool:
    if not shutil.which("ffmpeg"):
        return False
    r = subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi",
         "-i", "sine=frequency=440:duration=1", "-c:a", "alac", str(path)],
        capture_output=True,
    )
    return r.returncode == 0 and path.exists()


@pytest.fixture
def ctx(tmp_path) -> RunContext:
    cfg = MusicConfig(
        vault_root=tmp_path, inbox=tmp_path / "INBOX", staging=tmp_path / "STAGING",
        quarantine=tmp_path / "QUARANTINE", runs_root=tmp_path / "RUNS",
        meta_dir=tmp_path / "MetaData", alac_library=tmp_path / "ALAC-Library",
        db_path=tmp_path / "musaeus.db",
    )
    cfg.meta_dir.mkdir(parents=True, exist_ok=True)
    cfg.alac_library.mkdir(parents=True, exist_ok=True)
    return RunContext.new(cfg, open_db(cfg.db_path), dry_run=False)


def _track(ctx, name="t.m4a", mbid=MBID):
    p = ctx.config.alac_library / name
    if not _encode(p):
        pytest.skip("ffmpeg unavailable")
    upsert_archive(ctx.conn, {"file_path": str(p), "status": "CATALOGUED",
                              "artist": "Bryan Ferry", "title": "Slave to Love"})
    ctx.conn.execute("ALTER TABLE archive ADD COLUMN mb_artist_id TEXT")
    ctx.conn.execute("UPDATE archive SET mb_artist_id=? WHERE file_path=?", (mbid, str(p)))
    ctx.conn.commit()
    return p


class TestTheTagReachesTheFile:
    def test_a_row_with_an_mbid_gets_it_written_to_disk(self, ctx):
        p = _track(ctx)
        IdentityTagStage().run(ctx)
        # Read from DISK, not from the stage's own report.
        assert read_identity(p).get("mb_artist_id") == MBID

    def test_the_marker_is_set_only_after_a_verified_write(self, ctx):
        _track(ctx)
        IdentityTagStage().run(ctx)
        row = ctx.conn.execute("SELECT identity_tagged_at FROM archive").fetchone()
        assert row["identity_tagged_at"] is not None

    def test_a_second_run_skips_already_tagged_rows(self, ctx):
        _track(ctx)
        IdentityTagStage().run(ctx)
        second = IdentityTagStage().run(ctx)
        assert second.files_processed == 0, "the marker must remove it from the queue"


class TestAnUnverifiedWriteIsNotSettled:
    def test_a_write_that_does_not_land_leaves_the_marker_null(self, ctx, monkeypatch):
        _track(ctx)
        # The write silently fails to reach disk -- silent-no-op #2's shape.
        monkeypatch.setattr(
            "musaeus.stages.identity_tag.write_identity",
            lambda p, v: (False, "did not survive the write"),
        )
        result = IdentityTagStage().run(ctx)

        row = ctx.conn.execute("SELECT identity_tagged_at FROM archive").fetchone()
        assert row["identity_tagged_at"] is None, (
            "an unverified write must not settle the row -- selection is on "
            "identity_tagged_at IS NULL, so a marker here loses it for ever"
        )
        assert any("could NOT be verified" in n for n in result.notes)

    def test_it_is_retried_on_the_next_run(self, ctx, monkeypatch):
        _track(ctx)
        monkeypatch.setattr(
            "musaeus.stages.identity_tag.write_identity",
            lambda p, v: (False, "nope"),
        )
        IdentityTagStage().run(ctx)
        monkeypatch.undo()
        again = IdentityTagStage().run(ctx)
        assert again.files_changed == 1, "the deferred row must come back"


class TestVerifyEffectReadsDisk:
    def test_it_catches_a_marker_whose_file_lacks_the_tag(self, ctx):
        p = _track(ctx)
        stage = IdentityTagStage()
        result = stage.run(ctx)
        # Strip the tag behind the stage's back; verify_effect must notice.
        from mutagen.mp4 import MP4
        a = MP4(str(p))
        a.tags.clear()
        a.save()
        assert stage.verify_effect(ctx, result), "must read disk, not its own bookkeeping"


def test_dry_run_works_on_a_database_lacking_the_marker_column(tmp_path):
    """_rows() names identity_tagged_at, so a preview crashed on any database
    where this stage had never run live -- i.e. the preview failed on exactly
    the databases it exists to make safe. _ensure_columns is now unconditional.
    """
    from musaeus.config import MusicConfig
    from musaeus.context import RunContext
    from musaeus.db import open_db
    from musaeus.stages.identity_tag import IdentityTagStage

    cfg = MusicConfig(
        vault_root=tmp_path, inbox=tmp_path / "INBOX", staging=tmp_path / "STAGING",
        quarantine=tmp_path / "QUARANTINE", runs_root=tmp_path / "RUNS",
        meta_dir=tmp_path / "MetaData", alac_library=tmp_path / "ALAC-Library",
        db_path=tmp_path / "musaeus.db",
    )
    cfg.ensure_dirs()
    conn = open_db(cfg.db_path)
    cols = [r[1] for r in conn.execute("PRAGMA table_info(archive)")]
    if "identity_tagged_at" in cols:
        conn.execute("ALTER TABLE archive RENAME COLUMN identity_tagged_at TO _removed")
        conn.commit()
    # The state this reproduces: at least one identity column present (so
    # _present() does not early-return) while identity_tagged_at is absent.
    if "mb_artist_id" not in [r[1] for r in conn.execute("PRAGMA table_info(archive)")]:
        conn.execute("ALTER TABLE archive ADD COLUMN mb_artist_id TEXT")
    conn.execute(
        "INSERT INTO archive (file_path, status, mb_artist_id) "
        "VALUES ('/x/a.m4a', 'CATALOGUED', 'some-mbid')"
    )
    conn.commit()

    ctx = RunContext.new(cfg, conn, dry_run=True)
    result = IdentityTagStage().dry_run(ctx)   # must not raise
    assert result is not None
