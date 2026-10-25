#!/usr/bin/env python3
"""
MUSAEUS — enforce the mono/stereo-only library rule.

Grey's rule, 2026-08-21: "Mono and stereo versions only, anything else
should be added to the TuneMyMusic.csv and the physical file deleted."

Why the library cannot just keep them: the AVR target and the whole
loudness pipeline are built for 2-channel audio. BPMStage already skips
multichannel outright, and the earlier TuneMyMusic.csv entries carry the
reason "multichannel audio (no stereo interest -- see BPM skip)" -- so
these files sit in the library consuming space while being excluded from
half the pipeline. Logging them for re-acquisition in stereo and removing
the 5.1 copy is the resolution.

This DELETES audio, which nothing else in MUSAEUS does. Accordingly:

  - It only ever touches rows where the probed channel count is > 2.
    Mono (1) and stereo (2) are never candidates.
  - It re-probes each file with ffprobe immediately before deleting and
    refuses to act if the live channel count disagrees with the DB. A
    stale row must never be the reason a file is destroyed.
  - Every file is written to TuneMyMusic.csv BEFORE it is deleted, so the
    re-source record exists even if the run dies midway.
  - The archive row and its hash-ledger entry go too, otherwise
    AuditStage would correctly start failing on a finalized row whose
    file is missing.
  - --check reports exactly what --apply would do.

Usage:
    python3 scripts/musaeus_purge_multichannel.py --check
    python3 scripts/musaeus_purge_multichannel.py --apply
"""

from __future__ import annotations

import argparse
import csv
import errno
import os
import shutil
import sqlite3
import stat
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from musaeus.config import MusicConfig, get_config  # noqa: E402

CSV_FIELDS = ["reason", "codec", "bitrate_kbps", "sample_rate", "channels", "duration_sec", "path"]


def _probe_channels(path: Path) -> int | None:
    """Live channel count, or None if it cannot be determined."""
    r = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-select_streams",
            "a:0",
            "-show_entries",
            "stream=channels",
            "-of",
            "csv=p=0",
            str(path),
        ],
        capture_output=True,
        text=True,
    )
    # ffprobe's csv writer still emits a field separator when the file
    # carries a second stream (these releases embed cover art as mjpeg),
    # so a bare isdigit() check rejects a perfectly good "6," and would
    # silently spare files this rule is meant to catch. Take the first
    # field rather than demanding the whole string be a number.
    first = r.stdout.strip().split(",")[0].strip()
    return int(first) if first.isdigit() else None


#: An in-place swap must be the whole song: Canonicalize's 2-second tolerance let a
#: 1-second copy replace a 2-second song (Codex review of #237). AAC priming is ~0.05 s.
_SWAP_TOLERANCE_S = 0.25


def _downmix_to_stereo(src: Path, dest: Path) -> str | None:
    """Write a stereo copy with Canonicalize's own encoders (Grey, 2026-10-24).

    Returns the codec written ("alac"/"aac"), or None when the song is kept as it is.

    Cover art and tags are kept, lossless stays ALAC and lossy is AAC, and the
    copy is verified (length; mono or stereo) before it replaces anything. The
    hand-rolled command it replaces dropped the art, turned AAC into ALAC, and
    wrote to a ".tmp" name ffmpeg could not choose a format for -- every
    downmix failed, and the fallback deleted the song.
    """
    from musaeus.config import LOSSLESS_CODECS
    from musaeus.stages import canonicalize as canon

    # The file decides, never the row: a stale "aac" row made lossless ALAC lossy
    # (Codex review of #237). Unreadable or unknown: kept as it is.
    # In place only for .m4a: the encoders write MP4, and a .flac under its old
    # name would be MP4 its row calls FLAC (Codex review of #237, round 2).
    if src.suffix.lower() != ".m4a":
        print(f"    downmix refused: {src.suffix} is not .m4a -- left for a person")
        return None
    codec = canon.CanonicalizeStage._resolve_codec({}, src)
    if codec in LOSSLESS_CODECS:
        encode = canon._convert_to_alac
    elif codec in canon._TRANSCODABLE_CODECS:
        encode = canon._transcode_to_aac
    else:
        print(f"    downmix refused: codec {codec or '(unreadable)'!r}")
        return None
    try:
        encode(src, dest)  # writes mp4 whatever the name ("-f mp4")
        # ffmpeg keeps the plain atoms but drops every freeform tag -- the
        # MusicBrainz ids, BPM, key. The master's own tags go across, minus
        # loudness, which Forge measures again; and the song keeps its file
        # permissions (Codex review of #237, round 8). BEFORE the checks, so they
        # read the final file (round 9).
        from musaeus.edition_bake import copy_tags

        copy_tags(src, dest, None)
        os.chmod(dest, stat.S_IMODE(os.stat(src).st_mode))
        # Its extended attributes too; one that cannot be set stops the swap
        # (Codex review of #237, round 14).
        try:
            names = os.listxattr(src)
        except OSError as exc:
            # Only "this filesystem has none" is nothing to carry; any other
            # error stops the swap (Codex review of #237, round 15).
            if exc.errno not in (errno.ENOTSUP, errno.EOPNOTSUPP):
                raise
            names = []
        for name in names:
            os.setxattr(dest, name, os.getxattr(src, name))
        canon._verify_conversion(src, dest)
        from musaeus.duration import container_seconds, decodes_cleanly, stream_seconds

        # The audio stream too: a container can claim a length its audio does
        # not have (Codex review of #237, round 5).
        for seconds in (container_seconds, stream_seconds):
            a, b = seconds(src), seconds(dest)
            if a is None or b is None or abs(a - b) > _SWAP_TOLERANCE_S:
                raise RuntimeError(f"length changed ({a} s -> {b} s)")
        ok, err = decodes_cleanly(dest)  # the header can lie; the decode cannot
        if not ok:
            raise RuntimeError(f"the new copy does not decode cleanly: {err}")
    except Exception as exc:  # noqa: BLE001 -- any failure: the song is kept as it is
        print(f"    downmix failed: {str(exc)[:90]}")
        dest.unlink(missing_ok=True)
        return None
    return "alac" if encode is canon._convert_to_alac else "aac"


