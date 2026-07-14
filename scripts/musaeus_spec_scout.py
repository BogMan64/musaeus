#!/usr/bin/env python3
"""
MUSAEUS — Spec Scout
Scan the library for audio spec outliers: unexpected bitrates, codecs,
sample rates, or bit depths that don't match what they claim to be.

What it does:
  - Loads all CATALOGUED tracks with codec/bitrate/sample_rate/bit_depth
  - Flags outliers:
    * LOSSLESS_LOW_BITRATE: FLAC/ALAC/WAV with suspiciously low bitrate (<300 kbps)
    * LOSSY_HIGH_BITRATE: MP3/AAC with unusually high bitrate (>500 kbps — likely mislabelled)
    * WRONG_EXTENSION: file extension doesn't match detected codec
    * LOW_SAMPLE_RATE: sample_rate < 44100 Hz (below CD quality)
    * HIGH_BITRATE_MP3: MP3 > 320 kbps (impossible — likely corrupt metadata)
    * MONO_ANOMALY: mono track in an album where all others are stereo
    * VERY_SHORT: duration < 10s (might be a sample/jingle misplaced in library)
  - Writes spec_scout_report.txt to RUNS_ROOT
  - Optional --csv to write a CSV for spreadsheet review

Usage:
    python3 scripts/musaeus_spec_scout.py
    python3 scripts/musaeus_spec_scout.py --csv
    python3 scripts/musaeus_spec_scout.py --min-bitrate 128 --max-bitrate 400

ORPHEUS equivalent: SCRIPTS/orpheus_spec_scout.py,
                    SCRIPTS/orpheus_audio_analyzer.py
"""

from __future__ import annotations

import argparse
import csv
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from musaeus.config import LOSSLESS_EXTENSIONS, LOSSY_EXTENSIONS, get_config
from musaeus.db import open_db

# ── Issue definitions ─────────────────────────────────────────────────────────

# Extension → expected codec fragment (lowercase)
_EXT_CODEC_MAP: dict[str, str] = {
    ".mp3": "mp3",
    ".flac": "flac",
    ".m4a": "aac",
    ".aac": "aac",
    ".ogg": "vorbis",
    ".wav": "pcm",
    ".alac": "alac",
    ".aiff": "pcm",
    ".aif": "pcm",
}


# ── Data gathering ────────────────────────────────────────────────────────────


def _gather(conn) -> list[dict]:  # type: ignore[type-arg]
    rows = conn.execute(
        """
        SELECT file_path, artist, album, title,
               bitrate, codec, sample_rate, channels, duration
        FROM archive
        WHERE status = 'CATALOGUED'
        ORDER BY artist, album, file_path
        """
    ).fetchall()
    return [dict(r) for r in rows]


# ── Analysis ──────────────────────────────────────────────────────────────────


def _analyse(rows: list[dict], min_bitrate: int, max_bitrate: int) -> list[dict]:
    issues: list[dict] = []

    # Group by album for mono-anomaly detection
    album_channels: dict[str, list[int]] = defaultdict(list)
    for row in rows:
        ch = row.get("channels") or 0
        if ch:
            album_channels[row.get("album") or ""].append(int(ch))

    for row in rows:
        fp = row["file_path"]
        ext = Path(fp).suffix.lower()
        codec = (row.get("codec") or "").lower()
        bitrate = int(row.get("bitrate") or 0)
        sample_rate = int(row.get("sample_rate") or 0)
        channels = int(row.get("channels") or 0)
        duration = float(row.get("duration") or 0)

        def flag(
            issue_type: str,
            detail: str,
            _fp: str = fp,
            _row: dict = row,  # type: ignore[type-arg]
            _bitrate: int = bitrate,
            _codec: str = codec,
            _sample_rate: int = sample_rate,
        ) -> None:
            issues.append(
                {
                    "file_path": _fp,
                    "artist": _row.get("artist") or "",
                    "album": _row.get("album") or "",
                    "title": _row.get("title") or "",
                    "issue_type": issue_type,
                    "detail": detail,
                    "bitrate": _bitrate,
                    "codec": _codec,
                    "sample_rate": _sample_rate,
                }
            )

        # Lossless with suspiciously low bitrate
        if ext in LOSSLESS_EXTENSIONS and bitrate and bitrate < 300:
            flag(
                "LOSSLESS_LOW_BITRATE",
                f"FLAC/ALAC/WAV at only {bitrate} kbps — may be corrupt or mislabelled",
            )

        # Lossy with impossibly high bitrate
        if ext in LOSSY_EXTENSIONS and bitrate and bitrate > 500:
            flag(
                "LOSSY_HIGH_BITRATE",
                f"{ext} at {bitrate} kbps — likely mislabelled lossless",
            )

        # MP3 above 320
        if ext == ".mp3" and bitrate and bitrate > 320:
            flag(
                "HIGH_BITRATE_MP3",
                f"MP3 at {bitrate} kbps > 320 — impossible bitrate, corrupt metadata?",
            )

        # Extension / codec mismatch
        expected_codec = _EXT_CODEC_MAP.get(ext, "")
        if expected_codec and codec and expected_codec not in codec:
            flag(
                "WRONG_EXTENSION",
                f"Extension {ext} but codec is '{codec}' — file may be mislabelled",
            )

        # Low sample rate
        if sample_rate and sample_rate < 44100:
            flag(
                "LOW_SAMPLE_RATE",
                f"Sample rate {sample_rate} Hz < 44100 Hz (below CD quality)",
            )

        # Bitrate below custom threshold
        if min_bitrate and bitrate and bitrate < min_bitrate:
            flag(
                "BELOW_MIN_BITRATE",
                f"Bitrate {bitrate} kbps < threshold {min_bitrate} kbps",
            )

        # Bitrate above custom threshold (for lossy)
        if max_bitrate and bitrate and ext in LOSSY_EXTENSIONS and bitrate > max_bitrate:
            flag(
                "ABOVE_MAX_BITRATE",
                f"Lossy bitrate {bitrate} kbps > threshold {max_bitrate} kbps",
            )

        # Very short track
        if duration and duration < 10:
            flag(
                "VERY_SHORT",
                f"Duration {duration:.1f}s < 10s — sample, jingle, or corrupt?",
            )

        # Mono anomaly (mono in a predominantly stereo album)
        if channels == 1:
            album = row.get("album") or ""
            ch_list = album_channels.get(album, [])
            stereo_count = sum(1 for c in ch_list if c == 2)
            if stereo_count > len(ch_list) * 0.8:
                flag(
                    "MONO_ANOMALY",
                    f"Mono track in album where {stereo_count}/{len(ch_list)} tracks are stereo",
                )

    return issues


