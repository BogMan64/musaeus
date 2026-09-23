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
