"""consolidate_artist_folders merges an artist in a Genre/Artist/Album library.

It used to find the old artist by walking `Libraries/<tier>/<artist folder>`.
After the library gained its genre level that walk found nothing: a dry run
of "Simon" -> "Simon & Garfunkel" on 2026-09-24 reported 0 files in both ALAC
tiers while 17 sat in Folk/Simon, and --execute would have relabelled every
row and moved none of the files.

These tests build a small vault in that layout and hold the script to:
every copy found from the catalogue, every destination from the rule organize
files by, a clash refusing the whole merge, and nothing deleted that the
catalogue does not know about.
"""

from __future__ import annotations

import importlib.util
import sqlite3
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from musaeus.stages.organize import library_relpath

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "consolidate_artist_folders.py"


def _load():
    spec = importlib.util.spec_from_file_location("consolidate_artist_folders", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    # @dataclass looks its class's module up in sys.modules while decorating.
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def vault(tmp_path, monkeypatch):
    libs = tmp_path / "Libraries"
    cfg = SimpleNamespace(
        vault_root=tmp_path,
        alac_library=libs / "ALAC_Library",
        alac_archive=libs / "ALAC-Archival",
        meta_dir=tmp_path / "MetaData",
        db_path=tmp_path / "musaeus.db",
    )
    conn = sqlite3.connect(cfg.db_path)
    conn.row_factory = sqlite3.Row
    conn.execute(
        "CREATE TABLE archive (id INTEGER PRIMARY KEY, artist TEXT, genre TEXT, album TEXT, "
        "title TEXT, file_path TEXT, car_export_path TEXT, mb_artist_name TEXT, status TEXT)"
    )
    conn.execute(
        "CREATE TABLE events (id INTEGER PRIMARY KEY, run_id TEXT, ts TEXT, event_type TEXT, "
        "file_path TEXT, old_value TEXT, new_value TEXT, stage TEXT, note TEXT)"
    )
    mod = _load()
    retagged: list[Path] = []
    monkeypatch.setattr(mod, "_retag_car", lambda p, a: retagged.append(p))

    def add(artist, genre, album, title, *, car=True, master=True, data=b"audio"):
        rel = library_relpath(artist, None, genre, album, title, ".m4a")
        lib = cfg.alac_library / rel
        lib.parent.mkdir(parents=True, exist_ok=True)
        lib.write_bytes(data)
        if master:
            m = cfg.alac_archive / rel
            m.parent.mkdir(parents=True, exist_ok=True)
            m.write_bytes(data)
        car_path = None
        if car:
            car_path = libs / "CAR_Library" / rel.parts[1] / rel.parts[2] / rel.parts[3]
            car_path.parent.mkdir(parents=True, exist_ok=True)
            car_path.write_bytes(data)
        cur = conn.execute(
            "INSERT INTO archive (artist, genre, album, title, file_path, car_export_path, status) "
            "VALUES (?,?,?,?,?,?, 'CATALOGUED')",
            (artist, genre, album, title, str(lib), str(car_path) if car_path else None),
        )
        conn.commit()
        return cur.lastrowid

    return SimpleNamespace(cfg=cfg, conn=conn, mod=mod, add=add, libs=libs, retagged=retagged)


def _row(v, rid):
    return v.conn.execute("SELECT * FROM archive WHERE id=?", (rid,)).fetchone()


def test_finds_every_copy_under_the_genre_level_and_moves_all_three(vault):
    vault.add("Simon & Garfunkel", "Folk", "Bookends", "Mrs. Robinson")
    rid = vault.add("Simon", "Folk", "Bookends", "America")

    plan = vault.mod.plan_merge(vault.cfg, vault.conn, "Simon", "Simon & Garfunkel")
    assert len(plan.moves) == 3, "library, master and car copies must all be found"
    vault.mod.execute_plan(vault.cfg, vault.conn, plan)

    want = Path("Folk/Simon & Garfunkel/Bookends/Simon & Garfunkel - America.m4a")
    assert (vault.cfg.alac_library / want).is_file()
    assert (vault.cfg.alac_archive / want).is_file()
    car = vault.libs / "CAR_Library/Simon & Garfunkel/Bookends/Simon & Garfunkel - America.m4a"
    assert car.is_file()
    row = _row(vault, rid)
    assert (row["artist"], row["file_path"], row["car_export_path"]) == (
        "Simon & Garfunkel",
        str(vault.cfg.alac_library / want),
        str(car),
    )
    assert not (vault.cfg.alac_library / "Folk/Simon").exists(), "emptied folder should go"
    assert vault.retagged == [car]


def test_the_next_organize_pass_would_move_nothing(vault):
    vault.add("Simon & Garfunkel", "Folk", "Bookends", "Mrs. Robinson")
    ids = [
        vault.add("Simon", "Folk", a, t)
        for a, t in (("Bookends", "America"), ("Greatest Hits", "Kathy's Song"))
    ]
    vault.mod.execute_plan(
        vault.cfg,
        vault.conn,
        vault.mod.plan_merge(vault.cfg, vault.conn, "Simon", "Simon & Garfunkel"),
    )
    for rid in ids:
        r = _row(vault, rid)
        rel = library_relpath(
            r["artist"], r["mb_artist_name"], r["genre"], r["album"], r["title"], ".m4a"
        )
        assert Path(r["file_path"]) == vault.cfg.alac_library / rel


def test_a_cross_genre_merge_lands_in_the_targets_genre(vault):
    vault.add("Crosby, Stills, Nash & Young", "Folk Rock", "Deja Vu", "Carry On")
    rid = vault.add("Crosby", "Hip Hop", "Deja Vu", "Teach Your Children")
    vault.mod.execute_plan(
        vault.cfg,
        vault.conn,
        vault.mod.plan_merge(vault.cfg, vault.conn, "Crosby", "Crosby, Stills, Nash & Young"),
    )
    row = _row(vault, rid)
    assert row["genre"] == "Folk Rock"
    assert "/Folk Rock/Crosby, Stills, Nash & Young/Deja Vu/" in row["file_path"]
    assert not (vault.cfg.alac_library / "Hip Hop").exists()


def test_a_target_with_no_rows_keeps_each_rows_own_genre(vault):
    rid = vault.add("Grover Washington", "Jazz", "Winelight", "Just the Two of Us")
    vault.mod.execute_plan(
        vault.cfg,
        vault.conn,
        vault.mod.plan_merge(vault.cfg, vault.conn, "Grover Washington", "Grover Washington, Jr."),
    )
    row = _row(vault, rid)
    assert row["genre"] == "Jazz"
    assert row["artist"] == "Grover Washington, Jr."
    # The folder drops the trailing dot, as every library folder does
    # (Hank Williams Jr, Run-D.M.C) -- but the suffix itself must not be split off.
    assert "/Jazz/Grover Washington, Jr/Winelight/" in row["file_path"]


def test_same_name_with_genre_refiles_in_place(vault):
    rid = vault.add("Delerium", "Electronic", "Karma", "Silence")
    vault.mod.execute_plan(
        vault.cfg,
        vault.conn,
        vault.mod.plan_merge(vault.cfg, vault.conn, "Delerium", "Delerium", "Celtic"),
    )
    row = _row(vault, rid)
    assert row["genre"] == "Celtic" and "/Celtic/Delerium/Karma/" in row["file_path"]


@pytest.mark.parametrize("same_bytes", [False, True])
def test_a_clash_refuses_the_whole_merge_and_deletes_nothing(vault, same_bytes):
    vault.add("Simon & Garfunkel", "Folk", "Bookends", "America", data=b"A")
    clean = vault.add("Simon", "Folk", "Bookends", "At the Zoo")
    clash = vault.add("Simon", "Folk", "Bookends", "America", data=b"A" if same_bytes else b"B")
    before = {p for p in vault.libs.rglob("*") if p.is_file()}

    plan = vault.mod.plan_merge(vault.cfg, vault.conn, "Simon", "Simon & Garfunkel")
    assert plan.clashes, "a taken destination must be reported"
    with pytest.raises(RuntimeError):
        vault.mod.execute_plan(vault.cfg, vault.conn, plan)
    assert {p for p in vault.libs.rglob("*") if p.is_file()} == before
    assert _row(vault, clean)["artist"] == "Simon", "no row relabelled when the merge is refused"
    assert _row(vault, clash)["artist"] == "Simon"


def test_a_file_the_catalogue_does_not_know_survives(vault):
    vault.add("Simon & Garfunkel", "Folk", "Bookends", "Mrs. Robinson")
    vault.add("Simon", "Folk", "Bookends", "America")
    art = vault.cfg.alac_library / "Folk/Simon/Bookends/cover.jpg"
    art.write_bytes(b"jpg")
    vault.mod.execute_plan(
        vault.cfg,
        vault.conn,
        vault.mod.plan_merge(vault.cfg, vault.conn, "Simon", "Simon & Garfunkel"),
    )
    assert art.is_file(), "only empty folders may be removed"


def test_rows_outside_the_library_are_relabelled_not_moved(vault):
    q = vault.cfg.vault_root / "Quarantine" / "Simon - Cecilia.m4a"
    q.parent.mkdir(parents=True)
    q.write_bytes(b"q")
    vault.conn.execute(
        "INSERT INTO archive (artist, genre, album, title, file_path, status) VALUES "
        "('Simon','Folk','x','Cecilia',?, 'QUARANTINED')",
        (str(q),),
    )
    vault.conn.commit()
    plan = vault.mod.plan_merge(vault.cfg, vault.conn, "Simon", "Simon & Garfunkel")
    assert plan.relabel_only == 1 and not plan.moves
    vault.mod.execute_plan(vault.cfg, vault.conn, plan)
    assert q.is_file()
    assert (
        vault.conn.execute("SELECT artist FROM archive WHERE file_path=?", (str(q),)).fetchone()[0]
        == "Simon & Garfunkel"
    )


def test_every_move_is_recorded_as_an_event(vault):
    vault.add("Simon", "Folk", "Bookends", "America")
    vault.mod.execute_plan(
        vault.cfg,
        vault.conn,
        vault.mod.plan_merge(vault.cfg, vault.conn, "Simon", "Simon & Garfunkel"),
    )
    n = vault.conn.execute(
        "SELECT COUNT(*) FROM events WHERE event_type='ARTIST_CONSOLIDATED'"
    ).fetchone()[0]
    assert n == 3
