"""Preflight must name the archive's lock before any stage writes to it.

On 6cad046 preflight said nothing about the immutable flag. The tag stages
then reported a bare error count with no cause (7,975 masters carry the flag
on this machine, measured 2026-11-25).

The first test fails on 6cad046 for the right reason: no note about the
archive's lock state appears at all.
"""

from __future__ import annotations

from pathlib import Path

from musaeus.stages.preflight import PreflightStage


def _stage_report_on_an_empty_archive(disposable_vault, monkeypatch):
    monkeypatch.setattr("sys.stdin.isatty", lambda: False)
    (disposable_vault.cfg.libraries / "ALAC-Archival").mkdir(parents=True, exist_ok=True)
    conn = disposable_vault.open_db()
    ctx = disposable_vault.new_context(conn, dry_run=True)
    result = PreflightStage().dry_run(ctx)
    conn.close()
    return "\n".join(result.notes)


def test_preflight_reports_the_archive_lock_state(disposable_vault, monkeypatch):
    notes = _stage_report_on_an_empty_archive(disposable_vault, monkeypatch)
    assert "no immutable files detected" in notes


def test_preflight_warns_and_names_the_unlock_command(disposable_vault, monkeypatch):
    monkeypatch.setattr("sys.stdin.isatty", lambda: False)
    archive = disposable_vault.cfg.libraries / "ALAC-Archival"
    archive.mkdir(parents=True, exist_ok=True)
    conn = disposable_vault.open_db()
    ctx = disposable_vault.new_context(conn, dry_run=True)

    from musaeus import immutability

    monkeypatch.setattr(
        immutability,
        "locked_in",
        lambda root: (2, [Path("/m/Artist/Album/Artist - Song.m4a")]),
    )
    result = PreflightStage().dry_run(ctx)
    conn.close()

    notes = "\n".join(result.notes)
    assert "2 master file(s) carry the immutable flag" in notes
    assert "sudo chattr -R -i" in notes
    assert "sudo chattr -R +i" in notes
    # A warning, not a refused run: nothing to change needs no unlock.
    assert any("WARN  archive is locked" in n for n in result.notes)
    assert "ERROR" not in notes
