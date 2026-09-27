"""Act 2 on the originals batch (2026-09-26): 510 baked copies went unpaired.

twins   Most originals arrived twice -- from the 09-24 masters backup and
        the USB1 raw files -- so each sat in an EXACT group with its twin.
        NearDupe skipped every file that had ever been in an EXACT group,
        so no original was ever compared with the baked copy it replaces,
        and Act 3 filed both.
keeper  An EXACT group is named after the recording (dup_<hash>), so a new
        arrival identical to a library master joins the master's OLD group,
        where the master is still marked 'keep'. 'keep' was ranked like a
        moved copy ("already dealt with"), so the arrival won and the
        master was swapped for no reason (P!nk, Pointer Sisters, SRV).
measured  The -18 LUFS test ("looks baked") read each copy's own loudness.
        A new arrival is not measured until Act 3, so an arrival with the
        SAME audio as a baked-looking master never looked baked, and won:
        119 masters were swapped for identical copies of themselves.
"""

from __future__ import annotations

from pathlib import Path

from musaeus.config import MusicConfig
from musaeus.context import RunContext
from musaeus.db import open_db, upsert_archive
from musaeus.stages.dupe_resolver import DupeResolverStage
from musaeus.stages.neardupe import NearDupeStage, _group_id


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


def _row(
    ctx,
    path: Path,
    h: str,
    *,
    lufs=None,
    bitrate=900_000,
    filed=False,
    title="Brown Sugar",
    duration=None,
):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"x")
    upsert_archive(
        ctx.conn,
        {
            "file_path": str(path),
            "status": "CATALOGUED",
            "artist": "The Rolling Stones",
            "title": title,
            "album": "Sticky Fingers",
            "duration": duration,
            "audio_hash": h,
            "codec": "alac",
            "bitrate": bitrate,
            "size_bytes": 1,
        },
    )
    ctx.conn.execute("UPDATE archive SET lufs = ? WHERE file_path = ?", (lufs, str(path)))
    if filed:
        ctx.conn.execute(
            "UPDATE archive SET finalized_at = datetime('now') WHERE file_path = ?", (str(path),)
        )
    ctx.conn.commit()


def _exact(ctx, gid: str, h: str, *paths: Path, status: str = "pending") -> None:
    ctx.conn.executemany(
        "INSERT INTO duplicates (group_id, file_path, duplicate_type, status, audio_hash) "
        "VALUES (?, ?, 'EXACT', ?, ?)",
        [(gid, str(p), status, h) for p in paths],
    )
    ctx.conn.commit()


def test_twins_an_original_with_an_identical_twin_still_replaces_the_baked_copy(tmp_path):
    ctx = _ctx(tmp_path)
    baked = (
        ctx.config.alac_archive / "Rock" / "Stones" / "SF" / "The Rolling Stones - Brown Sugar.m4a"
    )
    _row(ctx, baked, "baked", lufs=-18.0, bitrate=1_400_000, filed=True)
    nuc = ctx.inbox / "NUC" / "The Rolling Stones - Brown Sugar.m4a"
    usb = ctx.inbox / "USB1" / "The Rolling Stones - Brown Sugar.m4a"
    _row(ctx, nuc, "orig")
    _row(ctx, usb, "orig")
    _exact(ctx, "dup_twins", "orig", nuc, usb)
    NearDupeStage().run(ctx)
    DupeResolverStage().run(ctx)
    live = [
        Path(r[0])
        for r in ctx.conn.execute("SELECT file_path FROM archive WHERE status='CATALOGUED'")
    ]
    assert baked not in live, "the baked copy was never paired with its original"
    assert len(live) == 1 and live[0] in (nuc, usb), live


def test_twins_identical_copies_are_not_also_paired_as_near_duplicates(tmp_path):
    ctx = _ctx(tmp_path)
    a, b = ctx.inbox / "a" / "x.m4a", ctx.inbox / "b" / "x.m4a"
    _row(ctx, a, "same")
    _row(ctx, b, "same")
    _exact(ctx, "dup_ab", "same", a, b)
    NearDupeStage().run(ctx)
    near = ctx.conn.execute(
        "SELECT COUNT(*) FROM duplicates WHERE duplicate_type='NEAR'"
    ).fetchone()[0]
    assert near == 0


def test_keeper_a_previous_keeper_is_not_ranked_as_already_dealt_with(tmp_path):
    ctx = _ctx(tmp_path)
    master = (
        ctx.config.alac_archive / "Rock" / "Stones" / "SF" / "The Rolling Stones - Brown Sugar.m4a"
    )
    arrival = ctx.inbox / "The Rolling Stones - Brown Sugar.m4a"
    _row(ctx, master, "same", lufs=-9.5, filed=True)
    _row(ctx, arrival, "same")
    # The shape Sentinel leaves: one group per recording, the master's row
    # kept from the run that chose it, the arrival's row new.
    _exact(ctx, "dup_same", "same", master, status="keep")
    _exact(ctx, "dup_same", "same", arrival)
    DupeResolverStage().run(ctx)
    assert master.is_file(), "the library master was swapped for an identical new copy"
    assert not arrival.exists()


