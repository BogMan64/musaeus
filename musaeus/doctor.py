#!/usr/bin/env python3
"""
MUSAEUS — Doctor: one answer to "is the library healthy?"

Read-only. Opens the database with mode=ro and never writes, so it is safe
to run at any time, including while something else holds the write lock.

Why this exists: over 2026-08-21/22, answering that question took six
separate ad-hoc SQL queries every time something moved -- and things moved
a lot, from a dupe cascade, an overnight cron, dupeGuru, and PerfectTunes.
Each time the queries were retyped slightly differently, and twice I drew
a wrong conclusion from one of them (blaming a tool for files the owner had
moved; reporting 181 losses that were sitting in a backup under numeric
filename prefixes). A question asked that often deserves one implementation
with the traps already accounted for.

Two traps are baked in here rather than left to be rediscovered:

  Hash is the wrong test for NEAR duplicates. Different encodings of the
  same recording have different PCM hashes by definition, so a hash-based
  "is this audio held elsewhere" check reports every near-dupe as unique.
  Checking that almost deleted 1,002 songs. This compares artist+title
  with edition suffixes stripped as well as by hash.

  Missing on disk is not the same as lost. A file can be absent from its
  recorded path and still be present under another row, in quarantine, or
  in the archive tier. Those are three different situations and only one
  of them is bad.

Distinct from HealthStage (metadata quality: blank fields, odd values) and
`status` (counts and breakdowns). This is integrity: does the database
still describe what is actually on the disk?
"""

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path

from .config import MusicConfig

# The marker is often preceded by a year or a qualifier: "Slow Ride - 2016
# Remaster", "Layla - Acoustic Live". Allow a short run of words before it
# rather than requiring the marker to follow the dash immediately.
_EDITION_RE = re.compile(
    r"\s*-\s*(?:\w+\s+){0,2}"
    r"(remaster|remastered|live|mono|stereo|single|album|radio|rerecorded|version|mix|edit)\b.*$",
    re.IGNORECASE,
)
_BRACKET_RE = re.compile(r"[\(\[].*?[\)\]]")
_NOISE_RE = re.compile(r"[^a-z0-9]")


def song_key(artist: str | None, title: str | None) -> tuple[str, str]:
    """Identity of a RECORDING, ignoring edition and punctuation.

    "Al Green - Call Me (Come Back Home)" and "Al Green - Call Me" are the
    same song; treating them as different is what produced a phantom
    "500 songs lost" report.
    """
    t = _EDITION_RE.sub("", _BRACKET_RE.sub("", title or ""))
    return (_NOISE_RE.sub("", (artist or "").lower()), _NOISE_RE.sub("", t.lower()))


@dataclass
class Finding:
    level: str  # "ok" | "warn" | "fail"
    check: str
    detail: str
    count: int = 0


@dataclass
class Report:
    findings: list[Finding] = field(default_factory=list)

    def add(self, level: str, check: str, detail: str, count: int = 0) -> None:
        self.findings.append(Finding(level, check, detail, count))

    @property
    def failed(self) -> bool:
        return any(f.level == "fail" for f in self.findings)

    def render(self) -> str:
        icon = {"ok": "✓", "warn": "!", "fail": "✗"}
        lines = [f"  {icon[f.level]}  {f.check:<34} {f.detail}" for f in self.findings]
        worst = (
            "FAIL"
            if self.failed
            else ("WARN" if any(f.level == "warn" for f in self.findings) else "OK")
        )
        lines.append("")
        lines.append(f"  library integrity: {worst}")
        return "\n".join(lines)


