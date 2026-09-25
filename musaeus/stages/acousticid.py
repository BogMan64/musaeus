#!/usr/bin/env python3
"""
MUSAEUS — Stage: AcousticID
Acoustic fingerprint-based duplicate detection using fpcalc + AcousticID API.

What it does:
  - Finds CATALOGUED rows that haven't been fingerprinted yet
  - Runs fpcalc to generate a Chromaprint fingerprint for each file
  - Queries AcousticID API to get the recording MBID for each fingerprint
  - Stores fingerprint + recording_id in archive
    (columns auto-added on first run via _ensure_columns)
  - Detects ACOUSTIC duplicates: same recording_id → different file_path
  - Stages duplicate pairs in the duplicates table with type='ACOUSTIC'
  - Logs ACOUSTIC_MATCHED / ACOUSTIC_DUPE_FOUND events
  - dry_run() reports matches without any DB changes

Why this matters beyond Sentinel:
  - Sentinel hashes the raw PCM stream — catches exact bitwise copies
  - AcousticID catches re-encodes: same song, FLAC vs MP3 vs different bitrate
  - These are the most common "same song twice" scenarios in real libraries

Requirements:
  - fpcalc binary (from chromaprint package) must be in PATH
  - ACOUSTICID_API_KEY in ~/.config/musaeus/settings.env
    (free key at acousticid.org — 3 req/s limit)

Graceful degradation:
  - fpcalc not found → stage skipped with warning
  - API key missing → fingerprints stored but recording IDs not looked up
  - API error → file skipped, run continues
  - Rate-limited → backs off 1s and retries once

ORPHEUS equivalent: SCRIPTS/stage_acoustic_fingerprint_duplicates.py
"""

from __future__ import annotations

import json
import logging
import re
import sqlite3
import subprocess
import time
import unicodedata
import urllib.error
import uuid
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import urlopen

from ..context import RunContext, StageResult, elision
from ..db import ensure_columns
from ..network_policy import check as _network_check
from .base import BaseStage, StageError

logger = logging.getLogger(__name__)

_ACOUSTICID_URL = "https://api.acoustid.org/v2/lookup"
# Two files more than this far apart in length are not the same
# recording. Generous enough for fade/gap differences between
# masterings, tight enough to reject a different song.
_DUPE_DURATION_TOLERANCE_S = 3.0

_RATE_LIMIT_S = 0.34  # 3 req/s limit for free tier
_TIMEOUT_S = 15
_FPCALC_TIMEOUT_S = 60  # fpcalc on a large FLAC can take ~10s
_COMMIT_EVERY = 50
_MIN_DURATION_S = 30  # skip clips shorter than 30s (too unreliable)


# ── Column migration ──────────────────────────────────────────────────────────


def _ensure_columns(conn) -> None:  # type: ignore[type-arg]
    """Columns this stage owns. Mechanism shared via db.ensure_columns;
    the list stays here, next to the code that reads them."""
    ensure_columns(
        conn,
        (
            ("chromaprint", "TEXT"),
            ("chromaprint_duration", "REAL"),
            ("acousticid_recording", "TEXT"),
            ("acousticid_score", "REAL"),
            ("acousticid_checked_at", "TEXT"),
        ),
    )


