"""scripts/musaeus_close_hand_deleted_rows.py refuses a missing database.

sqlite3.connect() makes an empty file where none is. A missing hash_index.db
was silently created blank, and the script then wrote its denies into it
instead of stopping (Codex on #322).
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

from musaeus.db import open_db

REPO = Path(__file__).resolve().parents[1]


def _script(tmp_path, monkeypatch):
    from musaeus import config

    monkeypatch.setenv("MUSAEUS_VAULT_ROOT", str(tmp_path))
    monkeypatch.setattr(config, "_cached_config", None)  # its folders come from this vault
    path = REPO / "scripts" / "musaeus_close_hand_deleted_rows.py"
    spec = importlib.util.spec_from_file_location("close_hand_deleted_rows", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_a_missing_ledger_is_refused_not_created(tmp_path, monkeypatch):
    script = _script(tmp_path, monkeypatch)
    open_db(script.DB).close()
    script.BACKUPS.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(sys, "argv", ["musaeus_close_hand_deleted_rows.py", "--execute"])
    with pytest.raises(SystemExit):
        script.main()
    assert not script.HASH_INDEX.exists()
    assert not list(script.BACKUPS.glob("*_pre_*.db")), "a backup was made before refusing"
