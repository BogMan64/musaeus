"""
A playlist entry must never be an absolute path.

PlaylistStage's own docstring says its output works "regardless of where the
USB drive mounts". Until 2026-09-17 a source it could not make relative was
emitted as an absolute path instead of being dropped, which is the silent-
no-op family again: the line looks identical to a working one, every player
accepts the file, and it fails only in the car.

It bites hardest on an edition index. `write_car_index.py` points this stage
at `CAR_Library/Playlists`, and a catalogued row with no `car_export_path`
falls back to its `ALAC_Library` path -- outside that tree. Measured against
the live vault on 2026-09-17: **406 rows** would have shipped to the car USB
as `/mnt/FORGE2TB/...` lines.
"""

from __future__ import annotations

import dataclasses
from pathlib import Path

import pytest

from musaeus.db import open_db
from musaeus.stages.playlist import PlaylistStage

from .disposable_vault import make_disposable_vault


def _add(conn, *, file_path, artist, title, genre, year):
    conn.execute(
        "INSERT INTO archive (file_path, filename, artist, title, genre, year, status) "
        "VALUES (?,?,?,?,?,?,'CATALOGUED')",
        (str(file_path), Path(file_path).name, artist, title, genre, year),
    )


@pytest.fixture
def vault_with_one_inside_and_one_outside(tmp_path):
    """One track inside the edition, one outside it. Only the first is placeable."""
    dv = make_disposable_vault(tmp_path)
    edition = dv.root / "Libraries" / "CAR_Library"
    inside = edition / "The Beatles" / "Revolver" / "Taxman.m4a"
    inside.parent.mkdir(parents=True, exist_ok=True)
    inside.write_bytes(b"not really audio")

    outside = tmp_path / "somewhere_else" / "Elsewhere.m4a"
    outside.parent.mkdir(parents=True, exist_ok=True)
    outside.write_bytes(b"not really audio")

    cfg = dataclasses.replace(dv.cfg, playlists=edition / "Playlists")
    conn = open_db(cfg.db_path)
    _add(conn, file_path=inside, artist="The Beatles", title="Taxman",
         genre="Rock", year="1966")
    _add(conn, file_path=outside, artist="Someone Else", title="Elsewhere",
         genre="Rock", year="1966")
    conn.commit()
    yield dv, cfg, conn, edition
    conn.close()


def _entries(playlist: Path) -> list[str]:
    return [
        ln.strip()
        for ln in playlist.read_text(encoding="utf-8").splitlines()
        if ln.strip() and not ln.startswith("#")
    ]


class TestNoEntryIsEverAbsolute:
    def test_outside_source_is_dropped_not_written_absolute(
        self, vault_with_one_inside_and_one_outside
    ):
        dv, cfg, conn, edition = vault_with_one_inside_and_one_outside
        from musaeus.context import RunContext

        ctx = RunContext.new(cfg, conn)
        PlaylistStage().run(ctx)
        ctx.finish()

        written = sorted((edition / "Playlists").glob("*.m3u8"))
        assert written, "no playlists were written at all"
        for pl in written:
            for entry in _entries(pl):
                assert not Path(entry).is_absolute(), f"{pl.name} wrote an absolute path: {entry}"

    def test_the_placeable_track_still_gets_in(
        self, vault_with_one_inside_and_one_outside
    ):
        """The drop must be surgical -- refusing the outside source must not
        cost the inside one, or the guard has traded one silent failure for
        another."""
        dv, cfg, conn, edition = vault_with_one_inside_and_one_outside
        from musaeus.context import RunContext

        ctx = RunContext.new(cfg, conn)
        PlaylistStage().run(ctx)
        ctx.finish()

        rock = edition / "Playlists" / "Rock.m3u8"
        assert rock.is_file()
        entries = _entries(rock)
        assert entries == ["../The Beatles/Revolver/Taxman.m4a"], entries

    def test_every_entry_resolves_to_a_real_file(
        self, vault_with_one_inside_and_one_outside
    ):
        """Relative is not the same as correct. `..` from the playlist folder
        has to land on the edition root in the vault AND on the USB, which is
        only true while the index sits one level below it."""
        dv, cfg, conn, edition = vault_with_one_inside_and_one_outside
        from musaeus.context import RunContext

        ctx = RunContext.new(cfg, conn)
        PlaylistStage().run(ctx)
        ctx.finish()

        for pl in sorted((edition / "Playlists").glob("*.m3u8")):
            for entry in _entries(pl):
                assert (pl.parent / entry).resolve().is_file(), f"{pl.name}: {entry}"

    def test_the_skip_is_reported_and_counted_once(
        self, vault_with_one_inside_and_one_outside
    ):
        """A silent drop is the failure this guard exists to prevent. The count
        must also be per FILE, not per pass -- each source is offered to the
        path builder once per genre list, once per era list and once for All."""
        dv, cfg, conn, edition = vault_with_one_inside_and_one_outside
        from musaeus.context import RunContext

        ctx = RunContext.new(cfg, conn)
        result = PlaylistStage().run(ctx)
        ctx.finish()

        skipped = [n for n in result.notes if "skipped (path outside" in n]
        assert skipped, f"the skip was not reported: {result.notes}"
        assert ": 1" in skipped[0], skipped[0]