def _remake_lossless_copy(cfg: MusicConfig, audio_hash: str, master: Path) -> None:
    """The Lossless edition copy of a downmixed master goes, to be made again.

    The planner trusts the kept audio_hash and would only retag that copy, so it
    stayed surround (Codex review of #237, round 11). It is removed with its
    record and the measurements cached for the old audio; the next edition build
    makes it from the stereo master. The AAC editions fold surround to stereo
    already (edition_bake.aac_before_loudnorm) and are left alone.
    """
    from musaeus import edition_ledger as el

    path = el.ledger_path(cfg)
    if not path.exists():
        return
    conn = el.open_ledger(path)
    try:
        copy = el.copies(conn, "lossless").get(audio_hash)
        conn.execute("DELETE FROM measurements WHERE master_hash = ?", (audio_hash,))
        conn.commit()
        if copy is not None:
            from musaeus.edition_bake import read_marker
            from musaeus.edition_build import LOSSLESS_KIND, marker_for

            # Only a copy marked as this master's: a stale record's path may
            # hold another song now (Codex review of #237, round 12) -- and only
            # inside the Lossless edition, never the master: a stale record
            # naming the master or a Car copy carries the same marker (round 16).
            out = Path(copy.output_path)
            inside = out.resolve().is_relative_to(LOSSLESS_KIND.root(cfg).resolve())
            if (
                inside
                and out.resolve() != Path(master).resolve()
                and out.exists()
                and read_marker(out) == marker_for(audio_hash)
            ):
                out.unlink()
            el.forget(conn, "lossless", audio_hash)
    finally:
        conn.close()


