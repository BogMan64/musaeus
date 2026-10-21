"""`musaeus dedupe --sheet FILE` carries out a decided sheet (Grey, 2026-10-21).

Grey ruled one copy per song by rules, and approved the copies to keep as a
sheet. The sheet is carried out by the resolver's own carry-out, the move a
choice made in the console gets: REMOVE copies to review with a restore
script, KEEP copies marked kept by a person. Nothing is written without
--execute, and the sheet is checked against the catalogue as it is now.
"""

from __future__ import annotations

import csv
from pathlib import Path

import pytest

from musaeus import dedupe
from musaeus.config import MusicConfig
from musaeus.db import open_db, upsert_archive
from musaeus.masters_lock import EXIT_BUSY, masters_lock
from musaeus.stages.dupe_resolver import DupeResolverStage


@pytest.fixture
def cfg(tmp_path):
    meta = tmp_path / "MetaData"
    meta.mkdir()
    (meta / "artist_canon.tsv").write_text("", encoding="utf-8")
    c = MusicConfig(
        vault_root=tmp_path, inbox=tmp_path / "INBOX", staging=tmp_path / "STAGING",
        quarantine=tmp_path / "Q", runs_root=tmp_path / "RUNS", meta_dir=meta,
        alac_library=tmp_path / "ALAC-Library", db_path=tmp_path / "musaeus.db",
    )  # fmt: skip
    c.ensure_dirs()
    return c


def _copy(cfg, name: str, *, kept_by_person: str | None = None) -> tuple[int, Path]:
    p = cfg.alac_library / "A" / name
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(name.encode() * 100)
    conn = open_db(cfg.db_path)
    upsert_archive(conn, {"file_path": str(p), "status": "CATALOGUED", "artist": "A", "title": "Song",
                          "album": "Album", "codec": "alac", "duration": 200.0,
                          "audio_hash": "pcm:" + name})  # fmt: skip
    conn.execute(
        "UPDATE archive SET kept_by_person_at = ? WHERE file_path = ?", (kept_by_person, str(p))
    )
    conn.commit()
    rid = conn.execute("SELECT id FROM archive WHERE file_path = ?", (str(p),)).fetchone()[0]
    conn.close()
    return rid, p


def _sheet(tmp_path, rows) -> Path:
    path = tmp_path / "decided.csv"
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(["Group", "Action", "Why", "DB id", "Master file", "Audio hash"])
        for group, action, rid, p in rows:
            w.writerow([group, action, "", rid, str(p), "pcm:" + Path(p).name])
    return path


def _run(cfg, sheet, execute):
    said: list[str] = []
    code = dedupe.apply_sheet(cfg, sheet, execute=execute, out=said.append)
    return code, "\n".join(said)


def _row(cfg, rid):
    conn = open_db(cfg.db_path)
    r = conn.execute(
        "SELECT status, kept_by_person_at FROM archive WHERE id = ?", (rid,)
    ).fetchone()
    pending = conn.execute("SELECT count(*) FROM duplicates WHERE status = 'pending'").fetchone()[0]
    conn.close()
    return (r[0], r[1]), pending


def test_a_dry_run_moves_and_writes_nothing(cfg, tmp_path):
    k, kp = _copy(cfg, "studio.m4a")
    r, rp = _copy(cfg, "live.m4a")
    code, said = _run(cfg, _sheet(tmp_path, [("1", "KEEP", k, kp), ("1", "REMOVE?", r, rp)]), False)
    assert code == 0 and "1 copies in 1 groups would move" in said
    assert kp.exists() and rp.exists()
    conn = open_db(cfg.db_path)
    assert conn.execute("SELECT count(*) FROM duplicates").fetchone()[0] == 0
    conn.close()


def test_execute_moves_the_removed_copy_and_marks_the_kept_one(cfg, tmp_path):
    k, kp = _copy(cfg, "studio.m4a")
    r, rp = _copy(cfg, "live.m4a")
    code, said = _run(cfg, _sheet(tmp_path, [("1", "KEEP", k, kp), ("1", "REMOVE?", r, rp)]), True)
    assert code == 0, said
    assert kp.exists() and not rp.exists()
    assert len(list(cfg.dupes_review_dir.rglob("*.m4a"))) == 1, "not in review"
    assert list(cfg.dupes_review_dir.rglob("restore_*.sh")), "no restore script"
    (status, kept_mark), pending = _row(cfg, k)
    assert status == "CATALOGUED" and kept_mark and pending == 0
    assert _row(cfg, r)[0][0] == "DUPE_REVIEW"


def test_the_resolver_leaves_a_carried_out_sheet_alone(cfg, tmp_path):
    k, kp = _copy(cfg, "studio.m4a")
    r, rp = _copy(cfg, "live.m4a")
    _run(cfg, _sheet(tmp_path, [("1", "KEEP", k, kp), ("1", "REMOVE", r, rp)]), True)
    from musaeus.context import RunContext

    conn = open_db(cfg.db_path)
    result = DupeResolverStage().execute(RunContext.new(cfg, conn, dry_run=False))
    conn.close()
    assert kp.exists() and not result.errors, result.errors


