"""`musaeus export itunes`: what the iTunes Companion may touch.

The Companion (strawberry_companion/modules/itunes.py, load_musaeus_manifest)
refuses a manifest whose paths leave library_root, that repeats a track, or
whose playlists name a track not listed. These pin what MUSAEUS writes.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from musaeus import itunes_export
from musaeus.config import MusicConfig
from musaeus.db import open_db
from musaeus.edition_build import marker_for
from musaeus.edition_ledger import ledger_path, open_ledger


@pytest.fixture
def cfg(tmp_path: Path) -> MusicConfig:
    c = MusicConfig(
        vault_root=tmp_path,
        inbox=tmp_path / "INBOX",
        staging=tmp_path / "STAGING",
        quarantine=tmp_path / "QUARANTINE",
        runs_root=tmp_path / "RUNS",
        meta_dir=tmp_path / "MetaData",
        alac_library=tmp_path / "Libraries" / "ALAC_Library",
        db_path=tmp_path / "musaeus.db",
    )
    c.ensure_dirs()
    open_db(c.db_path).close()
    return c


@pytest.fixture(autouse=True)
def _marker_is_the_file_text(monkeypatch):
    """A test copy's marker is its text (a real copy carries it as an MP4 tag)."""

    def read(p):
        try:
            return Path(p).read_text(encoding="utf-8")
        except OSError:
            return None

    monkeypatch.setattr("musaeus.edition_bake.read_marker", read)


def _copy(cfg, rel: str, *, on_disk: bool = True) -> tuple[Path, Path]:
    """A master and its lossless copy, recorded in the ledger."""
    master = cfg.alac_archive / rel
    out = cfg.alac_library / rel
    if on_disk:
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(marker_for(rel), encoding="utf-8")
    ledger = open_ledger(ledger_path(cfg))
    ledger.execute(
        "INSERT INTO edition_copies (edition, master_hash, master_path, output_path, built_at) "
        "VALUES ('lossless', ?, ?, ?, '2026-11-11')",
        (rel, str(master), str(out)),
    )
    ledger.commit()
    ledger.close()
    return master, out


def _event(cfg, kind: str, file_path, old, new) -> None:
    conn = open_db(cfg.db_path)
    conn.execute(
        "INSERT INTO events (run_id, event_type, file_path, old_value, new_value) "
        "VALUES ('r', ?, ?, ?, ?)",
        (kind, str(file_path), old and str(old), new and str(new)),
    )
    conn.commit()
    conn.close()


def _moved(cfg, old: Path, new: Path, kind: str = "ORGANIZE_MOVE") -> None:
    _event(cfg, kind, new, old, new)


def test_the_lossless_copies_on_disk_are_the_tracks(cfg):
    _, a = _copy(cfg, "Rock/A/X/A - One.m4a")
    _copy(cfg, "Rock/A/X/A - Gone.m4a", on_disk=False)
    m = itunes_export.build(cfg)
    assert m["schema"] == "musaeus-itunes-bridge/v1"
    assert m["library_root"] == str(cfg.alac_library)
    assert m["tracks"] == [{"path": str(a), "previous_paths": []}]


def test_a_refiled_master_gives_its_copys_old_place_through_the_chain(cfg):
    master, out = _copy(cfg, "Rock/Band/Album/Band - Song.m4a")
    first = cfg.alac_archive / "Pop/Band/Unknown Album/Band - Song.m4a"
    second = cfg.alac_archive / "Rock/Band/Unsorted/Band - Song.m4a"
    _moved(cfg, first, second)
    _moved(cfg, second, master, "ORGANIZE_RENAME")
    _moved(cfg, cfg.inbox / "Band - Song.m4a", first)  # arrival: never in the library
    (track,) = itunes_export.build(cfg)["tracks"]
    assert track["previous_paths"] == [
        str(cfg.alac_library / "Rock/Band/Unsorted/Band - Song.m4a"),
        str(cfg.alac_library / "Pop/Band/Unknown Album/Band - Song.m4a"),
    ]