def test_measured_an_identical_arrival_shares_the_masters_loudness(tmp_path):
    ctx = _ctx(tmp_path)
    master = (
        ctx.config.alac_archive / "Rock" / "Stones" / "SF" / "The Rolling Stones - Brown Sugar.m4a"
    )
    arrival = ctx.inbox / "NUC" / "The Rolling Stones - Brown Sugar.m4a"
    _row(ctx, master, "same", lufs=-18.0, filed=True)
    _row(ctx, arrival, "same")  # not measured yet
    _exact(ctx, "dup_same", "same", master, arrival)
    DupeResolverStage().run(ctx)
    assert master.is_file(), "the master was swapped for an identical, unmeasured copy"
    assert not arrival.exists()


def test_measured_a_different_unmeasured_original_still_beats_the_baked_copy(tmp_path):
    ctx = _ctx(tmp_path)
    baked = (
        ctx.config.alac_archive / "Rock" / "Stones" / "SF" / "The Rolling Stones - Brown Sugar.m4a"
    )
    original = ctx.inbox / "NUC" / "The Rolling Stones - Brown Sugar.m4a"
    _row(ctx, baked, "baked", lufs=-18.0, filed=True)
    _row(ctx, original, "orig")  # different audio, not measured yet
    ctx.conn.executemany(
        "INSERT INTO duplicates (group_id, file_path, duplicate_type, status, audio_hash) "
        "VALUES ('near_1', ?, 'NEAR', 'pending', ?)",
        [(str(baked), "baked"), (str(original), "orig")],
    )
    ctx.conn.commit()
    DupeResolverStage().run(ctx)
    assert original.is_file()
    assert not baked.exists(), "the baked copy was kept over its original"


def test_measured_the_same_holds_for_identical_copies_found_by_hash_alone(tmp_path):
    # No duplicates row at all: the resolver's second source, live rows that
    # share an audio hash.
    ctx = _ctx(tmp_path)
    master = (
        ctx.config.alac_archive / "Rock" / "Stones" / "SF" / "The Rolling Stones - Brown Sugar.m4a"
    )
    arrival = ctx.inbox / "NUC" / "The Rolling Stones - Brown Sugar.m4a"
    _row(ctx, master, "same", lufs=-18.0, filed=True)
    _row(ctx, arrival, "same")
    DupeResolverStage().run(ctx)
    assert master.is_file(), "the master was swapped for an identical, unmeasured copy"
    assert not arrival.exists()


def test_both_sources_never_move_every_copy_between_them(tmp_path):
    # The resolver's two sources rank the same pair: groups first, then
    # rows sharing an audio hash, from a list made BEFORE the groups moved
    # anything. Where they disagree, each moved the copy the other kept.
    # Here a manual `dedupe` decision marked the filed copy 'archive'
    # (dedupe only marks, it never moves): the group honours it, the
    # hash-only source ranks the filed copy first.
    ctx = _ctx(tmp_path)
    master = (
        ctx.config.alac_archive / "Rock" / "Stones" / "SF" / "The Rolling Stones - Brown Sugar.m4a"
    )
    arrival = ctx.inbox / "NUC" / "The Rolling Stones - Brown Sugar.m4a"
    _row(ctx, master, "same", lufs=-9.5, filed=True)
    _row(ctx, arrival, "same")
    _exact(ctx, "dup_same", "same", master, status="archive")
    _exact(ctx, "dup_same", "same", arrival)
    DupeResolverStage().run(ctx)
    assert master.is_file() or arrival.is_file(), "both copies were moved out"


def test_gone_a_member_with_no_catalogue_row_is_never_the_keeper(tmp_path):
    # A group outlives its paths: Act 3 files a member somewhere else and
    # the old path stays in the group with no catalogue row behind it. It
    # lost only because its missing codec read as lossy; against a lossy
    # live copy it tied, and won on "studio over live" -- so the only copy
    # left was moved out as the loser.
    ctx = _ctx(tmp_path)
    live = ctx.config.alac_archive / "Soul" / "Wilson Pickett - Mustang Sally (Live).m4a"
    _row(ctx, live, "live", filed=True)
    ctx.conn.execute(
        "UPDATE archive SET codec = 'aac', bitrate = 256000, title = 'Mustang Sally (Live)' "
        "WHERE file_path = ?",
        (str(live),),
    )
    gone = ctx.inbox / "Wilson Pickett - Mustang Sally.m4a"  # filed elsewhere since
    ctx.conn.executemany(
        "INSERT INTO duplicates (group_id, file_path, duplicate_type, status, audio_hash) "
        "VALUES ('near_2', ?, 'NEAR', 'pending', ?)",
        [(str(live), "live"), (str(gone), "studio")],
    )
    ctx.conn.commit()
    DupeResolverStage().run(ctx)
    assert live.is_file(), "the only copy was moved out for a path with nothing behind it"