# ── Report rendering ──────────────────────────────────────────────────────────


def _print_report(issues: list[dict], cfg, write_csv: bool) -> None:
    runs_root = cfg.runs_root
    runs_root.mkdir(parents=True, exist_ok=True)
    report_path = runs_root / "spec_scout_report.txt"

    # Group by issue type for summary
    by_type: dict[str, int] = defaultdict(int)
    for issue in issues:
        by_type[issue["issue_type"]] += 1

    lines = [
        "MUSAEUS SPEC SCOUT REPORT",
        f"Vault  : {cfg.vault_root}",
        f"Issues : {len(issues)} total",
        "=" * 72,
        "",
        "Summary by issue type:",
    ]
    for issue_type, count in sorted(by_type.items(), key=lambda x: -x[1]):
        lines.append(f"  {issue_type:<28}  {count:>5}")
    lines.append("")
    lines.append("Detail:")
    lines.append("")

    for issue in issues:
        lines.append(f"  [{issue['issue_type']}]")
        lines.append(f"    {issue['artist']} — {issue['title']}")
        lines.append(f"    {issue['file_path']}")
        lines.append(f"    {issue['detail']}")
        lines.append("")

    report_text = "\n".join(lines)
    print(report_text)

    with open(report_path, "w", encoding="utf-8") as f:
        f.write(report_text)
    print(f"\nReport written to: {report_path}")

    if write_csv:
        csv_path = runs_root / "spec_scout_report.csv"
        with open(csv_path, "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(
                f,
                fieldnames=[
                    "issue_type",
                    "artist",
                    "album",
                    "title",
                    "bitrate",
                    "codec",
                    "sample_rate",
                    "detail",
                    "file_path",
                ],
            )
            w.writeheader()
            for issue in issues:
                w.writerow(issue)
        print(f"CSV written to   : {csv_path}")


# ── Main ──────────────────────────────────────────────────────────────────────


def main() -> None:
    parser = argparse.ArgumentParser(description="MUSAEUS spec scout — find audio spec outliers.")
    parser.add_argument("--csv", action="store_true", help="Also write a CSV report")
    parser.add_argument(
        "--min-bitrate",
        type=int,
        default=0,
        metavar="KBPS",
        help="Flag lossy tracks below this bitrate (default: 0 = disabled)",
    )
    parser.add_argument(
        "--max-bitrate",
        type=int,
        default=0,
        metavar="KBPS",
        help="Flag lossy tracks above this bitrate (default: 0 = disabled)",
    )
    args = parser.parse_args()

    try:
        cfg = get_config()
    except ValueError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(1)

    conn = open_db(cfg.db_path)
    try:
        rows = _gather(conn)
    finally:
        conn.close()

    print(f"Loaded {len(rows):,} catalogued tracks.")
    issues = _analyse(rows, min_bitrate=args.min_bitrate, max_bitrate=args.max_bitrate)
    _print_report(issues, cfg, write_csv=args.csv)
    sys.exit(0)


if __name__ == "__main__":
    main()
