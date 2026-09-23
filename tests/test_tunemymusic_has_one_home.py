"""TuneMyMusic.csv has exactly one home, and it is not inside Libraries/.

By 2026-09-23 there were three copies of the wanted list -- 363 rows in
Libraries/ALAC-Archival (where the program wrote), 520 in MetaData, and 60
in an older export -- each holding songs the others did not. A fourth
location was waiting in scripts/musaeus_purge_multichannel.py, which built
its own path into ALAC_Library instead of asking config. Libraries/ had
already been wiped once and taken the list with it.

So the path is decided in one place, config.tunemymusic_csv_path, and this
test fails if any module spells out its own.
"""

from __future__ import annotations

import ast
from pathlib import Path

from musaeus.config import MusicConfig

REPO = Path(__file__).resolve().parents[1]


def _cfg(tmp_path: Path) -> MusicConfig:
    return MusicConfig(
        vault_root=tmp_path,
        inbox=tmp_path / "INBOX",
        staging=tmp_path / "STAGING",
        quarantine=tmp_path / "QUARANTINE",
        runs_root=tmp_path / "RUNS",
        meta_dir=tmp_path / "MetaData",
        alac_library=tmp_path / "Libraries" / "ALAC_Library",
        db_path=tmp_path / "musaeus.db",
    )


def test_the_list_lives_in_metadata_not_in_libraries(tmp_path):
    cfg = _cfg(tmp_path)
    p = cfg.tunemymusic_csv_path
    assert p.parent == cfg.meta_dir, p
    assert "Libraries" not in p.parts, "Libraries/ is wipeable; the wanted list cannot be rebuilt"


def test_no_module_builds_its_own_path_to_the_list():
    """Only `something / "TuneMyMusic.csv"` counts. A docstring is an
    ast.Constant too, and prose about the file must not trip this."""
    offenders = []
    for root in ("musaeus", "scripts"):
        for f in sorted((REPO / root).rglob("*.py")):
            if f.name == "config.py" and f.parent.name == "musaeus":
                continue
            for node in ast.walk(ast.parse(f.read_text(encoding="utf-8"))):
                if (
                    isinstance(node, ast.BinOp)
                    and isinstance(node.op, ast.Div)
                    and isinstance(node.right, ast.Constant)
                    and isinstance(node.right.value, str)
                    and node.right.value.endswith("TuneMyMusic.csv")
                ):
                    offenders.append(f"{f.relative_to(REPO)}:{node.lineno}")
    assert not offenders, "use config.tunemymusic_csv_path instead:\n  " + "\n  ".join(offenders)