def _fpcalc(path: str) -> tuple[float, str]:
    """
    Run fpcalc on an audio file.
    Returns (duration_seconds, fingerprint_string).
    Raises RuntimeError or ValueError on failure.
    """
    import shutil

    fpcalc = shutil.which("fpcalc")
    if not fpcalc:
        raise RuntimeError("fpcalc not found in PATH")

    try:
        res = subprocess.run(
            [fpcalc, "-json", path],
            capture_output=True,
            text=True,
            timeout=_FPCALC_TIMEOUT_S,
        )
    except subprocess.TimeoutExpired as exc:
        raise ValueError(f"fpcalc timed out after {_FPCALC_TIMEOUT_S}s") from exc

    # The verdict is the OUTPUT, not the exit code.
    #
    # fpcalc exits non-zero for any input shorter than its ~120s read
    # window -- "ERROR: Error decoding audio frame (End of file)" on
    # stderr -- while still writing a complete, usable fingerprint to
    # stdout. Measured against real library audio (fpcalc 1.5.1), the same
    # track trimmed to 45s/60s/90s/119s returns rc=3 with valid
    # fingerprints of 1168/1595/2447/3258 chars, and returns 0 at 125s and
    # above. Treating rc as the answer therefore discarded a good
    # fingerprint for every track between _MIN_DURATION_S and ~120s: 59 of
    # 2,028 rows (2.9%) in the 2026-08-26 batch, and far more of any
    # spoken-word or classical set.
    #
    # A real failure is distinguishable and still raises: it produces no
    # parseable JSON at all (rc=2, "Could not open the input file").
    try:
        data = json.loads(res.stdout)
    except ValueError as exc:  # includes json.JSONDecodeError
        raise ValueError(
            f"fpcalc produced no parseable output (rc={res.returncode}): {res.stderr[:200]}"
        ) from exc

    duration = float(data.get("duration", 0))
    fingerprint = data.get("fingerprint", "")
    if not fingerprint:
        raise ValueError(
            f"fpcalc returned empty fingerprint (rc={res.returncode}): {res.stderr[:200]}"
        )
    if res.returncode != 0:
        logger.debug(
            "[acousticid] fpcalc rc=%s but produced a usable fingerprint for %s: %s",
            res.returncode,
            path,
            res.stderr.strip()[:120],
        )
    return duration, fingerprint


# ── AcousticID API ────────────────────────────────────────────────────────────


class LookupUnavailable(Exception):
    """AcousticID gave no answer: timeout, DNS, 5xx, an unparseable body,
    or a policy refusal.

    Distinct from "AcousticID answered, and has no confident match". Both
    used to arrive at the caller identically -- the transport errors as a
    swallowed exception, the genuine miss as None -- and the caller then
    wrote acousticid_checked_at either way. Selection was on
    `chromaprint IS NULL`, written in that same statement, so a single
    network wobble removed the track from the queue permanently. Not
    "marked checked": structurally unreachable, past any force flag.

    Same three states mb_enrich needed (see f84e643), for the same reason:

        (recording_id, score)  found
        None                   asked, definitively no match  -> stamp
        LookupUnavailable      never asked successfully       -> leave alone
    """


def _row_get(row: object, key: str) -> str:
    """A column that may not exist on an older schema. Absent reads as empty.

    sqlite3.Row raises IndexError for an unknown key rather than returning
    None, and the fallback SELECT in _run does not always carry these.
    """
    try:
        return str(row[key] or "")  # type: ignore[index]
    except (IndexError, KeyError, TypeError):
        return ""


def _norm_for_match(value: str) -> str:
    """Letters and digits only -- punctuation and case are not identity."""
    return re.sub(r"[^a-z0-9]", "", unicodedata.normalize("NFKD", value or "").lower())


def _pick_matching_recording(
    recordings: list[dict], want_artist: str, want_title: str
) -> str | None:
    """The recording that actually agrees with this file, or None.

    Returns None rather than guessing. A fingerprint match with no agreeing
    recording means "AcoustID knows this audio but cannot tell us which of
    several recordings it is" -- which is not the same as an answer, and
    must not be recorded as one.
    """
    wa, wt = _norm_for_match(want_artist), _norm_for_match(want_title)
    if not wt:
        return None  # nothing to check against; refusing beats guessing

    best: str | None = None
    for rec in recordings:
        rid = rec.get("id")
        if not rid:
            continue
        rid = str(rid)
        rt = _norm_for_match(rec.get("title", ""))
        if not rt or (rt not in wt and wt not in rt):
            continue
        artists = [_norm_for_match(a.get("name", "")) for a in rec.get("artists", [])]
        if wa and artists and not any(a and (a in wa or wa in a) for a in artists):
            continue  # title agrees, artist does not -- a cover
        if wa and artists:
            return rid  # both agree: done
        best = best or rid  # title agrees, no artist to check
    return best


