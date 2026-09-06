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

from .brackets import strip_bracketed
from .config import MusicConfig

# The marker is often preceded by a year or a qualifier: "Slow Ride - 2016
# Remaster", "Layla - Acoustic Live". Allow a short run of words before it
# rather than requiring the marker to follow the dash immediately.
_EDITION_RE = re.compile(
    r"\s*-\s*(?:\w+\s+){0,2}"
    r"(remaster|remastered|live|mono|stereo|single|album|radio|rerecorded|version|mix|edit)\b.*$",
    re.IGNORECASE,
)
_NOISE_RE = re.compile(r"[^a-z0-9]")


_AUDIO_SUFFIXES = frozenset({".m4a", ".flac", ".mp3", ".wav", ".aac", ".ogg"})


def song_key(artist: str | None, title: str | None) -> tuple[str, str]:
    """Identity of a RECORDING, ignoring edition and punctuation.

    "Al Green - Call Me (Come Back Home)" and "Al Green - Call Me" are the
    same song; treating them as different is what produced a phantom
    "500 songs lost" report.
    """
    t = _EDITION_RE.sub("", strip_bracketed(title or ""))
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
        "SELECT file_path, artist, title, status, audio_hash, finalized_at "
        "FROM archive"
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

    # 2b. The reciprocal of check 2, for the subtrees check 2 deliberately
    #     skips.
    #
    #     Check 2 scans alac_library and excludes the review folders, so it
    #     answers "is anything in the EDITION unaccounted for". Both review
    #     folders live under alac_archive, which that scan never visits at
    #     all -- so on 2026-09-06 it reported 0 while 484 untracked files,
    #     19 GB of them, sat in DUPES_MOVED_FOR_REVIEW under filenames like
    #     "Unknown Artist - Unknown Title (312).m4a". A row is what makes a
    #     file findable; without one nothing here, in the console, or in any
    #     stage would ever have named them again. The check above was true
    #     and useless at the same time, which is worse than a check that
    #     fails, because a ✓ is read as "nothing is unaccounted for".
    #
    #     These folders are meant to hold files that still HAVE a row and are
    #     awaiting Grey's ruling. A file here with no row is not awaiting
    #     anything -- it is lost -- so it is reported separately rather than
    #     folded into check 2, whose exclusions stay exactly as they were.
    #     getattr, not attribute access: diagnose() is called in tests with a
    #     stand-in config that carries only the keys a given test needs, the
    #     same reason _authority_disagreements reaches for meta_dir that way.
    review_dirs = [
        d
        for d in (
            getattr(cfg, "dupes_review_dir", None),
            getattr(cfg, "tribute_review_dir", None),
        )
        if d is not None and d.exists()
    ]
    stranded = [
        p
        for d in review_dirs
        for p in d.rglob("*")
        if p.is_file()
        and p.suffix.lower() in _AUDIO_SUFFIXES
        and str(p) not in known
    ]
    if stranded:
        size_gb = sum(p.stat().st_size for p in stranded) / 1024**3
        rep.add(
            "warn",
            "review folders with no row",
            f"{len(stranded)} file(s), {size_gb:.1f} GB, e.g. {stranded[0].name}",
            len(stranded),
        )
    elif review_dirs:
        rep.add("ok", "review folders with no row", "none")

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
        # ...and by audio_hash, which is the stable identity for a recording
        # and survives a rename. song_key alone cannot see a copy that is
        # safely held under a different artist name, and that is now the
        # normal case: ClassicalComposerStage renames the KEPT copy to the
        # composer while the quarantined duplicate keeps its performer
        # credit, so the pair can never match by name.
        #
        # Measured 2026-08-25: Carmignola's Four Seasons and Sarah Nemtanu's
        # BWV 1043 both reported as sole copies while their audio sat
        # catalogued under Vivaldi and Bach. Without this, every classical
        # dedup produces a false FAIL.
        held_audio = {r["audio_hash"] for r in lib if r["audio_hash"] and r["file_path"] in on_disk}
        solo = [
            r
            for r in quarantined
            if song_key(r["artist"], r["title"]) not in held
            and not (r["audio_hash"] and r["audio_hash"] in held_audio)
        ]
        rep.add(
            "fail" if solo else "ok",
            "quarantine holds a sole copy",
            f"{len(solo)} of {len(quarantined)}"
            + ("  -- purging would lose these" if solo else ""),
            len(solo),
        )
    else:
        rep.add("ok", "quarantine", "empty")

    # N. Audio that was REJECTED for truncation, present in the library
    #    anyway under another row.
    #
    #    Added 2026-09-04, from a real escape. Carlos Santana's "Bella"
    #    existed in INBOX twice: as a FLAC, which Canonicalize converted,
    #    duration-checked, and correctly REFUSED; and as an already-ALAC copy
    #    of the same truncated audio, which Canonicalize classified
    #    PASSTHROUGH -- "nothing to convert, no file write at all". A
    #    passthrough produces no output, and that duration check compares a
    #    conversion's OUTPUT against its source, so it never ran. The
    #    rejection worked perfectly; its already-converted twin walked past.
    #
    #    Nothing about the file betrays it. Header, stream and decode all
    #    agree at 143.78s and ffmpeg reports no error: it is a valid short
    #    file. Comparing durations across copies of the same recording was
    #    tried first and abandoned -- measured against this library it
    #    produced 802 findings, mostly live versions legitimately longer than
    #    their studio originals, which is the crying-wolf failure of SOP 4.27
    #    rather than a check.
    #
    #    The signal that IS exact: the rejected file has an audio_hash, and
    #    any other row carrying that same hash holds the same rejected audio.
    #    On the library this fired once, on the one file that escaped, and
    #    nowhere else.
    #    Collected as two sets, not a dict keyed on audio_hash. Two identical
    #    truncated files in INBOX -- the "existed twice" shape this check was
    #    written for -- are both refused and share one audio_hash. A dict keeps
    #    only the last path, and the other source is then reported as escaped
    #    audio: the same false positive already fixed once here (P1-2).
    _refusals = list(
        conn.execute(
            """
            SELECT a.audio_hash AS audio_hash, e.old_value AS src
              FROM events e
              JOIN archive a ON a.file_path = e.old_value
             WHERE e.event_type = 'CANONICALIZE_VERIFY_FAILED'
               AND a.audio_hash IS NOT NULL AND a.audio_hash <> ''
            """
        )
    )
    rejected_hashes = {r["audio_hash"] for r in _refusals}
    #    The rejected source itself still carries its own hash and is still
    #    on disk -- that is the refusal working, not a leak. Only OTHER rows
    #    holding that audio are the escape. EVERY refused source must be here,
    #    not one per hash.
    rejected_paths = {r["src"] for r in _refusals}
    escaped = [
        r
        for r in lib
        if r["audio_hash"] in rejected_hashes
        and r["file_path"] not in rejected_paths
        and r["file_path"] in on_disk
    ]
    if escaped:
        rep.add(
            "fail",
            "rejected audio present anyway",
            f"{len(escaped)} row(s) carry audio a conversion refused, "
            f"e.g. {Path(escaped[0]['file_path']).name}",
            len(escaped),
        )
    elif rejected_hashes:
        rep.add(
            "ok",
            "rejected audio present anyway",
            f"none ({len(rejected_hashes)} rejection(s) on record, all held)",
        )
    else:
        rep.add("ok", "rejected audio present anyway", "no rejections on record")

    # N+1. A knock-off that was removed, still present under another row.
    #
    #      Added 2026-09-04, from the Chuck Billy case. "Seek & Destroy" from
    #      *Metallic Assault: A Tribute to Metallica* was ruled out on
    #      2026-09-01 and deleted. Three more copies of the same performance
    #      were still on disk on 2026-09-04, one of them CATALOGUED in the
    #      newly built library, and they were found only because someone went
    #      looking.
    #
    #      tribute_quarantine could not have caught that copy: it matches
    #      \btribute\b and \bkaraoke\b against artist, title and album, and
    #      the album tag -- the only field naming the compilation -- had been
    #      overwritten with the playlist name "My playlist C" by whatever
    #      exported it. The four credited musicians are all real people. No
    #      field carried any evidence at all.
    #
    #      What survives that is the audio. A copy quarantined as a knock-off
    #      or deleted as one shares its audio_hash with every other copy of
    #      the same performance, whatever the tags say.
    #
    #      DELETED is included because a manual removal (Grey's ruling) does
    #      not set TRIBUTE_REVIEW -- keying on the automatic status alone
    #      would have missed the very case this was built for. Hashes from a
    #      refused conversion are excluded: the Santana row was DELETED for
    #      truncation, not for being a knock-off, and its twin is the refused
    #      source sitting exactly where it should.
    #      DELETED still counts -- Grey's manual rulings set it and nothing
    #      else -- but it is an untyped marker, and on 2026-09-05 dedup
    #      started using it too: 10,403 redundant copies were retired that
    #      way, each with a legitimate CATALOGUED twin by construction.
    #      Read as knock-offs they reported 8,224 survivors and turned
    #      integrity to FAIL: the crying-wolf failure this module warns of.
    #
    #      So a dedup purge now records DUPE_PURGED against the row, and
    #      that is what excludes it -- a reason-bearing signal, which is what
    #      P1-4 asked for. Deliberately NOT keyed on the knock-off events
    #      themselves: only 296 of 543 evented paths still resolve to a live
    #      row, because file_path drifts as files move (Chuck Billy's own
    #      TRIBUTE_QUARANTINED names a TRIBUTE_REMOVED_FOR_REVIEW path he
    #      left weeks ago). Absence of an event is not absence of intent.
    _dupe_purged = {
        r["file_path"]
        for r in conn.execute(
            "SELECT DISTINCT file_path FROM events WHERE event_type = 'DUPE_PURGED' "
            "AND file_path IS NOT NULL AND file_path <> ''"
        )
    }

    def _removed_as_knockoff(r) -> bool:
        if r["status"] == "TRIBUTE_REVIEW":
            return True
        return r["status"] == "DELETED" and r["file_path"] not in _dupe_purged

    removed_knockoff_hashes = {
        r["audio_hash"]
        for r in rows
        if _removed_as_knockoff(r)
        and r["audio_hash"]
        and r["audio_hash"] not in rejected_hashes
    }
    knockoff_paths = {r["file_path"] for r in rows if _removed_as_knockoff(r)}
    #      The knockoff_paths test is belt-and-braces, NOT the load-bearing
    #      guard its twin above is. `lib` is CATALOGUED only and a knock-off
    #      path is TRIBUTE_REVIEW or DELETED, and file_path is unique, so it
    #      is disjoint by construction and always True here. It reads like
    #      the rejected_paths check, where the guard IS required because a
    #      refused source stays CATALOGUED -- that asymmetry is real, and
    #      this note exists so the next reader does not assume otherwise.
    survivors = [
        r
        for r in lib
        if r["audio_hash"] in removed_knockoff_hashes
        and r["file_path"] not in knockoff_paths
        and r["file_path"] in on_disk
    ]
    #      NAMING. This was called "removed knock-off still held" until
    #      2026-09-06. It is broader than that name: a DELETED row with no
    #      DUPE_PURGED counts, and rows are deleted for reasons that are not
    #      knock-offs -- truncation, a title_pattern match, an owner ruling
    #      about something else. All three of the rows it reported on
    #      2026-09-06 were like that, and two of them were fine.
    #
    #      The breadth is NOT the bug and must not be narrowed: it is what
    #      caught the one real defect in that same batch -- a track whose
    #      permanent deletion Grey authorised on 2026-09-03, re-ingested a
    #      day later because the deletion never reached denied_hashes.
    #      Keying on a reason would have suppressed exactly that. So the
    #      name is widened to match the check, and each row now carries the
    #      reason its twin was removed, which turns a false alarm into a row
    #      that explains itself instead of one that has to be re-diagnosed.
    def _removal_reason(r) -> str:
        if r["status"] == "TRIBUTE_REVIEW":
            return "tribute quarantine"
        ev = conn.execute(
            "SELECT event_type, note FROM events WHERE file_path = ? "
            "AND event_type IN ('TRIBUTE_QUARANTINED','FILE_DELETED','DELETED') "
            "ORDER BY id DESC LIMIT 1",
            (r["file_path"],),
        ).fetchone()
        if ev is None:
            return "no removal event on record"
        note = (ev["note"] or "").strip()
        return f"{ev['event_type']}: {note[:60]}" if note else ev["event_type"]

    if survivors:
        by_hash = {r["audio_hash"]: r for r in rows if _removed_as_knockoff(r)}
        detail = "; ".join(
            f"{Path(r['file_path']).name[:38]} [{_removal_reason(by_hash[r['audio_hash']])}]"
            for r in survivors[:3]
        )
        rep.add(
            "fail",
            "removed audio still held",
            f"{len(survivors)} catalogued row(s) share audio with a removed copy: {detail}",
            len(survivors),
        )
    elif removed_knockoff_hashes:
        rep.add(
            "ok",
            "removed audio still held",
            f"none ({len(removed_knockoff_hashes)} removal(s) on record)",
        )
    else:
        rep.add("ok", "removed audio still held", "no removals on record")

    # N+2. The authorities must agree with each other.
    #
    #      MUSAEUS keeps several: MasterLaw.csv (artist -> genre),
    #      artist_canon.tsv (artist -> artist), Genre_Allowed.txt (the
    #      vocabulary) and denied_hashes. Nothing checked them against ONE
    #      ANOTHER, and on 2026-09-05 that cost three separate faults in a
    #      single evening -- each one a decision Grey had genuinely made,
    #      recorded in one authority, and never reaching another:
    #
    #        - the deny list was downgraded to advisory in the stage, but
    #          verify_effect still read every row and reported 12 false leaks
    #        - "Question Mark and the Mysterians" was settled in MasterLaw
    #          and absent from artist_canon, so the library carried both
    #          spellings, two tracks each
    #        - two canon entries pointed at names that are THEMSELVES canon
    #          keys, and resolve_exact does not follow a chain, so a row
    #          stopped half-way to its canonical name
    #
    #      A single authority can be correct and the system still wrong.
    _authority_disagreements(cfg, rep)

    conn.close()
    return rep


