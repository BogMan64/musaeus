"""File-changing previews name paths and leave files and catalogue untouched."""

from pathlib import Path

import pytest

from musaeus.config import MusicConfig
from musaeus.db import deny_hash, ensure_deny_list, open_db, open_hash_index, upsert_archive
from musaeus.planner import build_plan
from musaeus.stages.artist_consolidate import ArtistConsolidateStage
from musaeus.stages.corrupt import CorruptStage
from musaeus.stages.deny_list import DenyListStage
from musaeus.stages.organize import OrganizeStage
from musaeus.stages.tribute_quarantine import TributeQuarantineStage


@pytest.fixture
def vault(tmp_path: Path) -> tuple[MusicConfig, object]:
    cfg = MusicConfig(
        vault_root=tmp_path,
        inbox=tmp_path / "INBOX",
        staging=tmp_path / "STAGING",
        quarantine=tmp_path / "QUARANTINE",
        runs_root=tmp_path / "RUNS",
        meta_dir=tmp_path / "MetaData",
        alac_library=tmp_path / "ALAC-Library",
        db_path=tmp_path / "musaeus.db",
    )
    for root in (cfg.inbox, cfg.staging, cfg.alac_library, cfg.meta_dir):
        root.mkdir(parents=True, exist_ok=True)
    conn = open_db(cfg.db_path)
    yield cfg, conn
    conn.close()


def add_track(
    conn,
    path: Path,
    *,
    status="CATALOGUED",
    artist="Test Artist",
    title="Song",
    album="Album",
    audio_hash=None,
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"audio")
    upsert_archive(
        conn,
        {
            "file_path": str(path),
            "status": status,
            "artist": artist,
            "title": title,
            "album": album,
            "audio_hash": audio_hash,
        },
    )
    conn.commit()


def test_organize_lists_move_and_keeps_source(vault) -> None:
    cfg, conn = vault
    source = cfg.inbox / "flat.m4a"
    add_track(conn, source)

    stage = build_plan(cfg, [OrganizeStage]).stages[0]

    assert stage.candidates == 1
    assert stage.actions[0]["action"] == "move"
    assert stage.actions[0]["source"] == str(source)
    assert stage.actions[0]["destination"].endswith(
        "Unsorted/Test Artist/Album/Test Artist - Song.m4a"
    )
    assert source.exists()
    assert not Path(stage.actions[0]["destination"]).exists()
    assert conn.execute("SELECT file_path FROM archive").fetchone()[0] == str(source)


def test_organize_reserves_colliding_targets_in_plan(vault) -> None:
    cfg, conn = vault
    first = cfg.inbox / "one.m4a"
    second = cfg.inbox / "two.m4a"
    add_track(conn, first)
    add_track(conn, second)

    stage = build_plan(cfg, [OrganizeStage]).stages[0]

    assert stage.candidates == 2
    assert len({a["destination"] for a in stage.actions}) == 2
    assert any("Song (2).m4a" in a["destination"] for a in stage.actions)
    assert first.exists() and second.exists()


def test_artist_consolidate_lists_canon_rename(vault) -> None:
    cfg, conn = vault
    (cfg.meta_dir / "artist_canon.tsv").write_text("Old Name\tNew Name\n")
    source = cfg.inbox / "song.m4a"
    add_track(conn, source, artist="Old Name")

    stage = build_plan(cfg, [ArtistConsolidateStage]).stages[0]

    assert stage.actions == (
        {
            "action": "artist rename",
            "source": "Old Name",
            "destination": "New Name",
            "reason": "1 catalogued track(s)",
        },
    )
    assert conn.execute("SELECT artist FROM archive").fetchone()[0] == "Old Name"


def test_tribute_preview_names_quarantine_and_reason(vault) -> None:
    cfg, conn = vault
    source = cfg.inbox / "karaoke.m4a"
    add_track(conn, source, artist="Hit Tunes Karaoke")

    stage = build_plan(cfg, [TributeQuarantineStage]).stages[0]

    assert stage.candidates == 1
    assert stage.actions[0]["source"] == str(source)
    assert "TRIBUTE_REMOVED" in stage.actions[0]["destination"]
    assert stage.actions[0]["reason"]
    assert source.exists()


def test_deny_preview_names_quarantine_and_reason(vault) -> None:
    cfg, conn = vault
    source = cfg.inbox / "returning.m4a"
    add_track(conn, source, status="HASHED", audio_hash="h" * 64)
    cfg.hash_index_path.parent.mkdir(parents=True, exist_ok=True)
    index = open_hash_index(cfg.hash_index_path)
    ensure_deny_list(index)
    deny_hash(index, "h" * 64, "removed by owner decision", str(source))
    index.commit()
    index.close()

    stage = build_plan(cfg, [DenyListStage]).stages[0]

    assert stage.candidates == 1
    assert stage.actions[0]["source"] == str(source)
    assert stage.actions[0]["destination"] == str(cfg.quarantine / "denied" / source.name)
    assert stage.actions[0]["reason"] == "removed by owner decision"
    assert source.exists()


def test_corrupt_preview_decodes_and_names_quarantine(vault, monkeypatch) -> None:
    cfg, conn = vault
    source = cfg.inbox / "broken.m4a"
    add_track(conn, source)
    monkeypatch.setattr("musaeus.stages.corrupt.check_file", lambda *a, **k: (True, "suspect"))
    monkeypatch.setattr(
        "musaeus.stages.corrupt.ffmpeg_decode_check",
        lambda *a, **k: (False, "bad audio"),
    )

    stage = build_plan(cfg, [CorruptStage], deep=True).stages[0]

    assert stage.candidates == 1
    assert stage.actions[0]["source"] == str(source)
    assert stage.actions[0]["destination"] == str(cfg.quarantine / "corrupted" / source.name)
    assert "bad audio" in stage.actions[0]["reason"]
    assert source.exists()
    assert conn.execute("SELECT status FROM archive").fetchone()[0] == "CATALOGUED"


def test_corrupt_decodes_only_in_a_deep_plan(vault, monkeypatch) -> None:
    """A plain plan took about 3 minutes on the live library while Corrupt decoded
    audio (Grey, 2026-10-23: only when asked, `musaeus plan --deep`)."""
    cfg, _ = vault
    decoded: list[str] = []
    monkeypatch.setattr(
        CorruptStage,
        "plan_actions",
        classmethod(lambda cls, conn, c: decoded.append("x") or ([], "")),
    )
    stage = build_plan(cfg, [CorruptStage]).stages[0]
    assert decoded == [] and stage.previewable and not stage.actions
    build_plan(cfg, [CorruptStage], deep=True)
    assert decoded == ["x"]
