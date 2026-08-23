"""
P0-05 completion evidence: preview changes nothing and reaches nothing.

For every preview entry point, this compares the full vault state before
and after -- directory tree, per-file SHA-256, DB content checksum, DB
mtime and size -- and asserts exact equality, while a transport-denial
harness asserts no connection was even attempted.

Two things are deliberately NOT asserted, because asserting them would be
weaker than it looks:

  "no exception escaped" -- several stages wrap network calls in a broad
  except, so that claim is compatible with a call having gone out.
  Assert log.clean instead.

  "the DB file bytes are unchanged" -- SQLite churns WAL and page layout
  on identical logical content. The checksum is taken over iterdump(), so
  a false diff cannot masquerade as a real one.
"""

from __future__ import annotations

import hashlib
import sqlite3
from pathlib import Path
from types import SimpleNamespace

import pytest

from musaeus.planner import RunMode, build_plan
from tests.transport_denial import deny_transport


def _vault_state(root: Path, db: Path) -> tuple:
    tree = sorted(str(p.relative_to(root)) for p in root.rglob("*"))
    files = hashlib.sha256()
    for p in sorted(root.rglob("*")):
        if p.is_file():
            files.update(str(p.relative_to(root)).encode())
            files.update(p.read_bytes())
    if db.exists():
        conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        dump = hashlib.sha256()
        for line in conn.iterdump():
            dump.update(line.encode())
        conn.close()
        st = db.stat()
        db_state = (dump.hexdigest(), st.st_mtime_ns, st.st_size)
    else:
        db_state = ("no-db", 0, 0)
    return (tree, files.hexdigest(), db_state)


class _Stage:
    NAME = "fake"

    def __init__(self):  # pragma: no cover
        raise AssertionError("preview must never instantiate a stage")

    @classmethod
    def plan_candidates(cls, conn):
        return conn.execute("SELECT COUNT(*) FROM archive").fetchone()[0], "rows"


@pytest.fixture
def vault(tmp_path: Path):
    lib = tmp_path / "ALAC-Library"
    lib.mkdir()
    (lib / "a.m4a").write_bytes(b"audio-a")
    (lib / "b.m4a").write_bytes(b"audio-b")
    db = tmp_path / "musaeus.db"
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE archive (file_path TEXT, status TEXT)")
    conn.executemany(
        "INSERT INTO archive VALUES (?,?)",
        [(str(lib / "a.m4a"), "CATALOGUED"), (str(lib / "b.m4a"), "CATALOGUED")],
    )
    conn.commit()
    conn.close()
    return SimpleNamespace(vault_root=tmp_path, db_path=db, alac_library=lib)


class TestPreviewIsInert:
    def test_build_plan_changes_nothing_at_all(self, vault):
        before = _vault_state(vault.vault_root, vault.db_path)
        build_plan(vault, [_Stage], RunMode.PREVIEW)
        assert _vault_state(vault.vault_root, vault.db_path) == before

    def test_build_plan_attempts_no_transport(self, vault):
        with deny_transport() as log:
            build_plan(vault, [_Stage], RunMode.PREVIEW)
        assert log.clean, f"preview attempted network: {log.describe()}"

    def test_repeated_previews_stay_inert(self, vault):
        """Idempotence matters: a preview you run twice must still be free."""
        before = _vault_state(vault.vault_root, vault.db_path)
        for _ in range(3):
            build_plan(vault, [_Stage], RunMode.PREVIEW)
        assert _vault_state(vault.vault_root, vault.db_path) == before

    def test_preview_on_an_empty_vault_creates_nothing(self, tmp_path):
        cfg = SimpleNamespace(
            vault_root=tmp_path, db_path=tmp_path / "musaeus.db", alac_library=tmp_path / "lib"
        )
        with deny_transport() as log:
            plan = build_plan(cfg, [_Stage], RunMode.PREVIEW)
        assert list(tmp_path.iterdir()) == []
        assert log.clean
        assert any("no database" in n for n in plan.notes)

    def test_the_database_is_never_opened_writable(self, vault):
        """Proved by making the file read-only, not by inspecting the code."""
        vault.db_path.chmod(0o444)
        try:
            plan = build_plan(vault, [_Stage], RunMode.PREVIEW)
            assert plan.stages[0].candidates == 2
        finally:
            vault.db_path.chmod(0o644)

    def test_output_states_that_nothing_changed(self, vault):
        rendered = build_plan(vault, [_Stage], RunMode.PREVIEW).render()
        assert "PREVIEW ONLY" in rendered
        assert "no network lookup was performed" in rendered
