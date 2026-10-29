"""swap_review_copies: a better copy in duplicate review replaces its master.

Three steps so the song is never absent: --promote hands the review copy to
Act 3 carrying the master's filing facts; Act 3 finalizes it beside the
master; --retire hands the replaced master to delete_reviewed_tracks.py.
"""

from __future__ import annotations

import importlib.util
import sqlite3
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "swap_review_copies.py"
COLS = (
    "id INTEGER PRIMARY KEY, file_path TEXT, audio_hash TEXT, artist TEXT, album TEXT, "
    "title TEXT, genre TEXT, codec TEXT, sample_rate INTEGER, duration REAL, lufs REAL, status TEXT, "
    "mb_artist_name TEXT, mb_artist_id TEXT, canonicalized_at TEXT, finalized_at TEXT, "
    "kept_by_person_at TEXT, lufs_baked_at TEXT, lufs_baked_target REAL"
)


def _load():
    spec = importlib.util.spec_from_file_location("swap_review_copies", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(autouse=True)
def _no_leftover_holds(monkeypatch):
    """main() --execute holds the masters lock for the rest of the process;
    in one pytest process that leaked into the next test (review of #202)."""
    from musaeus import config, masters_lock

    monkeypatch.setattr(config, "_cached_config", None)
    yield
    while masters_lock._held:
        masters_lock._held.pop().__exit__(None, None, None)


@pytest.fixture
def vault(tmp_path, monkeypatch):
    mod = _load()
    masters = tmp_path / "Libraries" / "ALAC-Archival"
    review_dir = tmp_path / "REVIEW" / "DUPES_MOVED" / "2026-10-02"
    conn = sqlite3.connect(tmp_path / "musaeus.db")
    conn.row_factory = sqlite3.Row
    conn.execute(f"CREATE TABLE archive ({COLS})")
    conn.execute(
        "CREATE TABLE events (id INTEGER PRIMARY KEY, run_id TEXT, ts TEXT, event_type TEXT, "
        "file_path TEXT, old_value TEXT, new_value TEXT, stage TEXT, note TEXT)"
    )
    conn.execute(
        "CREATE TABLE duplicates (id INTEGER PRIMARY KEY, group_id TEXT, file_path TEXT, "
        "status TEXT, audio_hash TEXT)"
    )
    on_disk: dict[str, str] = {}  # path -> the audio hash its file really carries
    monkeypatch.setattr(mod, "audio_hash_safe", lambda p: (on_disk.get(str(p)), None))

    def row(path: Path, status: str, h: str, **kw) -> int:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(h.encode())
        on_disk[str(path)] = h
        fields = {
            "file_path": str(path), "audio_hash": h, "status": status, "artist": "Toto", "album": "IV",
            "title": "Rosanna", "genre": "Rock", "codec": "alac", "sample_rate": 44100, "duration": 331.0,
            "finalized_at": "2026-09-25 22:00:00",
        }  # fmt: skip
        fields.update(kw)
        cur = conn.execute(
            f"INSERT INTO archive ({', '.join(fields)}) VALUES ({', '.join('?' * len(fields))})",
            tuple(fields.values()),
        )
        conn.commit()
        return cur.lastrowid

    def set_aside(review_path: Path, kept: Path) -> None:
        conn.execute(
            "INSERT INTO events (event_type, file_path, new_value, note) VALUES "
            "('DUPE_MOVED_FOR_REVIEW', ?, ?, ?)",
            (str(review_path), str(review_path), f"group=near_1 type=NEAR kept={kept}"),
        )
        conn.commit()

    cfg = SimpleNamespace(
        alac_archive=masters, runs_root=tmp_path / "RUNS", db_path=tmp_path / "musaeus.db"
    )
    return SimpleNamespace(
        mod=mod, conn=conn, cfg=cfg, row=row, set_aside=set_aside, masters=masters,
        review_dir=review_dir, on_disk=on_disk,
    )  # fmt: skip


def _lib_and_better_review(v, **review_kw):
    lib_path = v.masters / "Rock/Toto/Toto IV/Toto - Rosanna.m4a"
    lib = v.row(
        lib_path, "CATALOGUED", "L", album="Toto IV", mb_artist_name="Toto", mb_artist_id="t1"
    )
    rev_path = v.review_dir / "Toto/My playlist/Toto - Rosanna.m4a"
    kw = {"album": "My playlist", "artist": "TOTO", "sample_rate": 96000}
    kw.update(review_kw)
    rev = v.row(rev_path, "DUPE_REVIEW", "R", **kw)
    v.set_aside(rev_path, lib_path)
    return lib, rev, lib_path, rev_path


def test_a_better_review_copy_is_found_with_its_master(vault):
    lib, rev, _, _ = _lib_and_better_review(vault)
    wins, tally = vault.mod.review_wins(vault.conn)
    assert [(p.review["id"], p.library["id"], p.step) for p in wins] == [(rev, lib, "quality")]
    assert tally["review (quality)"] == 1


def test_a_worse_review_copy_is_not_promoted(vault):
    _lib_and_better_review(vault, sample_rate=44100, title="Rosanna (Live)", duration=345.0)
    wins, tally = vault.mod.review_wins(vault.conn)
    assert wins == [] and tally["library (studio/live)"] == 1


def test_a_live_original_in_review_beats_its_same_length_remaster(vault):
    # Batch 7 (2026-10-25), Codex review of #248: one live take, only the original
    # labelled "Live"; the remaster read as studio kept the master's place.
    lib, rev, _, _ = _lib_and_better_review(vault, title="Rosanna (Live)", sample_rate=192000)
    vault.conn.execute("UPDATE archive SET title='Rosanna (Remastered 2003)' WHERE id=?", (lib,))
    vault.conn.commit()
    wins, _ = vault.mod.review_wins(vault.conn)
    assert [p.review["id"] for p in wins] == [rev]


def test_a_live_take_does_not_spread_past_its_labelled_copy(vault):
    # Codex review of #248, round 2: a copy read as live by length seeded the
    # next, 100.0 -> 100.4 -> 100.8 s, until the studio master read as live.
    lib, _, lib_path, _ = _lib_and_better_review(vault, title="Rosanna (Live)", duration=100.0)
    vault.conn.execute("UPDATE archive SET duration=100.8 WHERE id=?", (lib,))
    mid_path = vault.review_dir / "Toto/x/Toto - Rosanna.m4a"
    vault.row(mid_path, "DUPE_REVIEW", "M", sample_rate=96000, duration=100.4)
    vault.set_aside(mid_path, lib_path)
    wins, _ = vault.mod.review_wins(vault.conn)
    assert wins == []


def test_a_master_a_person_kept_is_never_offered_for_a_swap(vault):
    # Grey's Oct 21 sheet kept these masters over the copies now in review; 143 of
    # the 144 swaps listed on 2026-10-25 would have undone those choices.
    lib, _, _, _ = _lib_and_better_review(vault)
    vault.conn.execute("UPDATE archive SET kept_by_person_at='2026-10-21' WHERE id=?", (lib,))
    vault.conn.commit()
    losers: list[int] = []
    wins, tally = vault.mod.review_wins(vault.conn, losers)
    assert wins == [] and losers == [] and tally["kept by a person"] == 1


def test_promote_dry_run_changes_nothing(vault):
    _, rev, _, _ = _lib_and_better_review(vault)
    vault.mod.promote(vault.conn, vault.mod.review_wins(vault.conn)[0], execute=False)
    assert (
        vault.conn.execute("SELECT status FROM archive WHERE id=?", (rev,)).fetchone()[0]
        == "DUPE_REVIEW"
    )


def test_promote_hands_the_copy_to_act_3_with_the_masters_filing_facts(vault):
    lib, rev, _, rev_path = _lib_and_better_review(vault)
    out = vault.mod.promote(vault.conn, vault.mod.review_wins(vault.conn)[0], execute=True)
    assert out["promoted"] == 1
    r = vault.conn.execute("SELECT * FROM archive WHERE id=?", (rev,)).fetchone()
    assert r["status"] == "CATALOGUED"
    assert (r["artist"], r["album"], r["genre"], r["mb_artist_name"], r["mb_artist_id"]) == (
        "Toto", "Toto IV", "Rock", "Toto", "t1",
    )  # fmt: skip
    assert r["title"] == "Rosanna", "the copy keeps its own title"
    assert r["canonicalized_at"] is None and r["finalized_at"] is None, "Act 3 selects exactly this"
    assert r["file_path"] == str(rev_path), "promote moves no file; finalize does"
    ev = vault.conn.execute(
        "SELECT old_value, new_value FROM events WHERE event_type='SWAP_PROMOTED'"
    ).fetchone()
    assert (int(ev[0]), int(ev[1])) == (lib, rev)
    assert (
        vault.conn.execute("SELECT status FROM archive WHERE id=?", (lib,)).fetchone()[0]
        == "CATALOGUED"
    )


def test_a_review_copy_whose_audio_changed_is_refused(vault):
    _, rev, _, rev_path = _lib_and_better_review(vault)
    vault.on_disk[str(rev_path)] = "SOMETHING ELSE"
    out = vault.mod.promote(vault.conn, vault.mod.review_wins(vault.conn)[0], execute=True)
    assert out == {"refused": 1}
    assert (
        vault.conn.execute("SELECT status FROM archive WHERE id=?", (rev,)).fetchone()[0]
        == "DUPE_REVIEW"
    )


def test_the_master_is_found_after_a_rename(vault):
    lib_path_then = vault.masters / "Rock/Toto Band/Toto IV/Toto Band - Rosanna.m4a"
    lib, rev, lib_path, rev_path = _lib_and_better_review(vault)
    vault.conn.execute("UPDATE events SET note=? WHERE event_type='DUPE_MOVED_FOR_REVIEW'",
                       (f"group=near_1 type=NEAR kept={lib_path_then}",))  # fmt: skip
    vault.conn.execute(
        "INSERT INTO events (event_type, old_value, new_value) VALUES ('ARTIST_CONSOLIDATED', ?, ?)",
        (str(lib_path_then), str(lib_path)),
    )
    vault.conn.commit()
    wins, _ = vault.mod.review_wins(vault.conn)
    assert [p.library["id"] for p in wins] == [lib]


def test_retire_waits_for_act_3_then_hands_the_master_to_the_delete_tool(vault, monkeypatch):
    lib, rev, _, _ = _lib_and_better_review(vault)
    vault.mod.promote(vault.conn, vault.mod.review_wins(vault.conn)[0], execute=True)
    assert vault.mod.to_retire(vault.conn, vault.masters) == [], "not finalized yet"

    new_home = vault.masters / "Rock/Toto/Toto IV/Toto - Rosanna (2).m4a"
    new_home.write_bytes(b"R")  # what Act 3's finalize does
    vault.conn.execute(
        "UPDATE archive SET file_path=?, finalized_at='2026-10-04 01:00:00', "
        "canonicalized_at='2026-10-04 00:59:00' WHERE id=?",
        (str(new_home), rev),
    )
    vault.conn.commit()
    assert vault.mod.to_retire(vault.conn, vault.masters) == [lib]

    calls = []
    monkeypatch.setattr(
        vault.mod.subprocess,
        "run",
        lambda cmd, **k: calls.append(cmd) or SimpleNamespace(returncode=0),
    )
    assert vault.mod.retire(vault.cfg, [lib], execute=False) == 0
    cmd = calls[0]
    assert Path(cmd[1]).name == "delete_reviewed_tracks.py" and "--execute" not in cmd
    assert Path(cmd[2]).read_text() == f"{lib}\n"


def test_one_rule_decides_both_lists(vault):
    """The rows to delete come from the same rule as the swaps (a Desktop copy of
    the rule bucketed lengths and over-counted swaps by 72, 2026-10-03)."""
    _, better, _, _ = _lib_and_better_review(vault)
    worse_path = vault.review_dir / "Toto/Live/Toto - Rosanna (Live).m4a"
    worse = vault.row(worse_path, "DUPE_REVIEW", "W", title="Rosanna (Live)", duration=345.0)
    vault.set_aside(worse_path, vault.masters / "Rock/Toto/Toto IV/Toto - Rosanna.m4a")
    tie_path = vault.review_dir / "Toto/x/Toto - Rosanna.m4a"
    tie = vault.row(tie_path, "DUPE_REVIEW", "T", duration=332.5)
    vault.set_aside(tie_path, vault.masters / "Rock/Toto/Toto IV/Toto - Rosanna.m4a")
    losers: list[int] = []
    wins, _ = vault.mod.review_wins(vault.conn, losers)
    assert [p.review["id"] for p in wins] == [better]
    assert sorted(losers) == sorted([worse, tie]), "1.5 s longer is a tie, and a tie deletes"


def test_limit_promotes_only_the_first_n(vault, monkeypatch, capsys):
    """A one-song trial before the rest: --promote --execute --limit 1."""
    _lib_and_better_review(vault)
    other_lib = vault.masters / "Rock/Toto/Toto IV/Toto - Africa.m4a"
    vault.row(other_lib, "CATALOGUED", "L2", title="Africa", album="Toto IV")
    other_rev = vault.review_dir / "Toto/x/Toto - Africa.m4a"
    vault.row(other_rev, "DUPE_REVIEW", "R2", title="Africa", sample_rate=96000)
    vault.set_aside(other_rev, other_lib)
    cfg = SimpleNamespace(
        db_path=vault.cfg.db_path, alac_archive=vault.masters, runs_root=vault.cfg.runs_root
    )
    monkeypatch.setattr(vault.mod.MusicConfig, "from_env", staticmethod(lambda: cfg))
    monkeypatch.setattr(sys, "argv", ["prog", "--promote", "--execute", "--limit", "1"])
    assert vault.mod.main() == 0
    n = vault.conn.execute(
        "SELECT COUNT(*) FROM events WHERE event_type='SWAP_PROMOTED'"
    ).fetchone()[0]
    assert n == 1


def test_the_longest_of_copies_tied_with_each_other_is_the_one_judged(vault):
    """201.9 s ties 202.1 s, and 201.9 s ties a 200.0 s master: judged first by
    id, it hid the 202.1 s copy that beats the master (Codex review of #226)."""
    lib_path = vault.masters / "Rock/Toto/Toto IV/Toto - Rosanna.m4a"
    lib = vault.row(lib_path, "CATALOGUED", "L", album="Toto IV", duration=200.0)
    paths = [vault.review_dir / f"Toto/{n}/Toto - Rosanna.flac" for n in ("a", "b")]
    vault.row(paths[0], "DUPE_REVIEW", "A", codec="flac", album="Toto IV", duration=201.9)
    longer = vault.row(paths[1], "DUPE_REVIEW", "B", codec="flac", album="Toto IV", duration=202.1)
    for p in paths:
        vault.set_aside(p, lib_path)
    wins, _ = vault.mod.review_wins(vault.conn)
    assert [(p.review["id"], p.library["id"], p.step) for p in wins] == [(longer, lib, "length")]


def test_a_copy_with_the_masters_audio_is_ranked_as_baked_as_the_master(vault):
    """Ranked apart from the master, an unmeasured copy of a -18 LUFS master never
    looked baked and hid a longer copy that beats it (Codex review of #226, round 2)."""
    lib_path = vault.masters / "Rock/Toto/Toto IV/Toto - Rosanna.m4a"
    lib = vault.row(lib_path, "CATALOGUED", "L", album="Toto IV", duration=200.0, lufs=-18.0)
    same = vault.review_dir / "Toto/a/Toto - Rosanna.flac"
    other = vault.review_dir / "Toto/b/Toto - Rosanna.flac"
    vault.row(same, "DUPE_REVIEW", "L", codec="flac", album="Toto IV", duration=200.0)
    longer = vault.row(
        other, "DUPE_REVIEW", "B", codec="flac", album="Toto IV", duration=202.1, lufs=-18.0
    )
    for p in (same, other):
        vault.set_aside(p, lib_path)
    wins, _ = vault.mod.review_wins(vault.conn)
    assert [(p.review["id"], p.library["id"], p.step) for p in wins] == [(longer, lib, "length")]


def test_two_better_copies_of_one_song_promote_only_the_best(vault):
    """2026-10-03: each review copy was compared with the master alone, so a
    song with two better copies had BOTH promoted -- 40 duplicate masters.
    All copies of a song are ranked together; only the best one is promoted,
    and the others join the rows to delete."""
    lib, good, _, _ = _lib_and_better_review(vault, sample_rate=96000)
    lib_path = vault.masters / "Rock/Toto/Toto IV/Toto - Rosanna.m4a"
    best_path = vault.review_dir / "Toto/hi/Toto - Rosanna.m4a"
    best = vault.row(best_path, "DUPE_REVIEW", "B", sample_rate=192000)
    vault.set_aside(best_path, lib_path)
    losers: list[int] = []
    wins, tally = vault.mod.review_wins(vault.conn, losers)
    assert [(p.review["id"], p.library["id"]) for p in wins] == [(best, lib)]
    assert losers == [good], "beaten by a better copy of the same song"
    assert tally["beaten by a better review copy"] == 1


def test_retire_execute_really_runs_the_delete_tool(tmp_path, monkeypatch):
    """Codex audit 2026-10-16, F9: --execute took the exclusive masters lock
    before choosing a step, and --retire's child -- delete_reviewed_tracks.py
    --execute -- asked for the same lock, was refused (exit 75, "NOT RUN: the
    masters are in use by scripts/swap_review_copies.py") and deleted nothing.
    The test above mocks the child and passes execute=False, so it never saw
    this. Here the real tool runs, on a real master, in its own vault."""
    import subprocess as sp

    from musaeus.hasher import audio_hash

    mod = _load()
    master = tmp_path / "Libraries" / "ALAC-Archival" / "Rock/Toto/Toto IV/Toto - Rosanna.m4a"
    master.parent.mkdir(parents=True)
    sp.run(
        ["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi",
         "-i", "sine=frequency=440:duration=0.3", "-c:a", "alac", str(master)],
        check=True, stdin=sp.DEVNULL,
    )  # fmt: skip
    conn = sqlite3.connect(tmp_path / "musaeus.db")
    conn.execute(f"CREATE TABLE archive ({COLS})")
    conn.execute(
        "CREATE TABLE events (id INTEGER PRIMARY KEY, run_id TEXT, ts TEXT, event_type TEXT, "
        "file_path TEXT, old_value TEXT, new_value TEXT, stage TEXT, note TEXT)"
    )
    conn.execute(
        "INSERT INTO archive (id, file_path, audio_hash, status, artist, title) "
        "VALUES (1, ?, ?, 'CATALOGUED', 'Toto', 'Rosanna')",
        (str(master), audio_hash(master, strict=True)),
    )
    conn.commit()
    conn.close()
    (tmp_path / "_db_backups").mkdir()
    (tmp_path / "MetaData").mkdir()
    sqlite3.connect(tmp_path / "_db_backups" / "hash_index.db").execute(
        "CREATE TABLE denied_hashes (audio_hash TEXT PRIMARY KEY, reason TEXT, "
        "source_path TEXT, denied_at TEXT DEFAULT CURRENT_TIMESTAMP)"
    ).connection.commit()
    monkeypatch.setenv("MUSAEUS_VAULT_ROOT", str(tmp_path))
    monkeypatch.setenv("MUSAEUS_DB_PATH", str(tmp_path / "musaeus.db"))
    monkeypatch.setattr(mod, "to_retire", lambda conn, masters: [1])
    monkeypatch.setattr(sys, "argv", ["swap_review_copies.py", "--retire", "--execute"])

    rc = mod.main()

    assert not master.exists(), "the replaced master was not deleted"
    assert rc == 0


def _tie(v, *, review_first: bool, review_filed: bool = True):
    """A review copy equal to its library copy on every step of the keep rule."""
    lib_path = v.masters / "Rock/Toto/Toto IV/Toto - Rosanna.m4a"
    rev_path = v.review_dir / "Toto/Toto IV/Toto - Rosanna.m4a"
    filed = "2026-10-02 15:00:00" if review_filed else None

    def lib():
        return v.row(lib_path, "CATALOGUED", "L", finalized_at="2026-10-29 07:00:00")

    def rev():
        return v.row(rev_path, "DUPE_REVIEW", "R", finalized_at=filed)

    rev_id, lib_id = (rev(), lib()) if review_first else reversed((lib(), rev()))
    v.set_aside(rev_path, lib_path)
    return rev_id, lib_id


def test_a_tie_puts_back_the_copy_that_was_in_the_library_first(vault):
    # Grey, 2026-10-29: 9 downloads had replaced equal copies of Grey's (Aretha
    # "I Say A Little Prayer" among them). "Please make it the winner moving forward".
    rev, lib = _tie(vault, review_first=True)
    wins, tally = vault.mod.review_wins(vault.conn)
    assert [(p.review["id"], p.library["id"], p.step) for p in wins] == [
        (rev, lib, "tie: in the library first")
    ]


def test_a_tie_with_a_copy_never_filed_keeps_the_library_copy(vault):
    _tie(vault, review_first=True, review_filed=False)
    wins, tally = vault.mod.review_wins(vault.conn)
    assert wins == [] and tally["tie"] == 1


def test_a_tie_with_a_newer_copy_keeps_the_library_copy(vault):
    _tie(vault, review_first=False)
    wins, tally = vault.mod.review_wins(vault.conn)
    assert wins == [] and tally["tie"] == 1


def test_a_promoted_copy_keeps_the_bake_record_of_its_audio(vault):
    # CodeRabbit, review of #262: the copy is baked by its audio; the master that
    # held the record is retired after the promotion.
    lib_path = vault.masters / "Rock/Toto/Toto IV/Toto - Rosanna.m4a"
    rev_path = vault.review_dir / "Toto/Toto IV/Toto - Rosanna.m4a"
    rev = vault.row(rev_path, "DUPE_REVIEW", "X", finalized_at="2026-09-24 15:00:00")
    vault.row(lib_path, "CATALOGUED", "X", finalized_at="2026-10-29 07:00:00",
              lufs_baked_at="2026-09-24 19:06:34", lufs_baked_target=-18.0)  # fmt: skip
    vault.set_aside(rev_path, lib_path)
    wins, _ = vault.mod.review_wins(vault.conn)
    assert [p.review["id"] for p in wins] == [rev]

    vault.mod.promote(vault.conn, wins, execute=True)

    row = vault.conn.execute(
        "SELECT lufs_baked_at, lufs_baked_target FROM archive WHERE id=?", (rev,)
    ).fetchone()
    assert (row["lufs_baked_at"], row["lufs_baked_target"]) == ("2026-09-24 19:06:34", -18.0)


def test_of_tied_review_copies_the_one_filed_first_is_judged(vault):
    # CodeRabbit, review of #262: a later, unfiled copy 1 s longer was judged
    # instead, tied, and nothing was put back.
    lib_path = vault.masters / "Rock/Toto/Toto IV/Toto - Rosanna.m4a"
    first_path = vault.review_dir / "a/Toto - Rosanna.m4a"
    later_path = vault.review_dir / "b/Toto - Rosanna.m4a"
    first = vault.row(first_path, "DUPE_REVIEW", "R1", finalized_at="2026-10-02 15:00:00")
    vault.row(later_path, "DUPE_REVIEW", "R2", finalized_at=None, duration=332.0)
    vault.row(lib_path, "CATALOGUED", "L", finalized_at="2026-10-29 07:00:00")
    vault.set_aside(first_path, lib_path)
    vault.set_aside(later_path, lib_path)

    wins, _ = vault.mod.review_wins(vault.conn)

    assert [(p.review["id"], p.step) for p in wins] == [(first, "tie: in the library first")]


def test_a_review_copy_whose_audio_is_recorded_baked_elsewhere_is_baked(vault):
    # CodeRabbit, review of #262: the record sits on a third catalogued row, not on
    # the library copy; the review copy was ranked as an original and won on quality.
    lib, rev, _, _ = _lib_and_better_review(vault)
    vault.row(vault.masters / "Rock/Toto/Other/Toto - Rosanna.m4a", "CATALOGUED", "R",
              lufs_baked_at="2026-09-24 19:06:34")  # fmt: skip

    wins, tally = vault.mod.review_wins(vault.conn)

    assert wins == [] and tally["library (original/baked)"] == 1
