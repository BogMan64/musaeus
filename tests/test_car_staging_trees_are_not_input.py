"""A staging tree is this script's own bookkeeping, not somebody's dropped audio.

M-14 in the Repair Register, 2026-09-08 — raised as PLAUSIBLE, confirmed here
with numbers.

`--from-catalogue` symlinks every catalogued master into
`RUNS/AAC-Car-Masked/_staged_<pid>/`. That tree was cleaned only on entry
(guarded by `exists()`, which a fresh PID never satisfies) and after a dry
run. **A successful build left it in place for ever**, and the PID-keyed name
meant no later run cleaned it either.

`find_input_files()` excluded `_output` and nothing else. So a later run
*without* `--from-catalogue` walked into every leaked tree and re-ingested the
whole catalogue as though a human had dropped it there — `rglob` follows a
symlink to a file as a file.

Measured on the live vault the day this was fixed:

    leaked _staged_<pid> trees      3   (one from that same day's build)
    symlinks inside them       41,811
    genuine hand-dropped files      0
    files the script reported  41,031   against a library of ~16,000

It was found by *verifying a different fix*: M-05's dry-run guard printed the
41,031 while being tested. Before M-05, that same command would have encoded
them.

The repair has two halves and needs both:

- **discovery** — never walk into a `_staged_*` tree, whoever left it. This
  is the durable half: it holds for trees left by older versions, other PIDs,
  and runs that died before cleaning up.
- **cleanup** — remove *this* run's tree however the run ends. Only this
  PID's: the per-process naming exists because a shared `_staged` let one run
  delete the symlinks another was reading (2026-09-01, a dry run pulled the
  staging out from under a live build at file 4,860). Sweeping other PIDs'
  trees here would reintroduce exactly that.
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts" / "car_library"))
from build_car_library import find_input_files  # noqa: E402

_SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "car_library" / "build_car_library.py"


def _drop(root: Path, rel: str) -> Path:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(b"\x00" * 32)
    return p


def test_a_leaked_staging_tree_is_not_input(tmp_path: Path) -> None:
    """The finding."""
    _drop(tmp_path, "_staged_12345/Artist/Album/Artist - Song.m4a")
    _drop(tmp_path, "_staged_999/Other/Album/Other - Thing.m4a")
    assert find_input_files(tmp_path) == []


def test_symlinks_inside_a_staging_tree_are_not_input(tmp_path: Path) -> None:
    """The real trees hold symlinks, and rglob follows them to files."""
    real = _drop(tmp_path, "masters/Artist - Song.m4a")
    staged = tmp_path / "_staged_777" / "Artist" / "Album"
    staged.mkdir(parents=True)
    (staged / "Artist - Song.m4a").symlink_to(real)

    found = find_input_files(tmp_path)
    assert all("_staged_777" not in str(f) for f in found), found


def test_genuine_hand_dropped_audio_is_still_found(tmp_path: Path) -> None:
    """The fix must not make the hand-dropped mode useless."""
    a = _drop(tmp_path, "Some Artist - A Song.m4a")
    b = _drop(tmp_path, "subdir/Another - Track.flac")
    _drop(tmp_path, "_staged_1/Ignored/Ignored - X.m4a")

    found = set(find_input_files(tmp_path))
    assert found == {a, b}


def test_the_output_directory_is_still_excluded(tmp_path: Path) -> None:
    """The exclusion that already existed must survive the rewrite."""
    _drop(tmp_path, "_output/encoded/Artist/Artist - Song.m4a")
    keep = _drop(tmp_path, "Artist - Song.m4a")
    assert find_input_files(tmp_path) == [keep]


def test_a_directory_merely_starting_with_underscore_is_still_input(tmp_path: Path) -> None:
    """Exclude the two known names, not every underscore.

    Someone's folder called `_new` or `_to sort` is dropped audio and must
    still be picked up; over-broad exclusion would silently skip it.
    """
    keep = _drop(tmp_path, "_to sort/Artist - Song.m4a")
    assert find_input_files(tmp_path) == [keep]


def test_this_run_registers_its_own_cleanup(tmp_path: Path) -> None:
    """Structural: the success path must clean up, not only the dry-run path.

    Asserted on the shipped source because the alternative is a full
    catalogue build against a real vault. What matters is that an
    atexit-style cleanup is registered for the staging directory at all --
    before the fix, the only rmtree calls were on entry and after a dry run.
    """
    text = _SCRIPT.read_text()
    assert "atexit.register(shutil.rmtree, staging_dir" in text, (
        "this run's staging tree is not cleaned up on exit"
    )

    tree = ast.parse(text)
    assert any(
        isinstance(n, ast.Import) and any(a.name == "atexit" for a in n.names)
        for n in ast.walk(tree)
    ), "atexit is used but not imported"


def test_the_cleanup_is_scoped_to_this_process(tmp_path: Path) -> None:
    """Sweeping other PIDs' trees would recreate the 2026-09-01 incident.

    A dry run deleted the staging a live build was reading from, and the
    encoder spent 3,713 files reporting "No such file or directory". The
    per-process name is the guard; this pins that the cleanup respects it.
    """
    text = _SCRIPT.read_text()
    reg = text.split("atexit.register(shutil.rmtree,")[1].split(")")[0]
    assert "staging_dir" in reg, reg
    for wildcard in ("_staged_*", "glob", "iterdir"):
        assert wildcard not in reg, f"cleanup must target this PID's tree only, found {wildcard!r}"