def _purge_row(
    conn: sqlite3.Connection,
    cfg: MusicConfig,
    row: sqlite3.Row,
    *,
    channels: int | None,
    note: str,
) -> None:
    """Remove one archive row, its duplicates entries and its ledger entry.

    All three together: a row removed while its ledger entry survives is
    precisely the condition that produced the 2026-08-17/18 dupe cascade
    (scope doc section 4.17), where a stale hash outlived the file it named
    and quarantined the next honest copy of that audio.
    """
    path = str(row["file_path"])
    conn.execute("DELETE FROM archive WHERE rowid = ?", (row["rid"],))
    conn.execute("DELETE FROM duplicates WHERE file_path = ?", (path,))
    conn.execute(
        """
        INSERT INTO events (run_id, ts, event_type, file_path, stage, note)
        VALUES (?, ?, 'MULTICHANNEL_PURGED', ?, 'purge-multichannel', ?)
        """,
        (
            "manual",
            datetime.now(tz=timezone.utc).isoformat(timespec="seconds"),
            path,
            f"{channels}ch removed -- {note}",
        ),
    )
    conn.commit()

    if row["audio_hash"] and cfg.hash_index_path.exists():
        idx = sqlite3.connect(cfg.hash_index_path)
        idx.execute("DELETE FROM finalized_hashes WHERE file_path = ?", (path,))
        idx.commit()
        idx.close()


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--check", action="store_true", help="Report only, delete nothing")
    g.add_argument("--apply", action="store_true", help="Log to CSV, then delete")
    ap.add_argument(
        "--downmix",
        action="store_true",
        help="Downmix >2ch to stereo IN PLACE instead of deleting (Grey, 2026-10-24). "
        "Keeps the song; a failed downmix leaves it as it is, never deletes it.",
    )
    ap.add_argument(
        "--include-quarantine",
        action="store_true",
        help="Also purge multichannel files sitting in DUPES_MOVED_FOR_REVIEW",
    )
    args = ap.parse_args()

    cfg = get_config()
    if args.apply:  # rewrites or deletes masters
        from musaeus.masters_lock import hold_for_process

        hold_for_process(exclusive=True, what="scripts/musaeus_purge_multichannel.py",
                         runs_root=cfg.runs_root)  # fmt: skip
    conn = sqlite3.connect(cfg.db_path)
    conn.row_factory = sqlite3.Row

    statuses = ("CATALOGUED", "DUPE_REVIEW") if args.include_quarantine else ("CATALOGUED",)
    placeholders = ",".join("?" for _ in statuses)
    rows = conn.execute(
        f"""
        SELECT rowid AS rid, file_path, artist, title, album, codec, bitrate,
               sample_rate, channels, duration, audio_hash, status, canon_action, chromaprint
          FROM archive
         WHERE status IN ({placeholders}) AND channels > 2
         ORDER BY artist, title
        """,
        statuses,
    ).fetchall()

    print(f"multichannel rows (>2 channels): {len(rows)}")
    if not rows:
        return 0

    csv_path = cfg.tunemymusic_csv_path
    known = set()
    # Only the delete mode writes the wanted list; --downmix never reads it.
    # Its "path" column predates today's Title,Artist,Album list, which made
    # a downmix fail before touching a song (first real run, 2026-10-25).
    if csv_path.exists() and not args.downmix:
        with csv_path.open(newline="", encoding="utf-8") as fh:
            reader = csv.DictReader(fh)
            if "path" not in (reader.fieldnames or []):
                # Writing this mode's rows into today's Title,Artist,Album list
                # would corrupt it, and the song would be deleted against the
                # keep-and-downmix rule (Codex review of #238).
                print(f"NOT RUN: {csv_path.name} has no 'path' column; use --downmix")
                return 1
            known = {r["path"] for r in reader}

    deleted = logged = mismatch = missing = downmixed = 0
    freed = 0

    for row in rows:
        path = Path(row["file_path"])

        # A row whose file is already gone still needs its row removed.
        # This is exactly how the aborted first run left two files, and
        # leaving the rows would keep AuditStage failing on finalized rows
        # with nothing behind them.
        if not path.exists():
            missing += 1
            # --downmix never deletes: a song missing for now keeps its row and
            # ledger entry (Codex review of #237, round 4).
            if args.downmix:
                print(f"  missing on disk, left as it is: {path.name}")
            elif not args.check:
                _purge_row(conn, cfg, row, channels=row["channels"], note="file already gone")
            continue

        # Re-probe: never destroy a file on the strength of a DB row alone.
        live = _probe_channels(path)
        if live is None or live <= 2:
            mismatch += 1
            print(f"  REFUSED (db says {row['channels']}ch, file says {live}): {path.name}")
            continue

        # The file must be this row's recording: another song in its place would
        # be changed under the old identity (Codex review of #237, round 13).
        if args.downmix:
            from musaeus.recording_identity import not_this_recording

            why = not_this_recording(path, row, row["audio_hash"])
            if why:
                mismatch += 1
                print(f"  REFUSED ({why}): {path.name}")
                continue

        if args.check:
            what = (
                (
                    "would downmix to stereo"
                    if path.suffix.lower() == ".m4a"
                    else "left for a person (not .m4a)"
                )
                if args.downmix
                else "would remove"
            )
            print(f"  {what}: {live}ch  {row['artist']} - {row['title']}")
            continue

        if args.downmix:
            # A name of its own, made here: a fixed ".stereo.tmp" overwrote a file
            # already there (Codex review of #237, round 4).
            fd, name = tempfile.mkstemp(prefix=".stereo-", suffix=".tmp", dir=path.parent)
            os.close(fd)
            tmp = Path(name)
            written = _downmix_to_stereo(path, tmp)
            if written is None:
                tmp.unlink(missing_ok=True)  # only the file made here
                print(f"  downmix failed, song kept as it is: {path.name}")
                continue
            from musaeus.hasher import file_hash

            # The swap cannot be left half done (Codex review of #237, round 5):
            # the new copy is hashed BEFORE anything moves, the original is set
            # aside, and put back if the catalogue update does not commit.
            try:
                new_hash, new_size = file_hash(tmp), tmp.stat().st_size
                from musaeus.stages.canonicalize import _probe_streams

                audio = [
                    s
                    for s in _probe_streams(tmp).get("streams", [])
                    if s.get("codec_type") == "audio"
                ]
                new_bitrate = int(audio[0].get("bit_rate") or 0) or None if audio else None
            except Exception:
                tmp.unlink(missing_ok=True)
                raise
            # The original never leaves its path: the backup is a hard link (a
            # copy where links cannot be made), and the new copy replaces it in
            # one atomic step (Codex review of #237, round 6).
            fd, aside = tempfile.mkstemp(prefix=".orig-", suffix=".tmp", dir=path.parent)
            os.close(fd)
            os.unlink(aside)
            try:
                os.link(path, aside)
            except OSError:
                shutil.copy2(path, aside)
            try:
                os.replace(tmp, path)
            except Exception:
                tmp.unlink(missing_ok=True)
                os.unlink(aside)
                raise
            try:
                # audio_hash stays: it names the recording as it arrived, as
                # Canonicalize keeps it for a downmixed arrival, so the same
                # 5.1 file arriving again is still known. The file's own hash
                # is new, its bit-rot baseline goes (a verify reports it as new,
                # not corrupt), and Forge re-measures the loudness.
                conn.execute(
                    # The old loudness described the 6-channel audio: the resolver
                    # reads lufs, and Forge could embed the old gain (Codex review of
                    # #237, round 10). Cleared, so Forge measures again.
                    "UPDATE archive SET channels = 2, codec = ?, full_hash = ?, size_bytes = ?, "
                    "bitrate = COALESCE(?, bitrate), lufs = NULL, lufs_tp = NULL, rg_gain = NULL, "
                    "rg_peak = NULL, rg_tagged_at = NULL WHERE rowid = ?",
                    (written, new_hash, new_size, new_bitrate, row["rid"]),
                )
                conn.execute("DELETE FROM archive_tier_hashes WHERE path = ?", (str(path),))
                conn.execute(
                    "INSERT INTO events (run_id, ts, event_type, file_path, stage, note) "
                    "VALUES (?, ?, 'MULTICHANNEL_DOWNMIXED', ?, 'purge-multichannel', ?)",
                    (
                        "manual",
                        datetime.now(tz=timezone.utc).isoformat(timespec="seconds"),
                        str(path),
                        f"{live}ch downmixed to stereo in place",
                    ),
                )
                # The Lossless copy goes BEFORE the stereo row commits: a failed
                # cleanup rolls the swap back, and a rerun still finds the song
                # (Codex review of #237, round 13).
                if row["audio_hash"]:
                    _remake_lossless_copy(cfg, row["audio_hash"], path)
                conn.commit()
            except Exception:
                conn.rollback()
                os.replace(aside, path)  # the original back where it was
                raise
            os.unlink(aside)
            downmixed += 1
            print(f"  downmixed {live}ch -> 2ch: {row['artist']} - {row['title']}")
            continue

        entry = {
            "reason": (
                f"{live}-channel audio -- library is mono/stereo only; "
                "re-source a stereo version (physical file deleted)"
            ),
            "codec": (row["codec"] or "").upper(),
            "bitrate_kbps": int((row["bitrate"] or 0) / 1000) or "",
            "sample_rate": row["sample_rate"] or "",
            "channels": live,
            "duration_sec": round(row["duration"], 1) if row["duration"] else "",
            "path": str(path),
        }

        # CSV first: the re-source record must survive a crash mid-run.
        if str(path) not in known:
            write_header = not csv_path.exists() or csv_path.stat().st_size == 0
            with csv_path.open("a", newline="") as fh:
                w = csv.DictWriter(fh, fieldnames=CSV_FIELDS)
                if write_header:
                    w.writeheader()
                w.writerow(entry)
            known.add(str(path))
            logged += 1

        size = path.stat().st_size

        # DB first, unlink last -- the reverse of SOP 4.12's move ordering,
        # and deliberately so. A move can be undone; a delete cannot. If the
        # DB work throws here the file simply survives and the next run
        # retries it. The first version of this script unlinked first and
        # then hit a schema mismatch on the events insert, which left two
        # files gone with their rows still claiming them present.
        _purge_row(conn, cfg, row, channels=live, note="logged to TuneMyMusic.csv for re-source")

        path.unlink()
        freed += size
        deleted += 1

    if downmixed:
        print(f"\ndownmixed to stereo (song kept): {downmixed}")
    print(f"logged to CSV: {logged}   deleted: {deleted}   freed: {freed / 1e9:.2f} GB")
    if mismatch:
        print(f"refused on channel mismatch: {mismatch}")
    if missing:
        print(f"already gone from disk: {missing}")
    conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
