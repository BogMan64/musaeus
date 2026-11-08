"""The scope classifier on Windows paths (Windows port, 2026-11-08).

Only the temp folders hold disposable fixtures; everything else may be
someone's music. These check the Windows rules on normcase'd text (lower
case, backslashes), so they run on Linux too; the Windows CI job runs the
whole classifier through them for real.
"""

from __future__ import annotations

import sys

import pytest

from musaeus.safety.lock import FIXTURE, REJECTED, Scope, _windows_in_temp, _windows_rejected

USER_TEMP = r"c:\users\bob\appdata\local\temp"


@pytest.mark.parametrize(
    "path",
    [
        USER_TEMP + r"\pytest-of-bob\pytest-0\test_x0",
        r"d:\windows\temp\job",
        USER_TEMP + r"\x",
    ],
)
def test_under_a_windows_temp_folder_is_disposable(path):
    assert _windows_in_temp(path)
    assert not _windows_rejected(path)


@pytest.mark.parametrize(
    "path",
    [
        r"c:\users\bob\music\library",
        r"c:\users\bob\appdata\local\temperature\x",  # a longer name, not the folder
        r"c:\users\bob\appdata\local\temp2\x",
        r"c:\users\bob\appdata\roaming\temp\x",
        r"c:\temp\x",  # a folder someone made: may hold music
        r"\\server\share\temp\x",
    ],
)
def test_anything_else_is_not(path):
    assert not _windows_in_temp(path)


@pytest.mark.parametrize(
    "path", [r"c:" + "\\", r"d:" + "\\", r"c:\users", USER_TEMP, r"c:\windows\temp"]
)
def test_a_drive_the_users_folder_or_a_temp_folder_itself_is_refused(path):
    assert _windows_rejected(path)


@pytest.mark.skipif(sys.platform != "win32", reason="real Windows paths")
@pytest.mark.parametrize("root", [r"C:\Windows\Temp", r"C:\Users\bob\AppData\Local\Temp"])
def test_a_temp_folder_itself_declared_a_fixture_is_still_refused(root):
    """A fixture declaration kept the temp folder itself lockable (CodeRabbit, #293)."""
    assert Scope.build(root, "library", classification=FIXTURE).classification == REJECTED
