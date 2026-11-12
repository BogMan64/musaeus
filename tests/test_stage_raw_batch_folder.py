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


def test_a_missing_folder_leaves_the_manifest_alone(tmp_path, monkeypatch):
    row = {
        "path": str(tmp_path / "A" / "x.m4a"),
        "basename": "x.m4a",
        "state": "missing",
        "why": "",
    }
    _manifest(tmp_path / "MetaData", [row])
    before = (tmp_path / "MetaData" / srb.MANIFEST_NAME).read_text(encoding="utf-8")
    monkeypatch.setenv("MUSAEUS_VAULT_ROOT", str(tmp_path))
    gone = str(tmp_path / "gone")
    monkeypatch.setattr(
        sys, "argv", ["stage_raw_batch.py", "--raw-root", gone, "--rebuild-manifest"]
    )
    assert srb.main() == 2
    assert (tmp_path / "MetaData" / srb.MANIFEST_NAME).read_text(encoding="utf-8") == before


def test_a_settled_outcome_is_kept_by_full_path_not_name(tmp_path, monkeypatch):
    from musaeus.config import MusicConfig
    from musaeus.db import open_db

    raw = tmp_path / "raw"
    for sub in ("A", "B"):
        (raw / sub).mkdir(parents=True)
        (raw / sub / "x.m4a").write_bytes(b"x")
    monkeypatch.setenv("MUSAEUS_VAULT_ROOT", str(tmp_path))
    cfg = MusicConfig.from_env()
    open_db(cfg.db_path).close()
    settled = {
        "path": str(raw / "A" / "x.m4a"),
        "basename": "x.m4a",
        "state": "verified-new",
        "why": "copied",
    }
    _manifest(cfg.meta_dir, [settled])
    rows = srb.build_manifest(cfg, cfg.meta_dir / srb.MANIFEST_NAME, raw)
    states = {r["path"]: r["state"] for r in rows}
    assert states == {str(raw / "A" / "x.m4a"): "verified-new", str(raw / "B" / "x.m4a"): "pending"}
