"""Review of slice B (#145), finding 8: Curator's export split artist names
on its own, ignoring the canon ("KC & The Sunshine Band" -> "KC"), and two
songs could get one export path -- the second copy overwrote the first."""

from __future__ import annotations

from pathlib import Path

import pytest

from musaeus.config import MusicConfig
from musaeus.context import RunContext
from musaeus.db import open_db, upsert_archive
from musaeus.stages.curator import CuratorStage, _dest_path, _primary_artist


@pytest.mark.parametrize(
    ("credit", "folder"),
    [
        ("KC & The Sunshine Band", "KC & The Sunshine Band"),
        ("Peter, Paul and Mary", "Peter, Paul and Mary"),
        ("Beatles, The", "The Beatles"),  # folders are "The X" since 2026-10-17
        ("Elton John & Kiki Dee", "Elton John"),
        ("Al Green feat. Someone", "Al Green"),
    ],
)
def test_the_export_files_under_the_masters_artist(credit, folder):
    assert _primary_artist(credit) == folder


def test_two_songs_never_share_an_export_path(tmp_path):
    cfg = MusicConfig(
        vault_root=tmp_path,
        inbox=tmp_path / "INBOX",
        staging=tmp_path / "STAGING",
        quarantine=tmp_path / "QUARANTINE",
        runs_root=tmp_path / "RUNS",
        meta_dir=tmp_path / "MetaData",
        alac_library=tmp_path / "Libraries" / "ALAC_Library",
        db_path=tmp_path / "musaeus.db",
    )
    ctx = RunContext.new(cfg, open_db(cfg.db_path), dry_run=False)
    for credit, body in (("Elton John & Kiki Dee", b"DUET"), ("Elton John", b"SOLO-VERSION")):
        src = tmp_path / "masters" / f"{credit}.m4a"
        src.parent.mkdir(exist_ok=True)
        src.write_bytes(body)
        upsert_archive(
            ctx.conn,
            {"file_path": str(src), "status": "CATALOGUED", "artist": credit,
             "title": "Don't Go Breaking My Heart", "ext": ".m4a"},
        )  # fmt: skip
    ctx.conn.commit()
    export = tmp_path / "export"
    ctx.set("curator_export_root", str(export))
    ctx.set("curator_noise", "clean")
    CuratorStage().run(ctx)
    paths = [r[0] for r in ctx.conn.execute("SELECT car_export_path FROM archive")]
    assert len(set(paths)) == 2, f"two songs share one export path: {paths}"
    assert sorted(Path(p).read_bytes() for p in paths) == [b"DUET", b"SOLO-VERSION"]


def test_the_export_folder_is_the_masters_folder():
    from musaeus.stages.organize import library_relpath

    for credit, mb in (
        ("Ike & Tina Turner", "Ike & Tina Turner"),
        ("England Dan & John Ford Coley", "England Dan & John Ford Coley"),
        ("KC & The Sunshine Band", None),
        ("Elton John & Kiki Dee", None),
    ):
        filed = library_relpath(credit, mb, "Rock", "Album", "Song", ".m4a").parts[1]
        row = {"artist": credit, "mb_artist_name": mb, "title": "Song", "file_path": "/m/a.m4a"}
        exported = _dest_path(Path("/export"), row).relative_to("/export").parts[0]
        assert exported == filed, credit


def test_the_export_folder_spells_and_as_the_masters_do():
    """Grey, 2026-10-12: folders spell "and" as "&"; the export matches."""
    from musaeus.stages.organize import library_relpath

    row = {"artist": "Huey Lewis And The News", "mb_artist_name": None, "title": "Do You Believe In Love",
           "album": "Picture This And More", "file_path": "/m/a.m4a"}  # fmt: skip
    filed = library_relpath(row["artist"], None, "Rock", row["album"], row["title"], ".m4a").parts
    exported = _dest_path(Path("/export"), row).relative_to("/export").parts
    assert exported[:2] == filed[1:3] == ("Huey Lewis & The News", "Picture This & More")
