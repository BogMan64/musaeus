"""FinalizeStage and OrganizeStage must build the SAME library path.

Both stages decide where a track lives. On 2026-09-18 finalize was changed to
file under Genre/Artist/Album and organize was not, so on the very first
rebuild run every file finalize placed correctly was moved straight back out
by organize, on the same run:

    [organize] move  Unsorted/38 Special/Rock & Roll Strategy/38 Special - ...

The result on disk was ALAC_Library/Al Green/Gets Next to You/..., with the
genre level silently stripped. Nothing errored. Both stages reported
`changed=32` and a green tick, because each did exactly what it believed its
job was.

This is the third instance of one shape in two days:
  - the car encoder filed by album-artist while the catalogue filed by artist
  - normalize rewrote the artist tag back to sort form after the migration
  - and this

The guard is not "did someone remember to update both" -- it is that the two
stages call ONE function, and that grepping proves neither has grown a
private copy of the rule.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from musaeus.stages.finalize import genre_folder

_ORGANIZE = Path(__file__).resolve().parents[1] / "musaeus" / "stages" / "organize.py"
_FINALIZE = Path(__file__).resolve().parents[1] / "musaeus" / "stages" / "finalize.py"


class TestBothStagesUseOneRule:
    def test_organize_calls_genre_folder(self):
        src = _ORGANIZE.read_text()
        assert "genre_folder(" in src, (
            "organize.py no longer calls genre_folder -- it has its own idea of "
            "where a track lives, and will fight finalize on every run"
        )

    def test_organize_selects_the_genre_column(self):
        """It cannot file by genre if it never reads one. The query returning
        artist/album/title but not genre is how this broke the first time."""
        src = _ORGANIZE.read_text()
        assert "title, genre" in src or "genre, title" in src, (
            "organize.py's row query does not select `genre`"
        )

    def test_only_one_module_defines_the_rule(self):
        """A second definition is how two stages drift back apart."""
        defs = 0
        for f in (_ORGANIZE, _FINALIZE):
            tree = ast.parse(f.read_text())
            defs += sum(
                1
                for n in ast.walk(tree)
                if isinstance(n, ast.FunctionDef) and n.name == "genre_folder"
            )
        assert defs == 1, f"genre_folder is defined {defs} times; it must be defined once"

    @pytest.mark.parametrize(
        ("genre", "expected"),
        [
            ("Rock", "Rock"),
            ("R&B/Funk/Soul", "R&B-Funk-Soul"),
            (None, "Unsorted"),
            ("", "Unsorted"),
        ],
    )
    def test_the_shared_rule_still_behaves(self, genre, expected):
        assert genre_folder(genre) == expected


# ── Filed once: what finalize places, organize leaves alone ─────────────────
#
# 2026-09-24, the first batch on the fresh vault: finalize put all 389 files
# under "Unsorted/" -- it never SELECTed genre -- and filed credits by its own
# rule (the filing map, the full credit in the file name), so organize moved
# every one of them on the same run. The layout rule above was shared; the
# inputs to it were not.


def _vault(tmp_path):
    from musaeus.config import MusicConfig
    from musaeus.context import RunContext
    from musaeus.db import open_db

    cfg = MusicConfig(
        vault_root=tmp_path,
        inbox=tmp_path / "INBOX",
        staging=tmp_path / "STAGING",
        quarantine=tmp_path / "QUARANTINE",
        runs_root=tmp_path / "RUNS",
        meta_dir=tmp_path / "MetaData",
        alac_library=tmp_path / "ALAC-Library",
        alac_archive=tmp_path / "ALAC-Archival",
        db_path=tmp_path / "musaeus.db",
    )
    cfg.ensure_dirs()
    (cfg.meta_dir / "artist_filing.tsv").write_text(
        "Benny Goodman & His Orchestra\tBenny Goodman\n"
    )
    return RunContext.new(cfg, open_db(cfg.db_path), dry_run=False)


_AWKWARD = [
    ("The Black Eyed Peas", "Elephunk", "Hey Mama", "Hip Hop"),
    ("¥$ & Rich the Kid feat. Playboi Carti", "Unsorted", "Carnival", "Hip Hop"),
    ("Benny Goodman & His Orchestra", "Carnegie Hall", "Sing, Sing, Sing", "Jazz"),
    ("Paul McCartney & Stevie Wonder", "Tug of War", "Ebony and Ivory", "Rock"),
    ("Earth, Wind & Fire", "That's the Way of the World", "Shining Star", "R&B/Funk/Soul"),
    ("Nobody Ruled Yet", "Demo", "First Song", None),
]


def _stage_awkward_rows(ctx):
    from musaeus.db import upsert_archive

    for i, (artist, album, title, genre) in enumerate(_AWKWARD):
        p = ctx.inbox / f"in{i}.m4a"
        p.write_bytes(b"audio %d" % i)
        upsert_archive(
            ctx.conn,
            {
                "file_path": str(p),
                "status": "CATALOGUED",
                "artist": artist,
                "album": album,
                "title": title,
                "genre": genre,
                "audio_hash": f"h{i}",
            },
        )
    ctx.conn.execute(
        "UPDATE archive SET canonicalized_at = datetime('now'), canon_action = 'PASSTHROUGH'"
    )
    ctx.conn.commit()


def test_what_finalize_files_organize_does_not_move(tmp_path):
    from musaeus.stages.finalize import FinalizeStage
    from musaeus.stages.organize import OrganizeStage

    ctx = _vault(tmp_path)
    _stage_awkward_rows(ctx)
    assert FinalizeStage().execute(ctx).success
    placed = {r[0]: r[1] for r in ctx.conn.execute("SELECT id, file_path FROM archive")}

    result = OrganizeStage().execute(ctx)
    after = {r[0]: r[1] for r in ctx.conn.execute("SELECT id, file_path FROM archive")}
    assert after == placed, "organize moved files finalize had just placed"
    assert result.files_changed == 0


def test_finalize_files_under_the_genre_and_honours_the_filing_rulings(tmp_path):
    from musaeus.stages.finalize import FinalizeStage

    ctx = _vault(tmp_path)
    _stage_awkward_rows(ctx)
    FinalizeStage().execute(ctx)
    lib = ctx.config.alac_archive  # finalize files masters
    rel = {
        r[0]: str(Path(r[1]).relative_to(lib))
        for r in ctx.conn.execute("SELECT artist, file_path FROM archive")
    }
    assert rel["The Black Eyed Peas"].startswith("Hip Hop/Black Eyed Peas, The/")
    assert rel["Benny Goodman & His Orchestra"].startswith("Jazz/Benny Goodman/"), (
        "artist_filing.tsv ruling ignored"
    )
    assert rel["Paul McCartney & Stevie Wonder"].startswith("Rock/Paul McCartney/")
    assert rel["Earth, Wind & Fire"].startswith("R&B-Funk-Soul/Earth, Wind & Fire/")
    assert rel["Nobody Ruled Yet"].startswith("Unsorted/"), (
        "no genre is still Unsorted, never 'None'"
    )