def _authority_disagreements(cfg: MusicConfig, rep: Report) -> None:
    """Cross-check the canon files against each other. Read-only."""
    # getattr, not attribute access: doctor runs against whatever config it is
    # handed, and a minimal one need not carry meta_dir. The first version of
    # this check assumed it and took 28 existing doctor tests down with it --
    # a cross-authority check that breaks the caller is worse than none.
    meta = getattr(cfg, "meta_dir", None)
    if meta is None:
        rep.add("ok", "authorities agree", "no meta_dir on this config")
        return
    meta = Path(meta)
    canon_path = meta / "artist_canon.tsv"
    law_path = meta / "MasterLaw.csv"
    allowed_path = meta / "Genre_Allowed.txt"

    if not canon_path.exists() and not law_path.exists():
        rep.add("ok", "authorities agree", "no canon files to cross-check")
        return

    problems: list[str] = []

    # A canon entry pointing at a name that is itself a canon key. resolve_exact
    # does not follow a chain, so such a row lands half-way and stops.
    if canon_path.exists():
        try:
            from .canon.artist import ArtistCanon

            canon = ArtistCanon(canon_path)
            chains = [
                f"{raw!r} -> {target!r} -> {canon.resolve_exact(target)!r}"
                for raw, target in canon._map.items()
                if canon.resolve_exact(target)
                and canon.resolve_exact(target) != target
            ]
            if chains:
                problems.append(
                    f"{len(chains)} artist_canon entry(ies) point at a name that is "
                    f"itself a canon key, so a row stops half-way: {chains[0]}"
                )
        except Exception as exc:  # a broken canon is its own report, not this one
            problems.append(f"artist_canon.tsv could not be read: {type(exc).__name__}")

    # A genre MasterLaw assigns that the vocabulary forbids: the law would write
    # a value the allow-list rejects, and both files claim to be authoritative.
    if law_path.exists() and allowed_path.exists():
        try:
            import csv as _csv

            allowed = {
                ln.strip()
                for ln in allowed_path.read_text(encoding="utf-8").splitlines()
                if ln.strip() and not ln.startswith("#")
            }
            assigned = {
                r[-1].strip()
                for r in _csv.reader(law_path.open(encoding="utf-8"))
                if len(r) >= 2 and r[0].strip().lower() != "artist" and r[-1].strip()
            }
            off = sorted(assigned - allowed)
            if off:
                problems.append(
                    f"{len(off)} genre(s) MasterLaw assigns are not in "
                    f"Genre_Allowed.txt: " + ", ".join(repr(g) for g in off[:4])
                )
        except Exception as exc:
            problems.append(f"MasterLaw.csv could not be read: {type(exc).__name__}")

    if problems:
        for detail in problems:
            rep.add("fail", "authorities agree", detail, len(problems))
    else:
        rep.add("ok", "authorities agree", "canon files are consistent with each other")