def test_a_copy_moved_since_the_sheet_was_made_skips_its_group(cfg, tmp_path):
    k, kp = _copy(cfg, "studio.m4a")
    r, rp = _copy(cfg, "live.m4a")
    sheet = _sheet(tmp_path, [("1", "KEEP", k, kp), ("1", "REMOVE?", r, rp)])
    refiled = rp.with_name("live (refiled).m4a")
    rp.rename(refiled)
    conn = open_db(cfg.db_path)
    conn.execute("UPDATE archive SET file_path = ? WHERE id = ?", (str(refiled), r))
    conn.commit()
    conn.close()
    code, said = _run(cfg, sheet, True)
    assert code == 1 and "has moved since the sheet was made" in said
    assert kp.exists() and refiled.exists()


def test_a_copy_a_person_kept_is_never_removed(cfg, tmp_path):
    k, kp = _copy(cfg, "studio.m4a")
    r, rp = _copy(cfg, "live.m4a", kept_by_person="2026-10-20T00:00:00+00:00")
    code, said = _run(cfg, _sheet(tmp_path, [("1", "KEEP", k, kp), ("1", "REMOVE?", r, rp)]), True)
    assert code == 1 and "was kept by a person" in said
    assert kp.exists() and rp.exists()


def test_another_recording_at_the_same_path_skips_its_group(cfg, tmp_path):
    """Same row, same path, another recording (Codex review of #222): the
    group must not be written with the new audio as its identity."""
    k, kp = _copy(cfg, "studio.m4a")
    r, rp = _copy(cfg, "live.m4a")
    sheet = _sheet(tmp_path, [("1", "KEEP", k, kp), ("1", "REMOVE?", r, rp)])
    conn = open_db(cfg.db_path)
    conn.execute("UPDATE archive SET audio_hash = 'pcm:another' WHERE id = ?", (r,))
    conn.commit()
    conn.close()
    code, said = _run(cfg, sheet, True)
    assert code == 1 and "not the recording the sheet was made from" in said
    assert kp.exists() and rp.exists()


def test_a_kept_pair_with_nothing_to_remove_is_not_a_failure(cfg, tmp_path):
    k, kp = _copy(cfg, "one.m4a")
    k2, kp2 = _copy(cfg, "two.m4a")
    code, said = _run(cfg, _sheet(tmp_path, [("1", "KEEP", k, kp), ("1", "KEEP", k2, kp2)]), True)
    assert code == 0 and "nothing to remove" in said


def test_a_group_not_carried_out_is_not_left_for_act_2(cfg, tmp_path):
    """A pending group would be decided by the keep rule at the next Act 2,
    perhaps the other way round."""
    k, kp = _copy(cfg, "studio.m4a")
    r, rp = _copy(cfg, "live.m4a")
    kp.unlink()  # the kept copy is gone from disk: carry_out refuses
    code, said = _run(cfg, _sheet(tmp_path, [("1", "KEEP", k, kp), ("1", "REMOVE?", r, rp)]), True)
    assert code == 1 and "no kept copy is on disk" in said
    assert rp.exists()
    assert _row(cfg, r)[1] == 0, "a refused group was left pending"


def test_a_busy_masters_lock_moves_and_writes_nothing(cfg, tmp_path):
    k, kp = _copy(cfg, "studio.m4a")
    r, rp = _copy(cfg, "live.m4a")
    sheet = _sheet(tmp_path, [("1", "KEEP", k, kp), ("1", "REMOVE?", r, rp)])
    with masters_lock(cfg.runs_root, exclusive=True, what="a bit-rot check"):
        code, said = _run(cfg, sheet, True)
    assert code == EXIT_BUSY and "nothing moved" in said
    assert kp.exists() and rp.exists()
    assert _row(cfg, r)[1] == 0


NO_ID_COLUMN = (["Group", "Action", "Master file", "Audio hash"], "KEEP", "no column DB id")
UNKNOWN_ACTION = (
    ["Group", "Action", "DB id", "Master file", "Audio hash"],
    "MAYBE",
    "neither KEEP nor REMOVE",
)


@pytest.mark.parametrize("header, action, problem", [NO_ID_COLUMN, UNKNOWN_ACTION])
def test_a_sheet_that_cannot_be_read_is_refused_before_anything(
    cfg, tmp_path, header, action, problem
):
    path = tmp_path / "bad.csv"
    path.write_text(
        ",".join(header) + "\n" + ",".join(["1", action, "7", "/x", "pcm:x"][: len(header)]) + "\n"
    )
    with pytest.raises(ValueError, match=problem):
        dedupe.apply_sheet(cfg, path, execute=True, out=lambda s: None)