# ── After the Act 2 of 2026-09-26: 39 more baked copies beside originals ──


def _library(ctx, name: str) -> Path:
    return ctx.config.alac_archive / "Rock" / "Stones" / "Live" / name


def _live_pair(ctx, *, orig_duration: float, orig_title: str | None = None):
    title = "Midnight Rambler (Live In Brussels)"
    baked = _library(ctx, f"The Rolling Stones - {title}.m4a")
    orig = _library(ctx, f"The Rolling Stones - {title} (2).m4a")
    _row(ctx, baked, "baked", lufs=-18.0, filed=True, title=title, duration=412.0)
    _row(
        ctx, orig, "orig", lufs=-7.9, filed=True, title=orig_title or title, duration=orig_duration
    )
    return baked, orig


def test_live_a_baked_live_copy_is_paired_with_its_own_original(tmp_path):
    # Two live titles were never paired, so that two different concerts are
    # not merged. A baked copy and its original are the SAME concert: the
    # same title, the same length. 32 were left beside their originals.
    ctx = _ctx(tmp_path)
    baked, orig = _live_pair(ctx, orig_duration=412.1)
    NearDupeStage().run(ctx)
    DupeResolverStage().run(ctx)
    assert orig.is_file()
    assert not baked.exists(), "the baked live copy was never paired with its original"


def test_live_two_concerts_of_one_song_are_still_not_paired(tmp_path):
    ctx = _ctx(tmp_path)
    _live_pair(ctx, orig_duration=398.0)  # a different night
    NearDupeStage().run(ctx)
    near = ctx.conn.execute("SELECT COUNT(*) FROM duplicates WHERE duplicate_type='NEAR'")
    assert near.fetchone()[0] == 0


def test_live_two_live_titles_that_differ_are_still_not_paired(tmp_path):
    ctx = _ctx(tmp_path)
    _live_pair(ctx, orig_duration=412.0, orig_title="Midnight Rambler (Live In Paris)")
    NearDupeStage().run(ctx)
    near = ctx.conn.execute("SELECT COUNT(*) FROM duplicates WHERE duplicate_type='NEAR'")
    assert near.fetchone()[0] == 0


def test_stuck_a_pair_closed_as_stale_is_judged_again_when_it_is_whole(tmp_path):
    # A pair closed as 'stale' was never flagged again: NearDupe treated any
    # row for the pair as already flagged. 7 baked copies sat stuck.
    ctx = _ctx(tmp_path)
    baked = _library(ctx, "The Rolling Stones - Start Me Up.m4a")
    orig = ctx.config.alac_archive / "Rock" / "Stones" / "Tattoo You" / baked.name
    _row(ctx, baked, "baked", lufs=-18.0, filed=True, title="Start Me Up")
    _row(ctx, orig, "orig", lufs=-9.0, filed=True, title="Start Me Up")
    gid = _group_id(str(baked), str(orig))
    ctx.conn.executemany(
        "INSERT INTO duplicates (group_id, file_path, duplicate_type, status, audio_hash) "
        "VALUES (?, ?, 'NEAR', 'stale', ?)",
        [(gid, str(baked), "baked"), (gid, str(orig), "orig")],
    )
    ctx.conn.commit()
    NearDupeStage().run(ctx)
    DupeResolverStage().run(ctx)
    assert orig.is_file()
    assert not baked.exists(), "a pair closed as stale was never looked at again"


def test_stuck_one_untrustworthy_group_does_not_freeze_a_sound_one(tmp_path):
    # Groups sharing a file are resolved together. One group whose file had
    # changed under it closed every group joined to it -- including a sound
    # NEAR pair of a baked copy and its original.
    ctx = _ctx(tmp_path)
    baked = _library(ctx, "The Rolling Stones - Start Me Up.m4a")
    orig = ctx.config.alac_archive / "Rock" / "Stones" / "Tattoo You" / baked.name
    _row(ctx, baked, "baked", lufs=-18.0, filed=True, title="Start Me Up")
    _row(ctx, orig, "orig", lufs=-9.0, filed=True, title="Start Me Up")
    ctx.conn.execute(
        "INSERT INTO duplicates (group_id, file_path, duplicate_type, status, audio_hash) "
        "VALUES ('crossdupe_old', ?, 'CROSS_BATCH', 'pending', 'an-older-recording')",
        (str(baked),),
    )
    ctx.conn.executemany(
        "INSERT INTO duplicates (group_id, file_path, duplicate_type, status, audio_hash) "
        "VALUES ('near_sound', ?, 'NEAR', 'pending', ?)",
        [(str(baked), "baked"), (str(orig), "orig")],
    )
    ctx.conn.commit()
    result = DupeResolverStage().run(ctx)
    assert any("different recording" in e for e in result.errors), "the bad group is still named"
    assert orig.is_file()
    assert not baked.exists(), "a sound pair was frozen by an unrelated bad group"