def diagnose(cfg: MusicConfig) -> Report:
    """Run every integrity check. Read-only."""
    rep = Report()
    conn = sqlite3.connect(f"file:{cfg.db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row

    rows = conn.execute(
        "SELECT file_path, artist, title, status, audio_hash, finalized_at FROM archive"
    ).fetchall()
    lib = [r for r in rows if r["status"] == "CATALOGUED"]
    on_disk = {r["file_path"] for r in rows if Path(r["file_path"]).exists()}

    counts: dict[str, int] = {}
    for r in rows:
        counts[r["status"]] = counts.get(r["status"], 0) + 1
    rep.add(
        "ok",
        "rows by status",
        ", ".join(f"{k}={v}" for k, v in sorted(counts.items(), key=lambda kv: -kv[1])),
    )

    # 1. Rows whose file is gone -- split by whether the RECORDING survives.
    missing = [r for r in lib if r["file_path"] not in on_disk]
    if missing:
        held = {song_key(r["artist"], r["title"]) for r in rows if r["file_path"] in on_disk}
        lost = [r for r in missing if song_key(r["artist"], r["title"]) not in held]
        rep.add(
            "fail" if lost else "warn",
            "rows with a missing file",
            f"{len(missing)} rows; {len(missing) - len(lost)} whose recording survives elsewhere, "
            f"{len(lost)} genuinely gone",
            len(missing),
        )
    else:
        rep.add("ok", "rows with a missing file", "none")

    # 2. Files on disk that nothing in the database knows about.
    known = {r["file_path"] for r in rows}
    orphans = [
        p
        for p in cfg.alac_library.rglob("*.m4a")
        if "DUPES_MOVED_FOR_REVIEW" not in p.parts
        and "TRIBUTE_REMOVED_FOR_REVIEW" not in p.parts
        and "_history" not in p.parts
        and str(p) not in known
    ]
    rep.add(
        "warn" if orphans else "ok",
        "library files with no row",
        f"{len(orphans)}" + (f"  e.g. {orphans[0].name}" if orphans else ""),
        len(orphans),
    )

    # 3. Hash ledger agreement, both directions.
    if cfg.hash_index_path.exists():
        idx = sqlite3.connect(f"file:{cfg.hash_index_path}?mode=ro", uri=True)
        ledger = {h for (h,) in idx.execute("SELECT audio_hash FROM finalized_hashes")}
        dead = sum(
            1
            for (p,) in idx.execute("SELECT file_path FROM finalized_hashes")
            if not Path(p).exists()
        )
        idx.close()
        finalized = [r for r in rows if r["finalized_at"] and r["audio_hash"]]
        unindexed = [r for r in finalized if r["audio_hash"] not in ledger]
        rep.add(
            "fail" if unindexed else "ok",
            "finalized rows in hash ledger",
            f"{len(finalized) - len(unindexed)}/{len(finalized)}"
            + (f"  {len(unindexed)} MISSING" if unindexed else ""),
            len(unindexed),
        )
        # Reported, not warned about. finalized_hashes.file_path is
        # documented in db.py as the path "at time of finalize" -- an
        # immutable historical snapshot -- and audit.py states that a row
        # moved afterwards is *expected* not to match it. So a non-zero
        # count here is the normal state of a library where anything has
        # ever been renamed, moved or deliberately removed, and warning on
        # it permanently just teaches the reader to ignore the report.
        #
        # The section 4.17 cascade came from CrossDupeStage *acting* on an
        # unverified hit, and that is fixed where it belongs: cross_dupe.py
        # confirms the twin exists on disk before believing it, and reports
        # its own stale count. Nothing downstream is harmed by these
        # entries.
        #
        # Grey's ruling, 2026-08-24: keep entries for deliberately removed
        # content -- they are a true record of what was once held. Note
        # this does NOT stop that content being re-ingested; the ledger
        # cannot do that by design. A deny-list consulted by IngestStage
        # would be the mechanism for that, and does not exist.
        #
        # The number still earns its place as a drift indicator: a sudden
        # jump means files moved en masse, which is what 4.17 looked like
        # from the outside.
        rep.add(
            "ok",
            "ledger entries naming a gone file",
            f"{dead}  (finalize-time snapshots; expected)" if dead else "0",
            0,
        )
    else:
        rep.add("warn", "hash ledger", f"not found at {cfg.hash_index_path}")

    # 4. Genre coverage and the one-genre-per-artist rule.
    no_genre = conn.execute(
        "SELECT COUNT(*) FROM archive WHERE status='CATALOGUED' AND (genre IS NULL OR trim(genre)='')"
    ).fetchone()[0]
    multi = conn.execute(
        "SELECT COUNT(*) FROM (SELECT artist FROM archive WHERE status='CATALOGUED' "
        "AND genre IS NOT NULL AND trim(genre)!='' GROUP BY artist HAVING COUNT(DISTINCT genre)>1)"
    ).fetchone()[0]
    pct = 100 * (len(lib) - no_genre) / len(lib) if lib else 100.0
    rep.add(
        "warn" if no_genre else "ok",
        "genre coverage",
        f"{pct:.1f}%  ({no_genre} without)",
        no_genre,
    )
    rep.add("warn" if multi else "ok", "artists with >1 genre", str(multi), multi)

    # 5. Quarantine: is anything in there the only copy of its recording?
    quarantined = [r for r in rows if r["status"] in ("DUPE_REVIEW", "TRIBUTE_REVIEW")]
    if quarantined:
        held = {song_key(r["artist"], r["title"]) for r in lib if r["file_path"] in on_disk}
        solo = [r for r in quarantined if song_key(r["artist"], r["title"]) not in held]
        rep.add(
            "fail" if solo else "ok",
            "quarantine holds a sole copy",
            f"{len(solo)} of {len(quarantined)}"
            + ("  -- purging would lose these" if solo else ""),
            len(solo),
        )
    else:
        rep.add("ok", "quarantine", "empty")

    conn.close()
    return rep
