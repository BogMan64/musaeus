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


def test_a_genre_grey_approved_in_a_sheet_is_kept(tmp_path):
    """Pre-merge review of #180: review apply now stamps Grey's rulings, and
    --execute cleared every stamp."""
    vault, home = tmp_path / "vault", tmp_path / "home"
    vault.mkdir()
    home.mkdir()
    conn = open_db(vault / "musaeus.db")
    ensure_columns(conn, (("genre_ruled_at", "TEXT"),))
    for n in range(2):
        upsert_archive(conn, {"file_path": f"/m/{n}.m4a", "status": "CATALOGUED", "artist": "A"})
    conn.execute("UPDATE archive SET genre_ruled_at = '2026-09-14 10:00:00'")
    conn.execute(
        "INSERT INTO events (run_id, event_type, file_path, old_value, new_value, stage, note) "
        "VALUES ('r', 'REVIEW_APPLIED', '/m/1.m4a', 'Rock', 'Pop', 'approval', "
        "'genre (missing): 1 row(s)')"
    )
    conn.commit()
    conn.close()

    out = _run(vault, home, "--execute")

    assert "1 stamped row(s)" in out and "1 genre(s) Grey approved" in out
    conn = open_db(vault / "musaeus.db")
    left = dict(conn.execute("SELECT file_path, genre_ruled_at FROM archive").fetchall())
    conn.close()
    assert left == {"/m/0.m4a": None, "/m/1.m4a": "2026-09-14 10:00:00"}
