"""consolidate_artist_folders merges an artist in a Genre/Artist/Album library.

It used to find the old artist by walking `Libraries/<tier>/<artist folder>`.
After the library gained its genre level that walk found nothing: a dry run
of "Simon" -> "Simon & Garfunkel" on 2026-09-24 reported 0 files in both ALAC
tiers while 17 sat in Folk/Simon, and --execute would have relabelled every
row and moved none of the files.

The same failure came back on 2026-10-03. Since 2026-09-25 a row names its
MASTER in ALAC-Archival, and ALAC_Library holds the Lossless edition -- copies
no row may point at. The script still looked for each row under ALAC_Library,
so a dry run of "Derek" -> "Derek & The Dominos" reported "0 to move, 2
outside the library, relabel only": --execute would have renamed the artist in
the catalogue and left every master in the old folder.

These tests build a small vault in today's layout -- the row names its master,
the editions hold copies the ledger knows -- and hold the script to: the
master the row names is the file that moves, every destination comes from the
rule organize files by, edition copies are left for edition-build to move
after their master, a clash refuses the whole merge, and nothing is deleted
that the catalogue does not know about.
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
def vault(tmp_path):
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
        "title TEXT, file_path TEXT, car_export_path TEXT, mb_artist_name TEXT, mb_artist_id TEXT, status TEXT)"
    )
    conn.execute("CREATE TABLE archive_tier_hashes (path TEXT PRIMARY KEY, sha256 TEXT)")
    conn.execute(
        "CREATE TABLE duplicates (id INTEGER PRIMARY KEY, group_id INTEGER, file_path TEXT, status TEXT)"
    )
    conn.execute(
        "CREATE TABLE events (id INTEGER PRIMARY KEY, run_id TEXT, ts TEXT, event_type TEXT, "
        "file_path TEXT, old_value TEXT, new_value TEXT, stage TEXT, note TEXT)"
    )
    mod = _load()

    def add(artist, genre, album, title, *, editions=True, data=b"audio", mb=None, mbid=None):
        """A catalogued master, and (editions=True) its Lossless and car copies."""
        rel = library_relpath(artist, mb, genre, album, title, ".m4a")
        master = cfg.alac_archive / rel
        master.parent.mkdir(parents=True, exist_ok=True)
        master.write_bytes(data)
        if editions:
            for copy in (cfg.alac_library / rel, libs / "CAR_Library" / Path(*rel.parts[1:])):
                copy.parent.mkdir(parents=True, exist_ok=True)
                copy.write_bytes(b"copy of " + data)
        cur = conn.execute(
            "INSERT INTO archive (artist, genre, album, title, file_path, "
            "mb_artist_name, mb_artist_id, status) VALUES (?,?,?,?,?,?,?, 'CATALOGUED')",
            (artist, genre, album, title, str(master), mb, mbid),
        )
        conn.commit()
        return cur.lastrowid

    return SimpleNamespace(cfg=cfg, conn=conn, mod=mod, add=add, libs=libs)


def _row(v, rid):
    return v.conn.execute("SELECT * FROM archive WHERE id=?", (rid,)).fetchone()


def _edition_files(v):
    return {
        p: p.read_bytes()
        for root in (v.cfg.alac_library, v.libs / "CAR_Library")
        for p in root.rglob("*")
        if p.is_file()
    }


def test_moves_the_master_the_row_names(vault):
    vault.add("Simon & Garfunkel", "Folk", "Bookends", "Mrs. Robinson")
    rid = vault.add("Simon", "Folk", "Bookends", "America")

    plan = vault.mod.plan_merge(vault.cfg, vault.conn, "Simon", "Simon & Garfunkel")
    assert plan.relabel_only == 0, "a row naming its master is in the library"
    assert len(plan.moves) == 1, "the master is the one file the row names"
    vault.mod.execute_plan(vault.cfg, vault.conn, plan)

    want = (
        vault.cfg.alac_archive / "Folk/Simon & Garfunkel/Bookends/Simon & Garfunkel - America.m4a"
    )
    assert want.is_file()
    row = _row(vault, rid)
    assert (row["artist"], row["file_path"]) == ("Simon & Garfunkel", str(want))
    assert not (vault.cfg.alac_archive / "Folk/Simon").exists(), "emptied folder should go"


def test_edition_copies_are_left_for_edition_build(vault):
    """Copies follow their master on the next edition-build, which knows them
    by the master's audio and moves only a file carrying its marker. Moving a
    copy here would leave the ledger naming a path that is empty."""
    vault.add("Simon", "Folk", "Bookends", "America")
    before = _edition_files(vault)
    vault.mod.execute_plan(
        vault.cfg,
        vault.conn,
        vault.mod.plan_merge(vault.cfg, vault.conn, "Simon", "Simon & Garfunkel"),
    )
    assert _edition_files(vault) == before


def test_a_row_pointing_into_the_lossless_edition_refuses_the_merge(vault):
    """No row may name an edition copy (Grey, 2026-09-25); the audit fails one.
    Moving it would move a copy as if it were a master."""
    vault.add("Simon", "Folk", "Bookends", "America")
    rid = vault.add("Simon", "Folk", "Bookends", "Cecilia")
    copy = vault.cfg.alac_library / Path(_row(vault, rid)["file_path"]).relative_to(
        vault.cfg.alac_archive
    )
    vault.conn.execute("UPDATE archive SET file_path=? WHERE id=?", (str(copy), rid))
    vault.conn.commit()

    plan = vault.mod.plan_merge(vault.cfg, vault.conn, "Simon", "Simon & Garfunkel")
    assert any("Lossless edition" in c for c in plan.clashes)
    with pytest.raises(RuntimeError):
        vault.mod.execute_plan(vault.cfg, vault.conn, plan)
    assert _row(vault, rid)["artist"] == "Simon"


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
        assert Path(r["file_path"]) == vault.cfg.alac_archive / rel


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
    assert not (vault.cfg.alac_archive / "Hip Hop").exists()


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
    art = vault.cfg.alac_archive / "Folk/Simon/Bookends/cover.jpg"
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
    assert n == 1


def test_a_merged_row_takes_the_targets_identity_and_files_with_it(vault):
    """England Dan Seals, no MusicBrainz name, merged into the duo whose rows
    carry theirs: without adopting it, folder_artist split the merged row off
    into "England Dan" while the duo stayed whole -- and organize, reading the
    same field, would have kept them apart on every pass."""
    duo = "England Dan & John Ford Coley"
    kept = vault.add(
        duo, "Rock N'Roll", "Nights Are Forever", "Nights Are Forever", mb=duo, mbid="2bca31d7"
    )
    rid = vault.add(
        "England Dan Seals",
        "Rock N'Roll",
        "Nights Are Forever",
        "I'd Really Love to See You Tonight",
    )
    vault.mod.execute_plan(
        vault.cfg, vault.conn, vault.mod.plan_merge(vault.cfg, vault.conn, "England Dan Seals", duo)
    )
    row, other = _row(vault, rid), _row(vault, kept)
    assert (row["mb_artist_name"], row["mb_artist_id"]) == (duo, "2bca31d7")
    assert Path(row["file_path"]).parent == Path(other["file_path"]).parent, (
        "merged row filed apart from its artist"
    )
    rel = library_relpath(
        row["artist"], row["mb_artist_name"], row["genre"], row["album"], row["title"], ".m4a"
    )
    assert Path(row["file_path"]) == vault.cfg.alac_archive / rel


def test_an_identity_that_names_someone_else_is_not_adopted(vault):
    """In a batch, "Run-D.M.C. & Aerosmith" merged first gives "Run-D.M.C." its
    first row -- carrying the collab's MB identity. The next merge into
    "Run-D.M.C." must not hand that identity to a plain Run-D.M.C. track."""
    vault.add(
        "Run-D.M.C.",
        "Hip Hop",
        "Walk This Way",
        "Walk This Way",
        mb="Run-D.M.C. & Aerosmith",
        mbid="collab",
    )
    rid = vault.add(
        "Run-DMC", "Hip Hop", "Raising Hell", "Peter Piper", mb="Run-DMC", mbid="rundmc"
    )
    vault.mod.execute_plan(
        vault.cfg, vault.conn, vault.mod.plan_merge(vault.cfg, vault.conn, "Run-DMC", "Run-D.M.C.")
    )
    row = _row(vault, rid)
    assert (row["mb_artist_name"], row["mb_artist_id"]) == ("Run-DMC", "rundmc")


def test_and_versus_ampersand_is_still_the_same_identity(vault):
    vault.add(
        "KC & The Sunshine Band",
        "Disco",
        "KC",
        "Get Down Tonight",
        mb="KC and the Sunshine Band",
        mbid="kc",
    )
    rid = vault.add("KC (the Sunshine Band)", "Disco", "KC", "That's the Way")
    vault.mod.execute_plan(
        vault.cfg,
        vault.conn,
        vault.mod.plan_merge(
            vault.cfg, vault.conn, "KC (the Sunshine Band)", "KC & The Sunshine Band"
        ),
    )
    assert _row(vault, rid)["mb_artist_id"] == "kc"


def test_other_tables_naming_a_moved_file_follow_it(vault):
    rid = vault.add("Simon", "Folk", "Bookends", "America")
    master = _row(vault, rid)["file_path"]
    vault.conn.execute("INSERT INTO archive_tier_hashes VALUES (?, 'abc')", (master,))
    vault.conn.execute(
        "INSERT INTO duplicates (group_id, file_path, status) VALUES (7, ?, 'keep')", (master,)
    )
    vault.conn.commit()
    vault.mod.execute_plan(
        vault.cfg,
        vault.conn,
        vault.mod.plan_merge(vault.cfg, vault.conn, "Simon", "Simon & Garfunkel"),
    )
    new_master = _row(vault, rid)["file_path"]
    assert new_master != master
    assert vault.conn.execute("SELECT path FROM archive_tier_hashes").fetchone()[0] == new_master
    assert vault.conn.execute("SELECT file_path FROM duplicates").fetchone()[0] == new_master


def test_a_database_failure_puts_the_files_back(vault, monkeypatch):
    rid = vault.add("Simon", "Folk", "Bookends", "America")
    before = {p for p in vault.libs.rglob("*") if p.is_file()}

    def boom(*a, **k):
        raise sqlite3.OperationalError("disk I/O error")

    monkeypatch.setattr(vault.mod, "_record_row", boom)
    with pytest.raises(sqlite3.OperationalError):
        vault.mod.execute_plan(
            vault.cfg,
            vault.conn,
            vault.mod.plan_merge(vault.cfg, vault.conn, "Simon", "Simon & Garfunkel"),
        )
    assert {p for p in vault.libs.rglob("*") if p.is_file()} == before
    assert _row(vault, rid)["artist"] == "Simon"


def test_keep_both_files_a_clash_as_n_and_touches_nothing_else(vault):
    kept = vault.add("Steve Miller Band", "Rock", "Born 2B Blue", "Ya Ya", data=b"band")
    rid = vault.add("Steve Miller", "Rock", "Born 2B Blue", "Ya Ya", data=b"solo")
    kept_path = Path(_row(vault, kept)["file_path"])

    plan = vault.mod.plan_merge(
        vault.cfg, vault.conn, "Steve Miller", "Steve Miller Band", keep_both=True
    )
    assert not plan.clashes
    vault.mod.execute_plan(vault.cfg, vault.conn, plan)

    row = Path(_row(vault, rid)["file_path"])
    assert row.name == "Steve Miller Band - Ya Ya (2).m4a"
    assert row.parent == kept_path.parent
    assert row.read_bytes() == b"solo"
    assert kept_path.read_bytes() == b"band", "the master already there must be untouched"


def test_keep_both_still_refuses_a_missing_source(vault):
    rid = vault.add("Simon", "Folk", "Bookends", "America")
    Path(_row(vault, rid)["file_path"]).unlink()
    plan = vault.mod.plan_merge(vault.cfg, vault.conn, "Simon", "Simon & Garfunkel", keep_both=True)
    assert any("source missing" in c for c in plan.clashes)
