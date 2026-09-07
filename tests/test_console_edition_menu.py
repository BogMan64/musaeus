"""The console's "Build an Edition" entry.

The selection layer existed only as a CLI flag combination. That is fine
while someone is here to remember the syntax, and useless later -- the
console is how this pipeline is actually driven, and an edition you cannot
reach from it is an edition that does not get built.

Selection only. It reports what WOULD be built and what it would cost,
because the expensive half is the encode: the full Car edition is ~44
hours, so "what would I get?" has to be answerable before committing to
it rather than after.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from musaeus.config import MusicConfig
from musaeus.console import Console
from musaeus.db import open_db, upsert_archive
from musaeus.editions import EDITIONS


@pytest.fixture
def cfg(tmp_path: Path) -> MusicConfig:
    return MusicConfig(
        vault_root=tmp_path, inbox=tmp_path / "INBOX", staging=tmp_path / "STAGING",
        quarantine=tmp_path / "QUARANTINE", runs_root=tmp_path / "RUNS",
        meta_dir=tmp_path / "MetaData", alac_library=tmp_path / "ALAC-Library",
        db_path=tmp_path / "musaeus.db",
    )


def _console(cfg: MusicConfig) -> Console:
    con = Console()
    con._config = cfg
    return con


def _seed(cfg: MusicConfig) -> None:
    cfg.ensure_dirs()
    conn = open_db(cfg.db_path)
    for i in range(6):
        upsert_archive(conn, {
            "file_path": f"/vault/a{i}.m4a", "status": "CATALOGUED",
            "artist": "A", "album": "Al", "title": f"T{i}",
            "genre": "Rock", "duration": 240.0, "size_bytes": 40_000_000,
        })
    conn.commit()
    conn.close()


class TestEditionMenu:
    @pytest.mark.parametrize("idx,name", [(0, "lossless"), (1, "car"), (2, "iphone")])
    def test_each_edition_is_reachable_and_writes_nothing(
        self, cfg, monkeypatch, capsys, idx, name
    ) -> None:
        _seed(cfg)
        con = _console(cfg)
        # iphone additionally prompts for a budget; a blank answer is valid.
        responses = iter([str(idx), ""])
        monkeypatch.setattr("builtins.input", lambda *a, **k: next(responses))

        before = sorted(p.name for p in Path(cfg.vault_root).rglob("*") if p.is_file())
        con._edition_menu()
        after = sorted(p.name for p in Path(cfg.vault_root).rglob("*") if p.is_file())

        out = capsys.readouterr().out
        assert name in out.lower() or EDITIONS[name].codec.upper() in out
        assert "Selection only" in out
        assert before == after, "a preview must not create or remove files"

    def test_back_does_nothing(self, cfg, monkeypatch, capsys) -> None:
        _seed(cfg)
        con = _console(cfg)
        monkeypatch.setattr("builtins.input", lambda *a, **k: "3")
        con._edition_menu()
        assert "Selection only" not in capsys.readouterr().out

    def test_iphone_budget_is_applied(self, cfg, monkeypatch, capsys) -> None:
        """The one edition where a budget is not optional -- 81.7 GB of
        library does not go into a 30 GB phone."""
        _seed(cfg)
        con = _console(cfg)
        responses = iter(["2", "0.02"])  # 20 MB: fits some, not all
        monkeypatch.setattr("builtins.input", lambda *a, **k: next(responses))
        con._edition_menu()
        out = capsys.readouterr().out
        assert "do not fit" in out

    def test_a_bad_budget_is_refused_not_guessed(self, cfg, monkeypatch, capsys) -> None:
        _seed(cfg)
        con = _console(cfg)
        responses = iter(["2", "loads"])
        monkeypatch.setattr("builtins.input", lambda *a, **k: next(responses))
        con._edition_menu()
        out = capsys.readouterr().out
        assert "Not a number" in out
        assert "Selection only" not in out, "must not proceed on an unparseable budget"
