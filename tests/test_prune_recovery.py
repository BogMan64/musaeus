"""Retention for RUNS/recovery.

A checkpoint is the only way to reverse a finalize, so every test here is
about what the prune REFUSES to delete. The count and age gates are AND, so
a burst of runs in one day cannot age out a checkpoint the count would keep.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

_SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "prune_recovery.py"
_spec = importlib.util.spec_from_file_location("prune_recovery", _SCRIPT)
assert _spec and _spec.loader
pr = importlib.util.module_from_spec(_spec)
# Registered before exec: @dataclass resolves annotations via sys.modules.
sys.modules["prune_recovery"] = pr
_spec.loader.exec_module(pr)

NOW = datetime(2026, 8, 29, tzinfo=timezone.utc)


def _checkpoint(root: Path, name: str, days_old: float, *, manifest: bool = True) -> Path:
    d = root / name
    (d / "payload").mkdir(parents=True, exist_ok=True)
    (d / "payload" / "x.bin").write_bytes(b"0" * 100)
    if manifest:
        created = NOW - timedelta(days=days_old)
        (d / "manifest.json").write_text(
            json.dumps(
                {
                    "checkpoint_id": name,
                    "created_at": created.isoformat().replace("+00:00", "Z"),
                    "entries": [],
                    "source_root": "/x",
                }
            )
        )
    return d


# ── scanning ──────────────────────────────────────────────────────────────────


def test_scan_returns_newest_first(tmp_path):
    _checkpoint(tmp_path, "old", 30)
    _checkpoint(tmp_path, "new", 1)
    _checkpoint(tmp_path, "mid", 10)
    assert [c.name for c in pr.scan(tmp_path)] == ["new", "mid", "old"]


def test_scan_measures_size_including_nested_files(tmp_path):
    """Every file under the checkpoint counts -- payload AND manifest.

    The size is what tells you whether pruning is worth doing at all, and
    once `work/act23` puts source audio in the payload it is the whole
    point. Summing only the top level would understate it enormously.
    """
    d = _checkpoint(tmp_path, "a", 1)
    on_disk = sum(f.stat().st_size for f in d.rglob("*") if f.is_file())
    assert on_disk > 100, "fixture should have a nested payload plus a manifest"
    assert pr.scan(tmp_path)[0].size_bytes == on_disk


def test_a_missing_root_is_empty_not_an_error(tmp_path):
    assert pr.scan(tmp_path / "nope") == []


def test_a_checkpoint_without_a_manifest_is_marked_unreadable(tmp_path):
    _checkpoint(tmp_path, "broken", 90, manifest=False)
    assert pr.scan(tmp_path)[0].readable is False


# ── the two gates ─────────────────────────────────────────────────────────────


def test_the_newest_are_kept_however_old_they_are(tmp_path):
    for i in range(8):
        _checkpoint(tmp_path, f"cp{i:02d}", 100 + i)
    deletable, kept = pr.select_for_deletion(
        pr.scan(tmp_path), keep_last=5, min_age_days=1, now=NOW
    )
    assert len(kept) == 5
    assert len(deletable) == 3
    assert all("among the newest 5" in v for v in kept.values())


def test_young_checkpoints_are_kept_however_many_there_are(tmp_path):
    """A burst of runs in one day must not age anything out."""
    for i in range(20):
        _checkpoint(tmp_path, f"cp{i:02d}", 0.1 * i)
    deletable, kept = pr.select_for_deletion(
        pr.scan(tmp_path), keep_last=2, min_age_days=14, now=NOW
    )
    assert deletable == []
    assert len(kept) == 20


def test_both_gates_must_pass_for_deletion(tmp_path):
    _checkpoint(tmp_path, "newest", 1)
    _checkpoint(tmp_path, "old_but_protected_by_count", 40)
    _checkpoint(tmp_path, "old_and_beyond_the_count", 50)
    deletable, kept = pr.select_for_deletion(
        pr.scan(tmp_path), keep_last=2, min_age_days=14, now=NOW
    )
    assert [c.name for c in deletable] == ["old_and_beyond_the_count"]
    assert "newest" in kept and "old_but_protected_by_count" in kept


def test_an_unreadable_checkpoint_is_never_deleted(tmp_path):
    """Deleting what you cannot read is how recoverability vanishes quietly."""
    _checkpoint(tmp_path, "readable_old", 90)
    _checkpoint(tmp_path, "broken_old", 90, manifest=False)
    deletable, kept = pr.select_for_deletion(
        pr.scan(tmp_path), keep_last=0, min_age_days=1, now=NOW
    )
    names = [c.name for c in deletable]
    assert "broken_old" not in names
    assert "readable_old" in names
    assert "no readable manifest" in kept["broken_old"]


def test_kept_reasons_are_reported_for_every_kept_checkpoint(tmp_path):
    for i in range(4):
        _checkpoint(tmp_path, f"cp{i}", i)
    deletable, kept = pr.select_for_deletion(
        pr.scan(tmp_path), keep_last=2, min_age_days=14, now=NOW
    )
    assert deletable == []
    assert set(kept) == {"cp0", "cp1", "cp2", "cp3"}


# ── the CLI ───────────────────────────────────────────────────────────────────


def test_dry_run_deletes_nothing(tmp_path, monkeypatch, capsys):
    d = _checkpoint(tmp_path, "ancient", 400)
    monkeypatch.setattr(sys, "argv", [
        "prune_recovery.py", "--root", str(tmp_path),
        "--keep-last", "0", "--min-age-days", "1",
    ])
    assert pr.main() == 0
    assert d.exists(), "a dry run must not delete"
    assert "DRY RUN" in capsys.readouterr().out


def test_apply_deletes_only_the_eligible(tmp_path, monkeypatch):
    doomed = _checkpoint(tmp_path, "ancient", 400)
    safe = _checkpoint(tmp_path, "recent", 1)
    monkeypatch.setattr(sys, "argv", [
        "prune_recovery.py", "--root", str(tmp_path),
        "--keep-last", "0", "--min-age-days", "14", "--apply",
    ])
    assert pr.main() == 0
    assert not doomed.exists()
    assert safe.exists()


def test_human_sizes():
    assert pr._human(0) == "0B"
    assert pr._human(512) == "512B"
    assert pr._human(2048) == "2.0KB"
    assert pr._human(17 * 1024 * 1024) == "17.0MB"
