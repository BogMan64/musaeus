"""A refused write to a locked archive must say so, and must not copy first.

On 6cad046 the rename failed inside change_beside, write_beside caught the
OSError and returned "not written", and the tagger counted it as an error with
no message: `errors: 7,975` and no cause. Worse, the copy beside the file had
already been made and was then thrown away -- a full copy of every master
written for nothing.

Measured 2026-11-25: 7,975 of 7,975 masters carry the immutable flag, and
`os.replace` over one returns EPERM.

Both tests fail on 6cad046 for the right reason: the first says the message
names no cause ("[Errno 1] Operation not permitted"), and the second says a
locked file was changed anyway.
"""

from __future__ import annotations

import errno
import os

import pytest

import musaeus.safe_save as safe_save
from musaeus.safe_save import change_beside


def _refuse(src: object, dst: object) -> None:
    raise PermissionError(errno.EPERM, "Operation not permitted", str(dst))


def test_a_refused_rename_says_the_file_is_locked(tmp_path, monkeypatch):
    target = tmp_path / "Artist - Song.m4a"
    target.write_bytes(b"original")
    monkeypatch.setattr(os, "replace", _refuse)

    try:
        change_beside(target, lambda p: p.write_bytes(b"changed"), suffix=".tagging")
    except (OSError, RuntimeError) as exc:
        message = str(exc)
    else:  # pragma: no cover - the refusal is the point
        pytest.fail("a refused rename did not raise")

    assert "immutable" in message.lower() or "locked" in message.lower()
    assert "chattr" in message
    # The file is as it was, and the half-written copy is gone.
    assert target.read_bytes() == b"original"
    assert not (tmp_path / "Artist - Song.m4a.tagging").exists()


def test_a_locked_file_is_refused_before_any_copy(tmp_path, monkeypatch):
    """No copy of a locked file is made: one failed open, not a wasted copy.

    is_immutable is injected with raising=False so this file also runs against
    6cad046, where the name does not exist yet: there the copy IS made, the
    change succeeds, and the assertions fail -- the behaviour under test.
    """
    target = tmp_path / "Artist - Song.m4a"
    target.write_bytes(b"original")
    monkeypatch.setattr(safe_save, "is_immutable", lambda p: True, raising=False)

    copies: list[object] = []
    monkeypatch.setattr(safe_save, "copy_to_new", lambda *a, **k: copies.append(a))

    with pytest.raises((OSError, RuntimeError)):
        change_beside(target, lambda p: p.write_bytes(b"changed"), suffix=".tagging")

    assert copies == [], "a locked file must not be copied"
    assert target.read_bytes() == b"original"
