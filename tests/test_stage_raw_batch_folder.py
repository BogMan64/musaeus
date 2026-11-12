"""scripts/stage_raw_batch.py: a manifest belongs to the folder it lists.

--raw-root replaced a folder written into the script (2026-11-12). A
manifest built for one folder must not be staged for another (local
CodeRabbit on the personal-paths branch).
"""

from __future__ import annotations

import csv
import sys

import scripts.stage_raw_batch as srb


def _manifest(meta, rows):
    meta.mkdir(parents=True, exist_ok=True)
    with (meta / srb.MANIFEST_NAME).open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=["path", "basename", "state", "why"])
        w.writeheader()
        w.writerows(rows)


def test_a_manifest_of_another_folder_is_refused(tmp_path, monkeypatch, capsys):
    a, b = tmp_path / "A", tmp_path / "B"
    b.mkdir()
    row = {"path": str(a / "x.m4a"), "basename": "x.m4a", "state": "pending", "why": ""}
    _manifest(tmp_path / "MetaData", [row])
    monkeypatch.setenv("MUSAEUS_VAULT_ROOT", str(tmp_path))
    monkeypatch.setattr(sys, "argv", ["stage_raw_batch.py", "--raw-root", str(b), "--status"])
    assert srb.main() == 2
    assert "another folder" in capsys.readouterr().out
