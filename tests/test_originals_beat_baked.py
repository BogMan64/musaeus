"""An original always beats an old -18 LUFS baked copy (Grey, 2026-09-26).

About 1,170 of the library's masters turned out to be copies baked to -18
LUFS by the retired edition script -- from the old ALAC_Library and the
Sept 5 archive -- not originals. Their originals are being brought back in,
and DupeResolver must keep the original whatever its bitrate: a baked copy
has been loudness-processed (sometimes compressed), and a master is the
untouched recording.

A baked copy is recognised by its measured loudness: Forge's LUFS sits at
-18.0 (within 0.3). A newly arrived original has not been measured yet at
Act 2, so it is never mistaken for one.
"""

from __future__ import annotations

from pathlib import Path

from musaeus.config import MusicConfig
from musaeus.context import RunContext
from musaeus.db import open_db, upsert_archive
from musaeus.stages.dupe_resolver import DupeResolverStage, _keeper_sort_key


def _ctx(tmp_path: Path) -> RunContext:
    cfg = MusicConfig(
        vault_root=tmp_path,
        inbox=tmp_path / "INBOX",
        staging=tmp_path / "STAGING",
        quarantine=tmp_path / "QUARANTINE",
        runs_root=tmp_path / "RUNS",
        meta_dir=tmp_path / "MetaData",
        alac_library=tmp_path / "Libraries" / "ALAC_Library",
        alac_archive=tmp_path / "Libraries" / "ALAC-Archival",
        db_path=tmp_path / "musaeus.db",
    )
    cfg.ensure_dirs()
    return RunContext.new(cfg, open_db(cfg.db_path), dry_run=False)


def _near_pair(ctx: RunContext, library_lufs: float | None) -> tuple[Path, Path]:
    master = ctx.config.alac_archive / "Rock" / "Dion" / "A" / "Dion - Song.m4a"
    original = ctx.inbox / "Dion - Song.m4a"
    rows = (
        (master, 1_400_000, "a", library_lufs, True),  # higher bitrate: it would win on quality
        (original, 900_000, "b", None, False),  # just arrived: not measured yet
    )
    for path, bitrate, h, lufs, filed in rows:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"x")
        upsert_archive(
            ctx.conn,
            {
                "file_path": str(path),
                "status": "CATALOGUED",
                "artist": "Dion",
                "title": "Song",
                "album": "A",
                "audio_hash": h,
                "codec": "alac",
                "bitrate": bitrate,
                "size_bytes": 1,
            },
        )
        ctx.conn.execute("UPDATE archive SET lufs = ? WHERE file_path = ?", (lufs, str(path)))
        if filed:
            ctx.conn.execute(
                "UPDATE archive SET finalized_at = datetime('now') WHERE file_path = ?",
                (str(path),),
            )
    ctx.conn.executemany(
        "INSERT INTO duplicates (group_id, file_path, duplicate_type, status, audio_hash) "
        "VALUES ('near_x', ?, 'NEAR', 'pending', ?)",
        [(str(master), "a"), (str(original), "b")],
    )
    ctx.conn.commit()
    return master, original


def test_the_original_is_kept_over_a_baked_copy_of_higher_bitrate(tmp_path):
    ctx = _ctx(tmp_path)
    baked, original = _near_pair(ctx, library_lufs=-18.0)
    DupeResolverStage().run(ctx)
    assert original.is_file(), "the original was moved out in favour of a baked copy"
    assert not baked.exists(), "the baked copy should have gone to review"


def test_a_master_at_its_own_loudness_still_wins_on_quality(tmp_path):
    ctx = _ctx(tmp_path)
    master, arrival = _near_pair(ctx, library_lufs=-11.6)
    DupeResolverStage().run(ctx)
    assert master.is_file() and not arrival.exists()


def test_the_rule_sits_below_lossless_and_above_the_rest():
    lossy_original = {"codec": "aac", "bitrate": 256_000, "lufs": None}
    lossless_baked = {"codec": "alac", "bitrate": 900_000, "lufs": -18.0}
    assert _keeper_sort_key(lossless_baked) < _keeper_sort_key(lossy_original), (
        "a lossless baked copy still beats a lossy original: what encoding threw away "
        "matters more than a loudness pass"
    )
