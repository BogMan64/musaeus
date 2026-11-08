"""musaeus.file_lock: the same lock behaviour on Linux (flock) and Windows
(LockFileEx). These run in both CI workflows; the Windows job is what proves
the Windows branch."""

from __future__ import annotations

import os
import subprocess
import sys

import pytest

from musaeus.file_lock import process_alive, try_lock, unlock


@pytest.fixture
def lock_file(tmp_path):
    return tmp_path / "x.lock"


def _open(path):
    return open(path, "a+")  # noqa: SIM115 -- closed by the test


def test_an_exclusive_lock_turns_a_second_holder_away(lock_file):
    a, b = _open(lock_file), _open(lock_file)
    try:
        try_lock(a, exclusive=True)
        with pytest.raises(BlockingIOError):
            try_lock(b, exclusive=True)
        with pytest.raises(BlockingIOError):
            try_lock(b, exclusive=False)
    finally:
        a.close()
        b.close()


def test_shared_locks_go_together_and_keep_an_exclusive_out(lock_file):
    a, b, c = _open(lock_file), _open(lock_file), _open(lock_file)
    try:
        try_lock(a, exclusive=False)
        try_lock(b, exclusive=False)
        with pytest.raises(BlockingIOError):
            try_lock(c, exclusive=True)
    finally:
        for f in (a, b, c):
            f.close()


def test_unlock_and_close_both_free_the_lock(lock_file):
    a, b = _open(lock_file), _open(lock_file)
    try:
        try_lock(a, exclusive=True)
        unlock(a)
        try_lock(b, exclusive=True)
        b.close()
        b = _open(lock_file)  # closing freed it too
        try_lock(b, exclusive=True)
    finally:
        a.close()
        b.close()


def test_a_plain_file_number_works_like_a_file_object(lock_file):
    fd = os.open(lock_file, os.O_CREAT | os.O_RDWR, 0o644)
    other = _open(lock_file)
    try:
        try_lock(fd, exclusive=True)
        with pytest.raises(BlockingIOError):
            try_lock(other, exclusive=True)
        unlock(fd)
    finally:
        os.close(fd)
        other.close()


def test_a_locked_file_can_still_be_written_and_read(lock_file):
    """edition_build writes its pid into the file it locks; on Windows a lock
    over the content would make that unreadable to anyone else."""
    holder = open(lock_file, "w")  # noqa: SIM115
    try:
        try_lock(holder, exclusive=True)
        holder.write("4242")
        holder.flush()
        assert lock_file.read_text() == "4242"
    finally:
        holder.close()


def test_our_own_process_is_alive_and_is_not_interrupted_by_asking():
    """On Windows os.kill(pid, 0) sends CTRL_C_EVENT: asking about our own
    pid raised KeyboardInterrupt and stopped the whole suite (2026-11-08)."""
    assert process_alive(os.getpid()) is True


def test_a_finished_process_and_a_nonsense_pid_are_not_alive():
    child = subprocess.Popen([sys.executable, "-c", "pass"])
    child.wait()
    assert process_alive(child.pid) is False
    assert process_alive(0) is False
    assert process_alive(-1) is False


@pytest.mark.skipif(sys.platform == "win32", reason="the POSIX branch")
def test_a_process_we_may_not_signal_is_alive(monkeypatch):
    """CodeRabbit on #290: EPERM means it exists under another user. Read as
    gone, safety.lock would take over a live holder's lock."""

    def kill(pid, sig):
        raise PermissionError(1, "Operation not permitted")

    monkeypatch.setattr(os, "kill", kill)
    assert process_alive(12345) is True