def _acousticid_query(fingerprint: str, duration: float, api_key: str) -> list[dict]:
    """The raw AcoustID results for one fingerprint.

    Raises LookupUnavailable when no answer was obtained at all -- a timeout,
    a refusal by the network gateway, HTTP errors, or a reply that is not a
    result. An empty list is an answer: AcoustID knows nothing of this audio.
    The one place the service is asked, for matching (_acousticid_lookup) and
    naming (name_from_results) alike.
    """
    params = {
        "client": api_key,
        "fingerprint": fingerprint,
        "duration": str(int(duration)),
        "meta": "recordings",
        "format": "json",
    }
    url = f"{_ACOUSTICID_URL}?{urlencode(params)}"

    for attempt in range(2):
        try:
            # Ask the gateway before dispatching. Under LOCAL_ONLY this raises,
            # and the attempt is recorded BEFORE raising so the broad except
            # below cannot erase the evidence -- see network_policy.py.
            _network_check(_ACOUSTICID_URL)
            with urlopen(url, timeout=_TIMEOUT_S) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            break
        except urllib.error.HTTPError as exc:
            if exc.code == 429 and attempt == 0:
                logger.warning("[acousticid] rate-limited, backing off 1s")
                time.sleep(1.0)
                continue
            raise LookupUnavailable(f"HTTP {exc.code}") from exc
        except Exception as exc:
            # Timeout, DNS, connection reset, an unparseable body, or a
            # policy refusal. None of these are answers.
            raise LookupUnavailable(str(exc)) from exc
    else:  # pragma: no cover - both attempts exhausted without break
        raise LookupUnavailable("no response after retry")

    if data.get("status") != "ok":
        # The service replied but not with a result. "error" is not "no
        # such recording", so it must not settle the row.
        raise LookupUnavailable(f"status={data.get('status')!r}")
    return list(data.get("results", []))


#: Naming needs more than confirming. 0.80 is enough to agree that a file
#: already named "X - Y" is recording X - Y; taking a name from nothing but
#: the sound needs a stronger match.
NAMING_MIN_SCORE = 0.90


def _credit(artists: list[dict]) -> str:
    """An artist credit from AcoustID's artist list, with its join phrases."""
    parts = []
    for i, a in enumerate(artists):
        name = (a.get("name") or "").strip()
        if not name:
            continue
        parts.append(name)
        if i < len(artists) - 1:
            parts.append(a.get("joinphrase") or " & ")
    return "".join(parts).strip()


def name_from_results(
    results: list[dict], title_hint: str, min_score: float = NAMING_MIN_SCORE
) -> tuple[str, str, float] | None:
    """(artist, title, score) for an untagged file, or None -- refusing beats guessing.

    Each AcoustID result (one fingerprint cluster) is judged on its own,
    strongest first. A name is taken from the first result in which a
    recording's title agrees with what the file name still says (title_hint)
    AND every agreeing recording names the same artist. A cluster's order
    means nothing and a cluster is often polluted (see _acousticid_lookup), so
    neither the first recording nor the majority is trusted: a result whose
    agreeing recordings disagree about the artist -- covers, a member's solo
    re-recording -- is passed over, never settled.

    Per result, not pooled: measured against the live service 2026-09-25, the
    pooled rule refused "T.N.T" (AC/DC alone at 0.973; a second result mixed
    in two cover bands) and "Come And Get It" (Badfinger alone at 0.924).
    """
    hint = _norm_for_match(title_hint)
    if not hint:
        return None
    ranked = sorted(results, key=lambda r: float(r.get("score", 0)), reverse=True)
    for r in ranked:
        score = float(r.get("score", 0))
        if score < min_score:
            break
        agreeing: list[tuple[str, str]] = []
        for rec in r.get("recordings", []) or []:
            title = (rec.get("title") or "").strip()
            rt = _norm_for_match(title)
            if not rt or (rt not in hint and hint not in rt):
                continue
            artist = _credit(rec.get("artists") or [])
            if artist:
                agreeing.append((artist, title))
        if not agreeing or len({_norm_for_match(a) for a, _t in agreeing}) != 1:
            continue
        # The recording whose title IS the hint, if there is one; otherwise what
        # the file name said -- not a longer variant ("Yesterday - Remastered").
        exact = [x for x in agreeing if _norm_for_match(x[1]) == hint]
        artist, title = (exact or agreeing)[0]
        return artist, (title if exact else title_hint), score
    return None


