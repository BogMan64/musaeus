"""Effect checks for the five stages that write BOTH files and the archive.

2026-09-05. Of 39 stages, 17 verified their effect and 21 claimed one while
checking nothing. These five were the worst of that group: each moves or
copies a file AND rewrites the row, so a half-completed operation leaves the
database describing a disk that does not match it.

Each test pairs a passing case with the specific failure it exists to catch;
a check nobody has watched fail is a check nobody should trust.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from musaeus.config import MusicConfig
from musaeus.context import RunContext
from musaeus.db import open_db, upsert_archive
from musaeus.stages.base import NO_VERIFICATION
from musaeus.stages import (AuditorStage, CorruptStage, CuratorStage,
                            IntegrityStage, VariousArtistsFixStage)


@pytest.fixture
def cfg(tmp_path: Path) -> MusicConfig:
    return MusicConfig(
        vault_root=tmp_path, inbox=tmp_path / "INBOX", staging=tmp_path / "STAGING",
        quarantine=tmp_path / "QUARANTINE", runs_root=tmp_path / "RUNS",
        meta_dir=tmp_path / "MetaData", alac_library=tmp_path / "ALAC-Library",
        db_path=tmp_path / "musaeus.db",
    )


@pytest.fixture
def ctx(cfg: MusicConfig) -> RunContext:
    return RunContext.new(cfg, open_db(cfg.db_path), dry_run=False)


def _row(ctx, path: Path, *, on_disk=True, **cols):
    path.parent.mkdir(parents=True, exist_ok=True)
    if on_disk:
        path.write_bytes(b"audio")
    upsert_archive(ctx.conn, {"file_path": str(path), **cols})
    ctx.conn.commit()


def _event(ctx, path: Path, kind: str):
    ctx.log_event(kind, file_path=str(path), stage="t")
    ctx.conn.commit()


_R = MagicMock(files_changed=1)


class TestCorrupt:
    """A quarantined row must point at a file, inside quarantine."""

    def test_a_real_quarantine_passes(self, ctx, cfg):
        p = cfg.quarantine / "corrupted" / "bad.m4a"
        _row(ctx, p, status="QUARANTINED")
        _event(ctx, p, "QUARANTINE")
        assert CorruptStage().verify_effect(ctx, _R) == []

    def test_a_row_whose_move_failed_is_caught(self, ctx, cfg):
        """Status written, file never moved -- the row says handled and the
        corrupt file is still sitting in the library."""
        p = cfg.alac_library / "still_here.m4a"
        _row(ctx, p, status="QUARANTINED")
        _event(ctx, p, "QUARANTINE")
        problems = CorruptStage().verify_effect(ctx, _R)
        assert problems and "outside" in problems[0]

    def test_a_quarantined_row_pointing_at_nothing_is_caught(self, ctx, cfg):
        p = cfg.quarantine / "corrupted" / "vanished.m4a"
        _row(ctx, p, status="QUARANTINED")
        _event(ctx, p, "QUARANTINE")
        p.unlink()
        assert CorruptStage().verify_effect(ctx, _R)


class TestCurator:
    """An export is a library, not a list of promises."""

    def test_a_real_export_passes(self, ctx, cfg, tmp_path):
        src, dst = cfg.alac_library / "s.m4a", tmp_path / "USB" / "a" / "s.m4a"
        dst.parent.mkdir(parents=True, exist_ok=True)
        dst.write_bytes(b"copied")
        _row(ctx, src, status="CATALOGUED")
        ctx.conn.execute("UPDATE archive SET car_export_path=? WHERE file_path=?",
                         (str(dst), str(src)))
        ctx.conn.commit()
        _event(ctx, src, "CURATOR_COPY")
        assert CuratorStage().verify_effect(ctx, _R) == []

    def test_a_missing_export_is_caught(self, ctx, cfg, tmp_path):
        src = cfg.alac_library / "s.m4a"
        _row(ctx, src, status="CATALOGUED")
        ctx.conn.execute("UPDATE archive SET car_export_path=? WHERE file_path=?",
                         (str(tmp_path / "USB" / "gone.m4a"), str(src)))
        ctx.conn.commit()
        _event(ctx, src, "CURATOR_COPY")
        assert CuratorStage().verify_effect(ctx, _R)

    def test_a_truncated_export_is_caught(self, ctx, cfg, tmp_path):
        """exists() alone passes an empty file -- the copy that died at byte 0
        onto a full device."""
        src, dst = cfg.alac_library / "s.m4a", tmp_path / "USB" / "empty.m4a"
        dst.parent.mkdir(parents=True, exist_ok=True)
        dst.write_bytes(b"")
        _row(ctx, src, status="CATALOGUED")
        ctx.conn.execute("UPDATE archive SET car_export_path=? WHERE file_path=?",
                         (str(dst), str(src)))
        ctx.conn.commit()
        _event(ctx, src, "CURATOR_COPY")
        problems = CuratorStage().verify_effect(ctx, _R)
        assert problems and "empty" in problems[0]


class TestVariousArtistsFix:
    def test_a_real_fix_passes(self, ctx, cfg):
        p = cfg.alac_library / "Real Artist - t.m4a"
        _row(ctx, p, status="CATALOGUED", artist="Real Artist", title="t")
        _event(ctx, p, "VARIOUS_ARTISTS_FIXED")
        assert VariousArtistsFixStage().verify_effect(ctx, _R) == []

    def test_a_row_still_saying_various_artists_is_caught(self, ctx, cfg):
        p = cfg.alac_library / "va.m4a"
        _row(ctx, p, status="CATALOGUED", artist="Various Artists", title="t")
        _event(ctx, p, "VARIOUS_ARTISTS_FIXED")
        problems = VariousArtistsFixStage().verify_effect(ctx, _R)
        assert problems and "Various Artists" in problems[0]

    def test_a_fixed_row_whose_move_failed_is_caught(self, ctx, cfg):
        p = cfg.alac_library / "moved.m4a"
        _row(ctx, p, status="CATALOGUED", artist="Real", title="t")
        _event(ctx, p, "VARIOUS_ARTISTS_FIXED")
        p.unlink()
        assert VariousArtistsFixStage().verify_effect(ctx, _R)


class TestMeasurementStages:
    """Auditor and Integrity create their columns on first run, so a missing
    column means 'not run', not 'failed'."""

    def test_auditor_without_its_columns_makes_no_claim(self, ctx):
        assert AuditorStage().verify_effect(ctx, _R) is NO_VERIFICATION

    def test_integrity_without_its_columns_makes_no_claim(self, ctx):
        assert IntegrityStage().verify_effect(ctx, _R) is NO_VERIFICATION

    def test_auditor_rejects_an_implausible_measurement(self, ctx, cfg):
        """A loudnorm parse that yields nothing writes NULL or 0.0 for every
        row; a presence-only check would call that success."""
        ctx.conn.execute("ALTER TABLE archive ADD COLUMN auditor_lufs REAL")
        ctx.conn.execute("ALTER TABLE archive ADD COLUMN auditor_checked_at TEXT")
        p = cfg.alac_library / "a.m4a"
        _row(ctx, p, status="CATALOGUED")
        ctx.conn.execute("UPDATE archive SET auditor_lufs=NULL WHERE file_path=?", (str(p),))
        ctx.conn.commit()
        _event(ctx, p, "AUDITOR_PASS")
        assert AuditorStage().verify_effect(ctx, _R)

    def test_auditor_accepts_a_real_measurement(self, ctx, cfg):
        ctx.conn.execute("ALTER TABLE archive ADD COLUMN auditor_lufs REAL")
        ctx.conn.execute("ALTER TABLE archive ADD COLUMN auditor_checked_at TEXT")
        p = cfg.alac_library / "a.m4a"
        _row(ctx, p, status="CATALOGUED")
        ctx.conn.execute("UPDATE archive SET auditor_lufs=-14.2 WHERE file_path=?", (str(p),))
        ctx.conn.commit()
        _event(ctx, p, "AUDITOR_PASS")
        assert AuditorStage().verify_effect(ctx, _R) == []

    def test_integrity_catches_an_unwritten_result(self, ctx, cfg):
        """The PermissionsStage failure: work reported, UPDATE touched nothing."""
        ctx.conn.execute("ALTER TABLE archive ADD COLUMN integrity_checked_at TEXT")
        p = cfg.alac_library / "i.m4a"
        _row(ctx, p, status="CATALOGUED")
        _event(ctx, p, "INTEGRITY_FAIL")
        problems = IntegrityStage().verify_effect(ctx, _R)
        assert problems and "never written" in problems[0]
