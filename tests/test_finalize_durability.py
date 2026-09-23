"""The copy must be on disk before the original is deleted.

P0-C, reported 2026-09-09.

`_copy_into_library` did:

    shutil.copy2(source, tmp_target)
    if source.stat().st_size != tmp_target.stat().st_size: raise
    tmp_target.rename(target)

and the caller then unlinked the source. `shutil.copy2` returns when the
bytes reach the page cache, not the platter. A power loss anywhere between
the rename and the unlink left a target whose data had never been written
and a source that no longer existed — and for a CONVERTED row, STAGING holds
the **only** copy, so the recording was simply gone. The database, at
`PRAGMA synchronous=NORMAL`, could not be relied on to remember either.

The directory fsync is the half that gets forgotten. Without it the *rename*
can be lost even when the file's own data is safe, leaving the bytes on disk
under a name nothing points to.

**What this does not do, and why.** Verification is still size-only. A
content comparison would mean hashing both sides — roughly 2× file size in
extra reads across ~16,000 files, on the order of a terabyte — and the
failure it guards against, silent corruption inside a single `copy2` on a
checksumming filesystem, is far rarer than the power-loss window that fsync
actually closes. Stated here so the next reader knows it was weighed rather
than missed.
"""

from __future__ import annotations

import ast
from pathlib import Path

_SRC = Path(__file__).resolve().parents[1] / "musaeus" / "stages" / "finalize.py"


#: The function whose ordering this file is about. Named explicitly rather
#: than pattern-matched -- a first draft guessed at the name, found nothing,
#: and reported four failures that were about the test, not the code.
_COPY_FN = "_copy_then_verify_then_swap"


def _copy_fn() -> ast.FunctionDef:
    tree = ast.parse(_SRC.read_text())
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == _COPY_FN:
            return node
    raise AssertionError(
        f"{_COPY_FN} not found in finalize.py -- if it was renamed, this file "
        f"must follow it, because these assertions are about its ordering"
    )


def test_the_copy_is_fsynced() -> None:
    body = ast.unparse(_copy_fn())
    assert "os.fsync" in body, "the copy is not forced to disk before the source is deleted (P0-C)"


def test_the_directory_is_fsynced_too() -> None:
    """Without this the rename itself can be lost."""
    body = ast.unparse(_copy_fn())
    assert "O_RDONLY" in body and body.count("os.fsync") >= 2, (
        "only the file is fsynced; the rename can still be lost"
    )


def test_the_fsync_precedes_the_rename() -> None:
    """Order is the whole point: syncing after the rename protects nothing."""
    body = ast.unparse(_copy_fn())
    assert body.index("os.fsync") < body.index(".rename("), (
        "the file is renamed before it is durable"
    )


def test_the_size_check_survives() -> None:
    """The cheap check that catches a truncated copy must not be lost."""
    body = ast.unparse(_copy_fn())
    assert "size mismatch" in body


def test_the_source_is_never_unlinked_inside_the_copy() -> None:
    """Disposal belongs to the caller, after the DB row is committed.

    If this ever moves inside, the ordering the whole stage depends on is
    gone and no fsync will save it.
    """
    body = ast.unparse(_copy_fn())
    assert "source.unlink" not in body