def test_keep_both_follows_the_recordings_not_the_file_names(tmp_path):
    # Grey chose to keep both copies of 17 different versions (2026-09-26).
    # The mark named the files by path; Act 3 then renamed one ("... (Live)
    # (2)" -> "... (Live)"), and the next NearDupe would have paired them
    # again and moved the live copy.
    ctx = _ctx(tmp_path)
    studio = _library(ctx, "Wilson Pickett - Mustang Sally.m4a")
    live = _library(ctx, "Wilson Pickett - Mustang Sally (Live).m4a")
    _row(ctx, studio, "studio", lufs=-12.0, filed=True, title="Mustang Sally", duration=180.0)
    _row(ctx, live, "live", lufs=-11.0, filed=True, title="Mustang Sally (Live)", duration=250.0)
    old_live = str(live.with_name("Wilson Pickett - Mustang Sally (Live) (2).m4a"))
    ctx.conn.executemany(
        "INSERT INTO duplicates (group_id, file_path, duplicate_type, status, audio_hash) "
        "VALUES ('near_kept', ?, 'NEAR', 'keep', ?)",
        [(str(studio), "studio"), (old_live, "live")],
    )
    ctx.conn.commit()
    NearDupeStage().run(ctx)
    DupeResolverStage().run(ctx)
    assert live.is_file() and studio.is_file(), "a keep-both pair was split after a rename"


def test_open_every_group_of_a_resolved_component_is_closed(tmp_path):
    # Groups sharing a file are resolved as one. Only the first group's rows
    # were marked keep/archive; the others stayed 'pending' (145 after the
    # Act 2 of 2026-09-26) and came back on a later Act 2 as "nothing moved
    # -- the file at that path is now a different recording" once Act 3 had
    # renamed their files.
    ctx = _ctx(tmp_path)
    a = _library(ctx, "The Rolling Stones - Brown Sugar.m4a")
    b = ctx.config.alac_archive / "Rock" / "Stones" / "SF" / a.name
    c = ctx.config.alac_archive / "Rock" / "Stones" / "Hits" / a.name
    _row(ctx, a, "a", lufs=-9.0, bitrate=2_000_000, filed=True)
    _row(ctx, b, "b", lufs=-9.0, bitrate=1_000_000, filed=True)
    _row(ctx, c, "c", lufs=-9.0, bitrate=900_000, filed=True)
    ctx.conn.executemany(
        "INSERT INTO duplicates (group_id, file_path, duplicate_type, status, audio_hash) "
        "VALUES (?, ?, 'NEAR', 'pending', ?)",
        [
            ("near_1", str(a), "a"),
            ("near_1", str(b), "b"),
            ("near_2", str(b), "b"),
            ("near_2", str(c), "c"),
        ],
    )
    ctx.conn.commit()
    DupeResolverStage().run(ctx)
    assert a.is_file() and not b.exists() and not c.exists()
    left = ctx.conn.execute(
        "SELECT group_id, file_path FROM duplicates WHERE status = 'pending'"
    ).fetchall()
    assert not left, [tuple(r) for r in left]


def test_keep_both_decided_on_an_acoustid_pair_holds_too(tmp_path):
    # Grey decides AcoustID's same-recording pairs (2026-09-27: 29 "keep
    # both"). NearDupe honoured a keep-both only on its own NEAR groups.
    ctx = _ctx(tmp_path)
    studio = _library(ctx, "Wilson Pickett - Mustang Sally.m4a")
    mono = _library(ctx, "Wilson Pickett - Mustang Sally (Mono).m4a")
    _row(ctx, studio, "stereo", lufs=-12.0, filed=True, title="Mustang Sally", duration=180.0)
    _row(ctx, mono, "mono", lufs=-11.0, filed=True, title="Mustang Sally (Mono)", duration=180.4)
    ctx.conn.executemany(
        "INSERT INTO duplicates (group_id, file_path, duplicate_type, status, audio_hash) "
        "VALUES ('acoustic_1', ?, 'ACOUSTIC', 'keep', ?)",
        [(str(studio), "stereo"), (str(mono), "mono")],
    )
    ctx.conn.commit()
    NearDupeStage().run(ctx)
    DupeResolverStage().run(ctx)
    assert studio.is_file() and mono.is_file(), "a keep-both AcoustID pair was split"