def test_an_old_place_another_song_holds_now_is_left_out(cfg):
    """The Companion would re-link the other song's iTunes entry to this one."""
    master, _ = _copy(cfg, "Rock/B/Y/B - New.m4a")
    _, taken = _copy(cfg, "Rock/B/Y/B - Old.m4a")
    _moved(cfg, cfg.alac_archive / "Rock/B/Y/B - Old.m4a", master)
    tracks = {t["path"]: t["previous_paths"] for t in itunes_export.build(cfg)["tracks"]}
    assert tracks[str(cfg.alac_library / "Rock/B/Y/B - New.m4a")] == []
    assert str(taken) in tracks


def test_playlists_name_only_listed_tracks(cfg):
    _, a = _copy(cfg, "Blues Rock/C/Z/C - Here.m4a")
    index = cfg.alac_library / "Playlists"
    index.mkdir(parents=True)
    (index / "Blues_Rock.m3u8").write_text(
        "#EXTM3U\n#EXTINF:-1,C - Here\n../Blues Rock/C/Z/C - Here.m4a\n"
        "../Blues Rock/C/Z/C - Missing.m4a\n",
        encoding="utf-8",
    )
    (index / "Empty.m3u8").write_text("#EXTM3U\n../Nowhere.m4a\n", encoding="utf-8")
    m = itunes_export.build(cfg)
    assert m["playlists"] == [{"name": "Blues Rock", "track_paths": [str(a)]}]


def test_the_file_is_written_whole_and_reads_back(cfg, tmp_path):
    _copy(cfg, "Rock/A/X/A - One.m4a")
    out = tmp_path / "Exports" / "bridge.json"
    itunes_export.write(itunes_export.build(cfg), out)
    assert json.loads(out.read_text(encoding="utf-8"))["tracks"]
    assert not list(out.parent.glob("*.tmp"))


def test_a_place_another_song_had_first_is_not_this_songs(cfg):
    """Local CodeRabbit: A moved X -> Y; later B moved Z -> X, then X -> W.
    A's history is X only; B's is X, then Z. X, claimed by both, is no
    proof of either and is left out."""
    a_master, a_out = _copy(cfg, "Rock/A/Y/A - Song.m4a")
    b_master, b_out = _copy(cfg, "Rock/B/W/B - Song.m4a")
    x = cfg.alac_archive / "Rock/X/X/X.m4a"
    z = cfg.alac_archive / "Rock/Z/Z/Z.m4a"
    _moved(cfg, x, a_master)  # A: X -> Y
    _moved(cfg, z, x)  # B: Z -> X
    _moved(cfg, x, b_master)  # B: X -> W
    tracks = {t["path"]: t["previous_paths"] for t in itunes_export.build(cfg)["tracks"]}
    assert tracks[str(a_out)] == []
    assert tracks[str(b_out)] == [str(cfg.alac_library / "Rock/Z/Z/Z.m4a")]


def _hash(cfg, path: Path, prefix: str) -> None:
    conn = open_db(cfg.db_path)
    conn.execute(
        "INSERT INTO events (run_id, event_type, file_path, new_value) "
        "VALUES ('r', 'HASH_COMPUTED', ?, ?)",
        (str(path), prefix + "…"),
    )
    conn.commit()
    conn.close()


def test_a_place_left_and_refilled_another_way_is_not_this_songs(cfg):
    """CodeRabbit on #321: A moved X -> Y, left Y for review, and B was
    finalized to Y. B's history must not reach X."""
    y_master, y_out = _copy(cfg, "Rock/Y/Y/Y.m4a")
    x = cfg.alac_archive / "Rock/X/X/X.m4a"
    _moved(cfg, x, y_master)  # A: X -> Y
    _moved(cfg, y_master, cfg.vault_root / "REVIEW/Y.m4a", "DUPE_MOVED_FOR_REVIEW")  # A leaves
    _moved(cfg, cfg.vault_root / "STAGING/Y.m4a", y_master, "FINALIZE_MOVE")  # B arrives
    (track,) = itunes_export.build(cfg)["tracks"]
    assert track["path"] == str(y_out) and track["previous_paths"] == []


