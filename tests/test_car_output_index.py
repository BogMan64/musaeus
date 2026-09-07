"""Matching an encoded track back to its archive row was O(n^2).

build_car_library.py's final step reads the artist/title tags of every
STAGED source and looks for a file carrying the same tags in the OUTPUT
directory, to record archive.car_export_path. The lookup used to be
_find_output_by_tags(): a full rglob + ffprobe scan of the WHOLE output
tree, called once per source file.

Modeled against the 2026-09-01 car build (10,103 files each way): roughly
51 million ffprobe subprocess calls, an estimated 213 hours at an
optimistic 15ms each. Measured directly rather than only modeled: a
2026-09-02 re-run meant to populate car_export_path for the whole library
ran for over 16 hours -- well past any idle-throttle contention -- and
never printed a single "matched" line, because it was still inside the
first few thousand sources' inner scans when the machine rebooted. That is
why car_export_path sat at 5 rows through two separate "re-run to fix it"
attempts; the DB-matching step was never algorithmically capable of
finishing on a library this size, throttle or no throttle.

_index_output_by_tags() replaces the per-source scan with one pass over
the output directory -- n ffprobe calls to build a dict, then a dict
lookup per source. O(n) instead of O(n^2).
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts" / "car_library"))
import build_car_library as bcl  # noqa: E402

pytestmark = pytest.mark.skipif(
    not (shutil.which("ffmpeg") and shutil.which("ffprobe")),
    reason="ffmpeg/ffprobe not available",
)


def _tagged(path: Path, artist: str, title: str, seconds: int = 1) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["ffmpeg", "-v", "error", "-f", "lavfi", "-i", f"sine=frequency=440:duration={seconds}",
         "-c:a", "aac", "-metadata", f"artist={artist}", "-metadata", f"title={title}",
         str(path), "-y"],
        check=True, capture_output=True,
    )
    return path


def test_indexes_every_output_file_once(tmp_path: Path) -> None:
    out = tmp_path / "out"
    _tagged(out / "A" / "t1.m4a", "Artist One", "Title One")
    _tagged(out / "B" / "t2.m4a", "Artist Two", "Title Two")

    idx = bcl._index_output_by_tags(out)

    assert idx[("artist one", "title one")].name == "t1.m4a"
    assert idx[("artist two", "title two")].name == "t2.m4a"
    assert len(idx) == 2


def test_a_lookup_miss_is_none(tmp_path: Path) -> None:
    out = tmp_path / "out"
    _tagged(out / "t1.m4a", "Artist One", "Title One")
    idx = bcl._index_output_by_tags(out)
    assert idx.get(("nobody", "nothing")) is None


def test_files_with_no_tags_are_skipped_not_crashed_on(tmp_path: Path) -> None:
    out = tmp_path / "out"
    out.mkdir()
    subprocess.run(
        ["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "sine=frequency=440:duration=1",
         "-c:a", "aac", str(out / "untagged.m4a"), "-y"],
        check=True, capture_output=True,
    )
    idx = bcl._index_output_by_tags(out)
    assert idx == {}


def test_first_occurrence_wins_matching_the_original_linear_scan(tmp_path: Path) -> None:
    """The original _find_output_by_tags returned the first rglob() match.
    Two files can legitimately share (artist, title) -- a re-issue, a
    remaster with identical tags -- and the index must resolve duplicates
    the same deterministic way the old per-query scan did: first seen in
    rglob() enumeration order, not last."""
    out = tmp_path / "out"
    first = _tagged(out / "A" / "first.m4a", "Same Artist", "Same Title")
    _tagged(out / "B" / "second.m4a", "Same Artist", "Same Title")

    def linear_scan(output_dir: Path, artist: str, title: str) -> Path | None:
        for p in output_dir.rglob("*"):
            if not p.is_file():
                continue
            a, t = bcl._read_tags(p)
            if a == artist and t == title:
                return p
        return None

    expected = linear_scan(out, "same artist", "same title")
    idx = bcl._index_output_by_tags(out)
    assert idx[("same artist", "same title")] == expected == first


def test_indexing_is_one_pass_not_a_scan_per_source(tmp_path: Path, monkeypatch) -> None:
    """The whole point. Assert the ffprobe-backed tag reader is called
    O(n) times for n output files, not O(sources x outputs)."""
    out = tmp_path / "out"
    for i in range(5):
        _tagged(out / f"t{i}.m4a", f"Artist {i}", f"Title {i}")

    calls = {"n": 0}
    real_read_tags = bcl._read_tags

    def counting_read_tags(path: Path):
        calls["n"] += 1
        return real_read_tags(path)

    monkeypatch.setattr(bcl, "_read_tags", counting_read_tags)
    bcl._index_output_by_tags(out)

    assert calls["n"] == 5, (
        f"expected exactly one _read_tags call per output file, got {calls['n']}"
    )


# ── The wiring, not just the function ──────────────────────────────────────────


def test_the_index_is_built_once_outside_the_matching_loop() -> None:
    """A unit test on _index_output_by_tags alone cannot catch a caller
    that stops using it. Reverting main()'s call site to rebuild the index
    (or the old per-source scan) inside the `for src in files:` loop would
    silently reintroduce the O(n^2) cost while every test above kept
    passing, because none of them exercise main()'s wiring.

    Walks the AST: finds the `for src in files:` loop and asserts
    `_index_output_by_tags(` is called exactly once, and that the call
    sits OUTSIDE the loop body -- built once, not once per source.
    """
    import ast

    src_path = Path(__file__).resolve().parents[1] / "scripts" / "car_library" / "build_car_library.py"
    tree = ast.parse(src_path.read_text(), filename=str(src_path))

    main_fn = next(
        n for n in ast.walk(tree)
        if isinstance(n, ast.FunctionDef) and n.name == "main"
    )

    index_calls = [
        n for n in ast.walk(main_fn)
        if isinstance(n, ast.Call)
        and isinstance(n.func, ast.Name)
        and n.func.id == "_index_output_by_tags"
    ]
    assert len(index_calls) == 1, (
        f"expected exactly one _index_output_by_tags() call in main(), found {len(index_calls)}"
    )

    src_loop = next(
        n for n in ast.walk(main_fn)
        if isinstance(n, ast.For)
        and isinstance(n.target, ast.Name)
        and n.target.id == "src"
    )
    loop_line_range = range(src_loop.lineno, (src_loop.end_lineno or src_loop.lineno) + 1)
    assert index_calls[0].lineno not in loop_line_range, (
        "_index_output_by_tags() is called inside the per-source loop -- "
        "that is the O(n^2) bug this test exists to prevent"
    )


def test_the_old_per_source_scan_function_is_gone() -> None:
    """_find_output_by_tags did a full rglob+ffprobe rescan of the output
    tree for every source. If it comes back, something reintroduced the
    O(n^2) shape even if _index_output_by_tags also still exists."""
    assert not hasattr(bcl, "_find_output_by_tags")
