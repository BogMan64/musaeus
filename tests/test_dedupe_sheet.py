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


def test_the_decision_keeps_each_copys_title(cfg, tmp_path):
    """The resolver reads the rejected version from the decision; review may be
    cleared later (Codex review of #239, round 5)."""
    k, kp = _copy(cfg, "studio.m4a")
    r, rp = _copy(cfg, "live.m4a")
    _run(cfg, _sheet(tmp_path, [("1", "KEEP", k, kp), ("1", "REMOVE?", r, rp)]), True)
    conn = open_db(cfg.db_path)
    got = conn.execute(
        "SELECT status, title FROM duplicates WHERE duplicate_type = 'PERSON'"
    ).fetchall()
    conn.close()
    assert sorted(map(tuple, got)) == [("archive", "Song"), ("keep", "Song")]


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


def test_a_dry_run_does_not_create_a_catalogue(cfg, tmp_path):
    """open_db creates a missing catalogue and migrates one (Codex review of #222)."""
    assert not cfg.db_path.exists()
    sheet = _sheet(
        tmp_path, [("1", "KEEP", 1, tmp_path / "a.m4a"), ("1", "REMOVE", 2, tmp_path / "b.m4a")]
    )
    with pytest.raises(ValueError, match="no catalogue"):
        dedupe.apply_sheet(cfg, sheet, execute=False, out=lambda s: None)
    assert not cfg.db_path.exists()


def test_a_group_moved_part_way_finishes_on_a_second_run(cfg, tmp_path):
    """The copy already in review is done; the rest of the group still moves
    (Codex review of #222)."""
    k, kp = _copy(cfg, "studio.m4a")
    r1, rp1 = _copy(cfg, "live.m4a")
    r2, rp2 = _copy(cfg, "demo.m4a")
    _run(cfg, _sheet(tmp_path, [("1", "KEEP", k, kp), ("1", "REMOVE", r1, rp1)]), True)
    assert not rp1.exists() and rp2.exists()
    whole = [("1", "KEEP", k, kp), ("1", "REMOVE", r1, rp1), ("1", "REMOVE", r2, rp2)]
    code, said = _run(cfg, _sheet(tmp_path, whole), True)
    assert code == 0, said
    assert kp.exists() and not rp2.exists()


def test_a_copy_in_an_open_duplicate_group_is_left_for_the_console(cfg, tmp_path):
    """A kept mark protects a copy only in a group of kept copies: Act 2 would
    rank the open group and might move the sheet's keeper (Codex review of #222)."""
    k, kp = _copy(cfg, "studio.m4a")
    r, rp = _copy(cfg, "live.m4a")
    conn = open_db(cfg.db_path)
    conn.execute(
        "INSERT INTO duplicates (group_id, file_path, duplicate_type, confidence, run_id, audio_hash) "
        "VALUES ('near_x', ?, 'NEAR', 0.9, 'r', 'pcm:studio.m4a')",
        (str(kp),),
    )
    conn.commit()
    conn.close()
    code, said = _run(cfg, _sheet(tmp_path, [("1", "KEEP", k, kp), ("1", "REMOVE", r, rp)]), True)
    assert code == 1 and "open duplicate group" in said
    assert kp.exists() and rp.exists()


def test_a_group_not_carried_out_is_not_left_for_act_2(cfg, tmp_path, monkeypatch):
    """A pending group would be decided by the keep rule at the next Act 2,
    perhaps the other way round."""
    from musaeus.context import StageResult

    def refused(self, ctx, groups, kept, archived):
        return StageResult(
            stage_name="dupe_resolver", success=False, errors=["nothing moved: refused"]
        )

    monkeypatch.setattr(DupeResolverStage, "carry_out", refused)
    k, kp = _copy(cfg, "studio.m4a")
    r, rp = _copy(cfg, "live.m4a")
    code, said = _run(cfg, _sheet(tmp_path, [("1", "KEEP", k, kp), ("1", "REMOVE?", r, rp)]), True)
    assert code == 1 and "refused" in said
    assert rp.exists()
    assert _row(cfg, r)[1] == 0, "a refused group was left pending"


def test_a_run_stopped_before_the_keep_mark_marks_it_on_the_next_run(cfg, tmp_path):
    """Codex review of #222: the REMOVE copy moved, the keep not yet marked."""
    k, kp = _copy(cfg, "studio.m4a")
    r, rp = _copy(cfg, "live.m4a")
    sheet = _sheet(tmp_path, [("1", "KEEP", k, kp), ("1", "REMOVE", r, rp)])
    _run(cfg, sheet, True)
    conn = open_db(cfg.db_path)
    conn.execute("UPDATE archive SET kept_by_person_at = NULL WHERE id = ?", (k,))
    conn.commit()
    conn.close()
    code, said = _run(cfg, sheet, True)
    assert code == 0, said
    assert _row(cfg, k)[0][1], "the keep was not marked"


def test_a_copy_missing_from_disk_does_not_count_as_would_move(cfg, tmp_path):
    k, kp = _copy(cfg, "studio.m4a")
    r, rp = _copy(cfg, "live.m4a")
    rp.unlink()
    code, said = _run(cfg, _sheet(tmp_path, [("1", "KEEP", k, kp), ("1", "REMOVE", r, rp)]), False)
    assert "is not on disk" in said and "DRY RUN: 0 copies" in said


def test_a_busy_lock_does_not_open_or_create_the_catalogue(cfg, tmp_path):
    """open_db migrates (or creates) the catalogue: not while another job holds
    the lock (Codex review of #222)."""
    sheet = _sheet(
        tmp_path, [("1", "KEEP", 1, tmp_path / "a.m4a"), ("1", "REMOVE", 2, tmp_path / "b.m4a")]
    )
    with masters_lock(cfg.runs_root, exclusive=True, what="a bit-rot check"):
        code, said = _run(cfg, sheet, True)
    assert code == EXIT_BUSY
    assert not cfg.db_path.exists()


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


def test_a_copy_in_two_groups_is_refused_before_anything(cfg, tmp_path):
    """Group 1 removes B, group 2 keeps it: carried out one by one, B would be
    gone before group 2 (Codex review of #222)."""
    a, ap = _copy(cfg, "a.m4a")
    b, bp = _copy(cfg, "b.m4a")
    c, cp = _copy(cfg, "c.m4a")
    rows = [
        ("1", "KEEP", a, ap),
        ("1", "REMOVE", b, bp),
        ("2", "KEEP", b, bp),
        ("2", "REMOVE", c, cp),
    ]
    with pytest.raises(ValueError, match="twice"):
        dedupe.apply_sheet(cfg, _sheet(tmp_path, rows), execute=True, out=lambda s: None)
    assert ap.exists() and bp.exists() and cp.exists()


def test_a_row_with_no_group_is_refused(cfg, tmp_path):
    """Blank groups would gather unrelated songs into one (Codex review of #222)."""
    a, ap = _copy(cfg, "a.m4a")
    b, bp = _copy(cfg, "b.m4a")
    with pytest.raises(ValueError, match="no group"):
        dedupe.apply_sheet(cfg, _sheet(tmp_path, [("", "KEEP", a, ap), ("", "REMOVE", b, bp)]), execute=True,
                           out=lambda s: None)  # fmt: skip
    assert ap.exists() and bp.exists()