def test_a_place_left_with_no_record_of_a_return_stops_the_history(cfg):
    y_master, _ = _copy(cfg, "Rock/Y/Y/Y.m4a")
    _moved(cfg, cfg.alac_archive / "Rock/X/X/X.m4a", y_master)
    _moved(cfg, y_master, cfg.vault_root / "REVIEW/Y.m4a", "DUPE_MOVED_FOR_REVIEW")
    (track,) = itunes_export.build(cfg)["tracks"]
    assert track["previous_paths"] == []


def test_another_recordings_fingerprint_at_the_old_place_stops_the_history(cfg):
    master, _ = _copy(cfg, "Rock/Y/Y/Y.m4a")  # its master_hash is the rel path
    x = cfg.alac_archive / "Rock/X/X/X.m4a"
    _hash(cfg, x, "deadbeef")
    _moved(cfg, x, master)
    (track,) = itunes_export.build(cfg)["tracks"]
    assert track["previous_paths"] == []


def test_a_place_visited_twice_does_not_end_the_history(cfg):
    """Local CodeRabbit on #321: Z -> A -> B -> A -> C reaches Z."""
    c_master, _ = _copy(cfg, "Rock/C/C/C.m4a")
    z, a, b = (cfg.alac_archive / f"Rock/{n}/{n}/{n}.m4a" for n in "ZAB")
    for old, new in ((z, a), (a, b), (b, a), (a, c_master)):
        _moved(cfg, old, new)
    (track,) = itunes_export.build(cfg)["tracks"]
    lib = cfg.alac_library
    assert track["previous_paths"] == [
        str(lib / "Rock/A/A/A.m4a"),
        str(lib / "Rock/B/B/B.m4a"),
        str(lib / "Rock/Z/Z/Z.m4a"),
    ]


def test_a_recorded_file_holding_another_masters_copy_is_not_its_track(cfg):
    """CodeRabbit on #321: a stale record whose path holds another copy."""
    _, out = _copy(cfg, "Rock/A/X/A - One.m4a")
    out.write_text(marker_for("another master"), encoding="utf-8")
    assert itunes_export.build(cfg)["tracks"] == []


@pytest.mark.parametrize(
    "kind, where, new, reaches_z",
    [
        ("DUPLICATE_FOUND", "INBOX/X.m4a", None, True),  # Sentinel: a copy of X arrived
        ("CANONICALIZE", None, "PASSTHROUGH", True),  # X's tags fixed in place
        ("CANONICALIZE", "STAGING/X.m4a", "CONVERTED", False),  # X itself left
    ],
)
def test_only_an_event_that_takes_the_file_away_is_a_departure(cfg, kind, where, new, reaches_z):
    """CodeRabbit on #321: Z -> X, then an event naming X, then X -> Y."""
    y_master, _ = _copy(cfg, "Rock/Y/Y/Y.m4a")
    z, x = (cfg.alac_archive / f"Rock/{n}/{n}/{n}.m4a" for n in "ZX")
    _moved(cfg, z, x)
    _event(cfg, kind, cfg.vault_root / where if where else x, x, new)
    _moved(cfg, x, y_master)
    (track,) = itunes_export.build(cfg)["tracks"]
    lib = cfg.alac_library
    assert track["previous_paths"] == [str(lib / "Rock/X/X/X.m4a")] + (
        [str(lib / "Rock/Z/Z/Z.m4a")] if reaches_z else []
    )


def test_a_failed_write_leaves_no_temporary_file(cfg, tmp_path, monkeypatch):
    """CodeRabbit on #321."""
    out = tmp_path / "Exports" / "bridge.json"

    def boom(*_):
        raise OSError("disk full")

    monkeypatch.setattr(itunes_export.os, "replace", boom)
    with pytest.raises(OSError, match="disk full"):
        itunes_export.write({"tracks": []}, out)
    assert not list(out.parent.iterdir())