def _acousticid_lookup(
    fingerprint: str,
    duration: float,
    api_key: str,
    want_artist: str = "",
    want_title: str = "",
) -> tuple[str, float] | None:
    """
    Query AcousticID for a recording match.

    Returns (recording_id, score), or None when AcousticID answered and had
    no match scoring >= 0.80. Raises LookupUnavailable when no answer was
    obtained at all -- never conflate the two.
    """
    results = _acousticid_query(fingerprint, duration, api_key)
    for r in results:
        score = float(r.get("score", 0))
        if score < 0.80:
            continue
        recordings = r.get("recordings", [])
        if not recordings:
            continue

        # NEVER recordings[0].
        #
        # An AcoustID result carries every MusicBrainz recording associated
        # with that fingerprint cluster, and the order is not meaningful. The
        # cluster 172884e7 ("Metro Station - Now That We're Done") is listed
        # FIRST in a great many of them, so taking [0] tagged 14 unrelated
        # tracks -- ABC, Lenny Kravitz, Bruno Mars, Mariah Carey -- as the
        # same recording. Verified against the live API 2026-08-31: for both
        # "ABC - Poison Arrow" and "98 Degrees - Give Me Just One Night" the
        # CORRECT recording was present in the list, at positions 2 and 3,
        # behind that same polluted entry.
        #
        # A score says the audio matched the cluster. It does not say which
        # recording in the cluster this file is. That is the same distinction
        # mb_enrich's _same_artist exists for -- "Red" scoring 100 against
        # "Red Hot Chili Peppers".
        #
        # So the caller supplies what it already knows about the file, and
        # the recording has to agree with it.
        chosen = _pick_matching_recording(recordings, want_artist, want_title)
        if chosen:
            return chosen, score
        logger.debug(
            "[acousticid] %d recording(s) scored %.2f but none matched %r / %r",
            len(recordings),
            score,
            want_artist,
            want_title,
        )

    return None


# ── Stage ─────────────────────────────────────────────────────────────────────


def _ledger_path(ctx) -> Path:
    return Path(ctx.config.vault_root) / "_db_backups" / "hash_index.db"


def _restore_from_ledger(ctx) -> int:
    """Copy known fingerprints back into `archive`, and report how many.

    This is what makes a rebuilt catalogue cheap instead of another 21
    hours. Matching is on audio_hash, so it survives every rename and
    re-filing the library has been through.

    Best-effort by design: no ledger, an unreadable one, or a catalogue
    without audio_hash all mean "nothing to restore", never a failed stage.
    A cache that cannot be read is a missing optimisation, not an error.
    """
    from ..db import ensure_fingerprints

    led_path = _ledger_path(ctx)
    if not led_path.is_file():
        return 0
    try:
        led = sqlite3.connect(led_path)
        ensure_fingerprints(led)
        known = {
            r[0]: r
            for r in led.execute(
                "SELECT audio_hash, chromaprint, chromaprint_duration, "
                "acousticid_recording, acousticid_score, checked_at FROM fingerprints"
            )
        }
        led.close()
    except sqlite3.Error:
        return 0
    if not known:
        return 0

    try:
        rows = ctx.conn.execute(
            "SELECT file_path, audio_hash FROM archive WHERE status='CATALOGUED' "
            "AND acousticid_checked_at IS NULL AND COALESCE(audio_hash,'') <> ''"
        ).fetchall()
    except sqlite3.Error:
        return 0

    n = 0
    for fp, h in rows:
        hit = known.get(h)
        if hit is None:
            continue
        _, cp, cpd, rec, score, checked = hit
        ctx.conn.execute(
            "UPDATE archive SET chromaprint=COALESCE(?, chromaprint), "
            "chromaprint_duration=COALESCE(?, chromaprint_duration) WHERE file_path=?",
            (cp, cpd, fp),
        )
        if checked:
            ctx.conn.execute(
                "UPDATE archive SET acousticid_recording=?, acousticid_score=?, "
                "acousticid_checked_at=? WHERE file_path=?",
                (rec, score, checked, fp),
            )
        n += 1
    ctx.conn.commit()
    return n


