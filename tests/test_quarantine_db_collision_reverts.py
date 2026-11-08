"""A UNIQUE collision must not leave a file moved and a row pointing elsewhere.

P0-D, reported 2026-09-09.

`archive.file_path` is UNIQUE. Every stage that moves a file then updates the
row can therefore raise `sqlite3.IntegrityError` from that UPDATE, when some
other row already holds the destination path.

`IntegrityError` is **not** an `OSError`. Both stages here wrapped the move in
`except OSError`, so the exception escaped mid-loop with the file already
moved: disk and database disagreeing, the stage aborted, and — because
`TributeQuarantineStage` writes its manifest only after the loop and
`CorruptStage` writes none at all — no restore artefact for the files already
moved.

Three stages guarded it (canonicalize, finalize, organize) and three did not.
Of the three, `classical_composer` turned out safe for a different reason — it
updates the row *before* moving, so a collision happens while the file is
still in place — and `dupe_resolver` has since been rewritten around a
SAVEPOINT, which is stronger than catching the error. That left these two.

The fix follows `organize.py`'s existing pattern: catch, revert the move, and
if the revert also fails, say so loudly with both paths rather than continuing
quietly.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest


def test_integrity_error_is_not_an_oserror() -> None:
    """The premise. If this ever changes, both guards become redundant."""
    assert not issubclass(sqlite3.IntegrityError, OSError)


@pytest.mark.parametrize(
    "module,marker",
    [
        ("musaeus/stages/tribute_quarantine.py", "TRIBUTE_REVIEW"),
        ("musaeus/stages/corrupt.py", "QUARANTINED"),
    ],
)
def test_the_post_move_update_is_guarded(module: str, marker: str) -> None:
    """Structural: the UPDATE that follows a move must catch IntegrityError.

    Asserted on the shipped source because reaching the branch for real needs
    two rows contending for one destination path inside a live stage run;
    what matters is that the guard exists and names the right exception.
    """
    text = (Path(__file__).resolve().parents[1] / module).read_text()
    assert marker in text, f"{module} no longer performs this update"
    assert "sqlite3.IntegrityError" in text, (
        f"{module} moves a file then updates file_path with no IntegrityError "
        f"guard -- a UNIQUE collision would abort the stage with the file "
        f"already moved (P0-D)"
    )


@pytest.mark.parametrize(
    "module",
    [
        "musaeus/stages/tribute_quarantine.py",
        "musaeus/stages/corrupt.py",
    ],
)
def test_the_guard_attempts_a_revert(module: str) -> None:
    """Catching without reverting would still leave disk and DB disagreeing."""
    text = (Path(__file__).resolve().parents[1] / module).read_text()
    block = text.split("sqlite3.IntegrityError")[1][:900]
    assert "shutil.move" in block, f"{module} catches the collision but never reverts"
    assert "COULD NOT REVERT" in block, (
        f"{module} reverts but says nothing when the revert itself fails -- "
        f"that is the case a human has to fix by hand"
    )


@pytest.mark.parametrize(
    "module",
    [
        "musaeus/stages/tribute_quarantine.py",
        "musaeus/stages/corrupt.py",
    ],
)
def test_the_stage_continues_rather_than_aborting(module: str) -> None:
    """One collision must cost one file, not the rest of the run."""
    text = (Path(__file__).resolve().parents[1] / module).read_text()
    # From the guard to the next `except` at the same nesting, rather than a
    # fixed character window -- a first draft used 900 characters and reported
    # corrupt.py as unguarded because its counter sits at 1,050. A test that
    # reports on the size of its own window is the recurring mistake in this
    # repository, not a finding.
    after = text.split("sqlite3.IntegrityError", 1)[1]
    block = after.split("\n                    except ")[0].split("\n            except ")[0]
    assert "files_errored" in block, f"{module} does not count the failure"


def test_every_stage_that_moves_then_updates_is_now_guarded() -> None:
    """The sweep that found P0-D, kept so a new stage cannot reintroduce it.

    A stage that calls shutil.move and then writes file_path must name
    IntegrityError somewhere. classical_composer is exempt by ordering: it
    updates the row first, so a collision fires before anything moves.
    """
    root = Path(__file__).resolve().parents[1] / "musaeus" / "stages"
    offenders = []
    for path in sorted(root.glob("*.py")):
        text = path.read_text()
        if "shutil.move" not in text or "file_path" not in text:
            continue
        if "SAVEPOINT" in text or "sqlite3.IntegrityError" in text:
            continue
        move_at = text.index("shutil.move")
        update_after = text.find("UPDATE archive SET", move_at)
        if update_after != -1:
            offenders.append(path.name)
    assert not offenders, (
        "these stages move a file then update file_path with no IntegrityError "
        f"guard and no savepoint: {offenders}"
    )
