"""consolidate_artist_folders.merge_tree deletes a source file only when the
target is the SAME FILE -- not merely the same name and the same size.

It used to decide "same file filed twice" on byte size alone. Two different
recordings with one name in one album folder and an identical byte count are
rare, but the cost of the rare case is deleting a recording that exists
nowhere else, and this codebase has already met filename-and-size matches that
were different audio. A byte comparison costs a read and settles it.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "consolidate_artist_folders.py"


def _load():
    spec = importlib.util.spec_from_file_location("consolidate_artist_folders", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _put(root: Path, rel: str, data: bytes) -> Path:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(data)
    return p


def test_same_name_same_size_different_bytes_is_kept_and_reported(tmp_path):
    src, dst = tmp_path / "John Cougar Mellencamp", tmp_path / "John Mellencamp"
    f = _put(src, "Scarecrow/Rain on the Scarecrow.m4a", b"A" * 64)
    _put(dst, "Scarecrow/Rain on the Scarecrow.m4a", b"B" * 64)
    _, clashes = _load().merge_tree(src, dst, execute=True)
    assert f.exists(), "a different recording was deleted because its size matched"
    assert "Scarecrow/Rain on the Scarecrow.m4a" in clashes


def test_a_true_duplicate_is_still_removed(tmp_path):
    src, dst = tmp_path / "John Cougar Mellencamp", tmp_path / "John Mellencamp"
    f = _put(src, "Scarecrow/Small Town.m4a", b"same bytes")
    _put(dst, "Scarecrow/Small Town.m4a", b"same bytes")
    _, clashes = _load().merge_tree(src, dst, execute=True)
    assert not f.exists(), "an identical copy filed twice should go"
    assert clashes == []
