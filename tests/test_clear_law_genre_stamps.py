"""scripts/clear_law_genre_stamps.py: dry run changes nothing; --execute clears
every stamp and leaves one event per row (Grey, 2026-10-11)."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from musaeus.db import ensure_columns, open_db, upsert_archive

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "clear_law_genre_stamps.py"


def _run(vault: Path, home: Path, *args: str) -> str:
    env = dict(os.environ, HOME=str(home), MUSAEUS_VAULT_ROOT=str(vault))
    out = subprocess.run(
        [sys.executable, str(SCRIPT), *args], env=env, capture_output=True, text=True, check=True
    )
    return out.stdout


def test_dry_run_then_execute(tmp_path):
    vault, home = tmp_path / "vault", tmp_path / "home"
    vault.mkdir()
    home.mkdir()
    conn = open_db(vault / "musaeus.db")
    ensure_columns(conn, (("genre_ruled_at", "TEXT"),))
    for n in range(3):
        upsert_archive(conn, {"file_path": f"/m/{n}.m4a", "status": "CATALOGUED", "artist": "A"})
    conn.execute("UPDATE archive SET genre_ruled_at = '2026-09-14 10:00:00'")
    conn.commit()
    conn.close()

    assert "3 stamped row(s)" in _run(vault, home)
    conn = open_db(vault / "musaeus.db")
    assert (
        conn.execute("SELECT COUNT(*) FROM archive WHERE genre_ruled_at IS NULL").fetchone()[0] == 0
    )
    conn.close()

    _run(vault, home, "--execute")
    conn = open_db(vault / "musaeus.db")
    assert (
        conn.execute("SELECT COUNT(*) FROM archive WHERE genre_ruled_at IS NULL").fetchone()[0] == 3
    )
    events = conn.execute(
        "SELECT COUNT(*) FROM events WHERE event_type = 'GENRE_RULING_CLEARED'"
    ).fetchone()[0]
    assert events == 3
    conn.close()