def _remember_in_ledger(
    ctx, file_path: str, chromaprint, duration, recording, score, checked_at
) -> None:
    """Write one fingerprint to the ledger. Never fails the stage."""
    from ..db import ensure_fingerprints, remember_fingerprint

    try:
        row = ctx.conn.execute(
            "SELECT audio_hash FROM archive WHERE file_path=?", (file_path,)
        ).fetchone()
        if not row or not row[0]:
            return
        led = sqlite3.connect(_ledger_path(ctx))
        ensure_fingerprints(led)
        remember_fingerprint(led, row[0], chromaprint, duration, recording, score, checked_at)
        led.commit()
        led.close()
    except sqlite3.Error:
        return


class AcousticIDStage(BaseStage):
    """
    AcousticID — fingerprint-based duplicate detection.
    Catches re-encodes that Sentinel's PCM hash misses.
    """

    NAME = "acousticid"

    def validate(self, ctx: RunContext) -> None:
        import shutil

        if not shutil.which("fpcalc"):
            raise StageError(
                "fpcalc not found in PATH. "
                "Install chromaprint: sudo apt install libchromaprint-tools"
            )
        api_key = ctx.config.acousticid_api_key
        if not api_key:
            logger.warning(
                "[acousticid] ACOUSTICID_API_KEY not set — fingerprints will be "
                "computed but not matched against AcousticID. "
                "Add key to ~/.config/musaeus/settings.env"
            )

        try:
            count = ctx.conn.execute(
                "SELECT COUNT(*) FROM archive WHERE status='CATALOGUED' AND chromaprint IS NULL"
            ).fetchone()[0]
        except Exception:
            count = ctx.conn.execute(
                "SELECT COUNT(*) FROM archive WHERE status='CATALOGUED'"
            ).fetchone()[0]
        logger.info("[acousticid] %d file(s) need fingerprinting", count)

    def _run(self, ctx: RunContext, dry_run: bool) -> StageResult:
        result = self._make_result(dry_run=dry_run)

        if not dry_run:
            _ensure_columns(ctx.conn)
            restored = _restore_from_ledger(ctx)
            if restored:
                logger.info(
                    "[acousticid] %d row(s) restored from the fingerprint ledger "
                    "-- not re-fingerprinted",
                    restored,
                )

        api_key = ctx.config.acousticid_api_key

        # Selection is on acousticid_checked_at, NOT on chromaprint.
        #
        # chromaprint used to be the gate, and it is written from the local
        # fpcalc result whether or not the lookup succeeded -- so one
        # timeout retired the row for ever. The two facts have different
        # lifetimes and now have different columns: the fingerprint is a
        # property of the audio, computed locally and cached indefinitely;
        # acousticid_checked_at means "AcousticID gave an answer about
        # this", and only an answer writes it.
        #
        # This also makes the column load-bearing. It was written by this
        # stage and read by nothing, which is a lie waiting for someone to
        # trust it.
        # A whole-library pass holds the write lock for hours, which is why
        # this stage is deliberately outside DEFAULT_PIPELINE. A limit makes
        # the backlog drainable in sittings: selection is already
        # "acousticid_checked_at IS NULL", so consecutive runs resume rather
        # than repeat, and an interrupted chunk costs only that chunk.
        limit = int(ctx.get("acousticid_limit", 0)) or -1  # -1 = SQLite "no limit"

        try:
            rows = ctx.conn.execute(
                """
                SELECT file_path, duration, chromaprint, chromaprint_duration,
                       artist, title
                  FROM archive
                 WHERE status = 'CATALOGUED'
                   AND acousticid_checked_at IS NULL
                 ORDER BY file_path
                 LIMIT ?
                """,
                (limit,),
            ).fetchall()
        except Exception:
            rows = ctx.conn.execute(
                """
                SELECT file_path, duration, artist, title FROM archive
                WHERE status = 'CATALOGUED'
                ORDER BY file_path
                LIMIT ?
                """,
                (limit,),
            ).fetchall()

        # Which columns the SELECT actually returned. The fallback query
        # above omits the fingerprint columns, so this cannot be assumed.
        # sqlite3.Row needs .keys(); `in row` would test values.
        _cols = set(rows[0].keys()) if rows else set()

        # recording_id → [file_path] for dupe detection this run
        recording_map: dict[str, list[str]] = {}
        matched = 0
        no_match = 0
        unavailable = 0
        reused = 0
        dupes_found = 0
        pairs: list[tuple[str, str]] = []

        from datetime import datetime, timezone

        now = datetime.now(tz=timezone.utc).isoformat(timespec="seconds")

        for row in rows:
            result.files_processed += 1
            fp = row["file_path"]
            dur_db = row["duration"] or 0

            # Skip short clips
            if dur_db and float(dur_db) < _MIN_DURATION_S:
                result.files_skipped += 1
                continue

            # Reuse a fingerprint this row already carries. Now that a
            # no-answer leaves the row selectable, the retry must not pay
            # fpcalc again over the whole library on every run.
            stored = row["chromaprint"] if "chromaprint" in _cols else None
            stored_dur = row["chromaprint_duration"] if "chromaprint_duration" in _cols else None
            if stored and stored_dur:
                fingerprint, duration = stored, float(stored_dur)
                reused += 1
            else:
                try:
                    duration, fingerprint = _fpcalc(fp)
                except Exception as exc:
                    logger.warning("[acousticid] fpcalc error %s: %s", fp, exc)
                    result.files_skipped += 1
                    result.errors.append(f"{fp}: {exc}")
                    continue

            if duration < _MIN_DURATION_S:
                result.files_skipped += 1
                continue

            # Look up AcousticID
            recording_id: str | None = None
            score: float = 0.0
            answered = False
            if api_key:
                time.sleep(_RATE_LIMIT_S)
                try:
                    match = _acousticid_lookup(
                        fingerprint,
                        duration,
                        api_key,
                        want_artist=_row_get(row, "artist"),
                        want_title=_row_get(row, "title"),
                    )
                except LookupUnavailable as exc:
                    # No answer, so nothing is known and nothing may be
                    # settled. The fingerprint below is still stored -- it
                    # is a local fact -- but acousticid_checked_at stays
                    # NULL and the row is asked again next run.
                    unavailable += 1
                    logger.warning(
                        "[acousticid] no answer for %s (%s) — not marked, will retry",
                        fp,
                        exc,
                    )
                else:
                    answered = True
                    if match:
                        recording_id, score = match
                        matched += 1
                        logger.debug(
                            "[acousticid] %s → recording=%s score=%.2f",
                            fp,
                            recording_id,
                            score,
                        )
                    else:
                        no_match += 1

            # Store results. Two statements, because the two facts have
            # different truth conditions: the fingerprint was computed
            # locally and is true regardless of what the network did, while
            # the marker asserts that AcousticID answered.
            if not dry_run:
                ctx.conn.execute(
                    """
                    UPDATE archive
                       SET chromaprint=?, chromaprint_duration=?
                     WHERE file_path=?
                    """,
                    (fingerprint, duration, fp),
                )
                if answered:
                    ctx.conn.execute(
                        """
                        UPDATE archive
                           SET acousticid_recording=?,
                               acousticid_score=?,
                               acousticid_checked_at=?
                         WHERE file_path=?
                        """,
                        (recording_id, score if score else None, now, fp),
                    )
                # ...and to the LEDGER, which is the copy that survives.
                # Everything above writes to `archive`, and `archive` gets
                # rebuilt: the 2026-08-30 pass spent 21 hours fingerprinting
                # 7,545 files and, measured on 2026-09-16, not one of those
                # results was still in the database. Keyed on audio_hash
                # because that is what a fingerprint is a property of -- it
                # survives a rebuild, a re-file, and a path move.
                _remember_in_ledger(
                    ctx,
                    fp,
                    fingerprint,
                    duration,
                    recording_id if answered else None,
                    score if answered and score else None,
                    now if answered else None,
                )
                if recording_id:
                    ctx.log_event(
                        "ACOUSTIC_MATCHED",
                        file_path=fp,
                        new_value=recording_id,
                        stage=self.NAME,
                        note=f"score={score:.2f}",
                    )

            # Dupe detection within this run
            if recording_id:
                if recording_id not in recording_map:
                    # Also check DB for existing matches
                    try:
                        existing = ctx.conn.execute(
                            "SELECT file_path FROM archive "
                            "WHERE acousticid_recording=? AND file_path!=?",
                            (recording_id, fp),
                        ).fetchall()
                        recording_map[recording_id] = [r["file_path"] for r in existing]
                    except Exception:
                        recording_map[recording_id] = []

                if recording_map[recording_id]:
                    for other_fp in recording_map[recording_id]:
                        # Two recordings of different LENGTH are not the same
                        # recording, whatever the fingerprint service says.
                        # A cheap second opinion: the 217 files wrongly moved
                        # on 2026-08-31 included pairs 212s vs 207s and 218s
                        # vs 198s, every one of which this rejects.
                        other = ctx.conn.execute(
                            "SELECT duration, artist FROM archive WHERE file_path=?", (other_fp,)
                        ).fetchone()
                        other_dur = (other["duration"] if other else None) or 0.0

                        # ORPHEUS's own net, adopted: classify_acousticid_groups.py
                        # flags a group as CROSS_ARTIST_COVER when its members
                        # disagree on artist. Duration alone cannot catch a cover
                        # -- same song, same length, different act -- and a cover
                        # is a different recording.
                        other_artist = _row_get(other, "artist") if other else ""
                        mine = _norm_for_match(_row_get(row, "artist"))
                        theirs = _norm_for_match(other_artist)
                        if (
                            mine
                            and theirs
                            and mine != theirs
                            and mine not in theirs
                            and theirs not in mine
                        ):
                            logger.info(
                                "[acousticid] REJECTED pair, cross-artist (%s vs %s): %s == %s",
                                _row_get(row, "artist"),
                                other_artist,
                                fp,
                                other_fp,
                            )
                            continue

                        if other_dur and abs(other_dur - duration) > _DUPE_DURATION_TOLERANCE_S:
                            logger.info(
                                "[acousticid] REJECTED pair, %.0fs vs %.0fs: %s == %s",
                                duration,
                                other_dur,
                                fp,
                                other_fp,
                            )
                            continue
                        dupes_found += 1
                        pairs.append((Path(other_fp).name, Path(fp).name))
                        group_id = f"acoustic_{uuid.uuid4().hex[:8]}"
                        logger.info(
                            "[acousticid] DUPE  %s  ==  %s  (recording=%s)",
                            fp,
                            other_fp,
                            recording_id,
                        )
                        if not dry_run:
                            for member in (fp, other_fp):
                                # Column names verified against the real
                                # schema, not against this statement's own
                                # history. It previously named `type` and
                                # `created_at`; the table has
                                # `duplicate_type` and `staged_at`, so every
                                # execution raised "table duplicates has no
                                # column named type" -- which, together with
                                # the five archive columns that did not
                                # exist at all (see db.py's _MIGRATIONS), is
                                # why this stage has never staged a row.
                                # run_id is recorded too: the column has
                                # always existed and was never populated,
                                # leaving staged pairs unattributable to the
                                # run that found them.
                                ctx.conn.execute(
                                    """
                                    INSERT OR IGNORE INTO duplicates
                                        (group_id, file_path, duplicate_type,
                                         confidence, status, run_id, staged_at, audio_hash)
                                    VALUES (?, ?, 'ACOUSTIC', ?, 'review', ?, ?,
                                            (SELECT audio_hash FROM archive WHERE file_path = ?))
                                    """,
                                    (group_id, member, score, ctx.run_id, now, member),
                                )
                            ctx.log_event(
                                "ACOUSTIC_DUPE_FOUND",
                                file_path=fp,
                                new_value=other_fp,
                                stage=self.NAME,
                                note=f"recording={recording_id}",
                            )
                        result.files_changed += 1

                recording_map[recording_id].append(fp)

            if result.files_processed % _COMMIT_EVERY == 0 and not dry_run:
                ctx.conn.commit()
                logger.info("[acousticid] checkpoint %d", result.files_processed)

        prefix = "Would fingerprint" if dry_run else "Fingerprinted"
        checked = result.files_processed - result.files_skipped
        result.notes.append(
            f"{prefix} {checked} file(s): {matched} AcousticID match(es), "
            f"{dupes_found} acoustic dupe(s) found."
        )
        if reused:
            result.notes.append(f"  {reused} fingerprint(s) reused from a previous run.")
        if no_match:
            result.notes.append(f"  {no_match} file(s) answered with no confident match.")
        if unavailable:
            # Deferred, not decided. Said plainly because the old code
            # reported errors=0 while retiring these rows for ever.
            result.notes.append(
                f"  {unavailable} file(s) got NO ANSWER (network/timeout/5xx) — "
                f"not marked, will be retried on the next run."
            )
        if not api_key:
            result.notes.append(
                "ACOUSTICID_API_KEY not set — recording IDs not looked up. "
                "Fingerprints stored for future use; rows left unmarked so a "
                "later run with a key still asks about them."
            )
        if dupes_found:
            # Status 'review', not 'pending': DupeResolver acts on every pending
            # group, and these are Grey's to decide (2026-09-25, choice (b)). A
            # radio edit or an extended mix can be the same recording to
            # AcoustID and still a different track to keep. So they are listed
            # here, by name, and nothing moves them.
            result.notes.append(
                f"{dupes_found} same-recording pair(s) for you to decide -- nothing moved:"
            )
            for a, b in pairs[:10]:
                result.notes.append(f"    {a}  ==  {b}")
            if len(pairs) > 10:
                result.notes.append(
                    "    " + elision(len(pairs) - 10, suffix="(duplicates table, status 'review')")
                )

        ctx.record_stage(result)
        return result

    def verify_effect(self, ctx: RunContext, result: StageResult) -> list[str]:
        """Re-derive a sample of what was stored, from the audio itself.

        This stage's claim is that the stored fingerprint is that file's
        fingerprint, and that a marked row was genuinely answered. Checking
        the database against itself would prove neither -- the same reason
        identity_tag re-reads tags off disk instead of trusting what it
        believes it wrote. The stage previously claimed nothing at all
        while writing four columns.
        """
        problems: list[str] = []
        if result.dry_run or not result.files_processed:
            return problems

        try:
            # The marker asserts an answer ABOUT a fingerprint, so a marked
            # row carrying none is incoherent by construction.
            orphaned = ctx.conn.execute(
                "SELECT COUNT(*) FROM archive "
                "WHERE acousticid_checked_at IS NOT NULL AND chromaprint IS NULL"
            ).fetchone()[0]
            rows = ctx.conn.execute(
                "SELECT file_path, chromaprint FROM archive "
                "WHERE chromaprint IS NOT NULL ORDER BY file_path LIMIT 2"
            ).fetchall()
        except Exception:  # columns absent -- nothing was claimed
            return problems

        if orphaned:
            problems.append(f"{orphaned} row(s) marked as checked carry no fingerprint")

        for row in rows:
            path = row["file_path"]
            if not Path(path).exists():
                continue
            try:
                _, fresh = _fpcalc(path)
            except Exception:
                continue  # fpcalc being unavailable is not evidence of a bad store
            if fresh != row["chromaprint"]:
                problems.append(f"stored fingerprint does not match the audio: {path}")
        return problems

    def dry_run(self, ctx: RunContext) -> StageResult:
        return self._run(ctx, dry_run=True)

    def run(self, ctx: RunContext) -> StageResult:
        return self._run(ctx, dry_run=False)
