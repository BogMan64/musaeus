"""MUSAEUS_BACKUP_ROOTS holds several folders. On Windows the list uses ";",
because ":" is part of every drive letter ("D:\\Backups")."""

from pathlib import Path

from musaeus import bitrot_repair


def test_windows_drive_letters_survive_the_split(monkeypatch):
    monkeypatch.setattr(bitrot_repair.os, "pathsep", ";")
    monkeypatch.setenv("MUSAEUS_BACKUP_ROOTS", r"D:\NUC;E:\USB2")
    assert bitrot_repair.backup_roots() == [Path(r"D:\NUC"), Path(r"E:\USB2")]


def test_linux_list_still_splits_on_colon(monkeypatch):
    monkeypatch.setattr(bitrot_repair.os, "pathsep", ":")
    monkeypatch.setenv("MUSAEUS_BACKUP_ROOTS", "/mnt/NUC:/media/USB2")
    assert bitrot_repair.backup_roots() == [Path("/mnt/NUC"), Path("/media/USB2")]
