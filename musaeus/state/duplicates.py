"""
MUSAEUS — the typed duplicate repository (P0-14)

The audit finding this repairs is worse than "the insertion path does not
match the schema". Verified against a real `db.open_db()` database:

  * `stages/acousticid.py` inserts into `duplicates` naming columns
    `type` and `created_at`. The table has `duplicate_type` and
    `staged_at`. The statement raises
    `table duplicates has no column named type`.
  * The same stage first UPDATEs `archive` setting `chromaprint`,
    `chromaprint_duration`, `acousticid_recording`, `acousticid_score`
    and `acousticid_checked_at` -- none of which exist in the schema at
    all, not in `_SCHEMA` and not in `_MIGRATIONS`. It fails on the first
    file, before ever reaching the duplicates insert.

So AcoustID fingerprinting is not merely "never run" (MUSAEUS_TODO §8);
as written it cannot run. Two independent contract breaks, neither
detectable without executing it against a real schema.

The repair is a schema-owned repository rather than SQL assembled inside
a stage. `insert_acoustid_candidate` names every non-generated column
explicitly -- no `SELECT *`, no implicit positional insert, no dict
splat. That is deliberate: an insert that names its columns fails loudly
against the wrong schema, which is how both of the above would have been
caught on the first run instead of never.

P0 creates `pending` candidates only. No decision, merge, quarantine or
deletion, and transport stays denied by the gateway.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass, field
from typing import Any

from musaeus.state.schema import StateError, utc_now_iso

DETECTOR_ACOUSTID = "acoustid"

DECISION_PENDING = "pending"
DECISION_STATUSES: frozenset[str] = frozenset(
    {DECISION_PENDING, "confirmed", "rejected", "superseded"}
)

# The exact insertion contract from DR-07, in order. Named as data so the
# repository and its tests refer to one list rather than two that agree
# by inspection.
INSERTION_COLUMNS: tuple[str, ...] = (
    "run_id",
    "candidate_item_id",
    "matched_item_id",
    "detector",
    "provider_recording_id",
    "fingerprint_digest",
    "score",
    "evidence_json",
    "decision_status",
    "created_at",
)

DUPLICATES_STATEMENTS: tuple[str, ...] = (
    """
    CREATE TABLE IF NOT EXISTS duplicates (
        id                    INTEGER PRIMARY KEY AUTOINCREMENT,
        run_id                TEXT NOT NULL,
        candidate_item_id     TEXT NOT NULL,
        matched_item_id       TEXT NOT NULL,
        detector              TEXT NOT NULL,
        provider_recording_id TEXT,
        fingerprint_digest    TEXT,
        score                 REAL,
        evidence_json         TEXT NOT NULL,
        decision_status       TEXT NOT NULL DEFAULT 'pending',
        created_at            TEXT NOT NULL,
        -- Derived from the evidence, stored so the uniqueness constraint
        -- can be enforced by the database rather than by every caller
        -- remembering to check first.
        evidence_identity     TEXT NOT NULL,
        UNIQUE (candidate_item_id, matched_item_id, detector, evidence_identity)
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_duplicates_run ON duplicates(run_id)",
    "CREATE INDEX IF NOT EXISTS idx_duplicates_status ON duplicates(decision_status)",
)


class DuplicateContractError(StateError):
    """A duplicate record does not satisfy the declared contract."""

    reason_code = "duplicate_contract_invalid"


def canonical_pair(a: str, b: str) -> tuple[str, str]:
    """
    Order a candidate pair deterministically.

    (X, Y) and (Y, X) are the same finding. Without a canonical order the
    uniqueness constraint would let a resumed run insert the mirror image
    of a row it already has, and the duplicate count would grow every time
    the run was resumed -- which is precisely the shape of a
    false-positive cascade.
    """
    if not a or not b:
        raise DuplicateContractError("both item references are required", candidate=a, matched=b)
    if a == b:
        raise DuplicateContractError("an item cannot be a duplicate of itself", item=a)
    return (a, b) if a <= b else (b, a)


@dataclass(frozen=True)
class DuplicateCandidate:
    """A typed record matching the insertion contract exactly."""

    run_id: str
    candidate_item_id: str
    matched_item_id: str
    detector: str
    evidence: dict[str, Any] = field(default_factory=dict)
    provider_recording_id: str | None = None
    fingerprint_digest: str | None = None
    score: float | None = None
    decision_status: str = DECISION_PENDING
    created_at: str | None = None

    def validated(self) -> DuplicateCandidate:
        from dataclasses import replace

        candidate, matched = canonical_pair(self.candidate_item_id, self.matched_item_id)
        if not self.detector:
            raise DuplicateContractError("detector is required")
        if self.decision_status not in DECISION_STATUSES:
            raise DuplicateContractError(
                f"{self.decision_status!r} is not a known decision status",
                known=sorted(DECISION_STATUSES),
            )
        if self.decision_status != DECISION_PENDING:
            raise DuplicateContractError(
                f"P0 creates pending candidates only; refusing to record {self.decision_status!r}",
                remediation="duplicate resolution is P1 work",
            )
        if not self.evidence:
            raise DuplicateContractError(
                "evidence is required; a duplicate claim with no recorded provenance "
                "cannot be reviewed"
            )
        if "algorithm" not in self.evidence or "provider" not in self.evidence:
            raise DuplicateContractError(
                "evidence must record algorithm and provider provenance",
                supplied=sorted(self.evidence),
            )
        for key in _walk_keys(self.evidence):
            if any(word in key.lower() for word in ("api_key", "token", "secret", "password")):
                raise DuplicateContractError(
                    f"evidence key {key!r} looks credential-bearing", offending_key=key
                )
        return replace(
            self,
            candidate_item_id=candidate,
            matched_item_id=matched,
            created_at=self.created_at or utc_now_iso(),
        )

    def evidence_json(self) -> str:
        return json.dumps(self.evidence, sort_keys=True, separators=(",", ":"))

    def evidence_identity(self) -> str:
        """Stable identity for the uniqueness constraint.

        Derived from the evidence rather than being the whole JSON blob, so
        two runs recording the same finding with cosmetically different
        payloads still collide."""
        material = json.dumps(
            {
                "provider_recording_id": self.provider_recording_id,
                "fingerprint_digest": self.fingerprint_digest,
                "algorithm": self.evidence.get("algorithm"),
                "provider": self.evidence.get("provider"),
            },
            sort_keys=True,
        )
        return hashlib.sha256(material.encode("utf-8")).hexdigest()[:32]


def _walk_keys(obj: Any) -> list[str]:
    keys: list[str] = []
    if isinstance(obj, dict):
        for key, value in obj.items():
            keys.append(str(key))
            keys.extend(_walk_keys(value))
    elif isinstance(obj, list):
        for item in obj:
            keys.extend(_walk_keys(item))
    return keys


class DuplicateRepository:
    """The only sanctioned way to record a duplicate candidate."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn

    def insert_acoustid_candidate(
        self,
        *,
        run_id: str,
        candidate_item_id: str,
        matched_item_id: str,
        provider_recording_id: str | None,
        fingerprint_digest: str | None,
        score: float | None,
        evidence: dict[str, Any],
        created_at: str | None = None,
    ) -> int | None:
        """
        Insert one AcoustID candidate. Returns its id, or None when an
        identical candidate already exists.

        Every non-generated column is named in the statement. `id` is
        database-generated and deliberately absent.
        """
        record = DuplicateCandidate(
            run_id=run_id,
            candidate_item_id=candidate_item_id,
            matched_item_id=matched_item_id,
            detector=DETECTOR_ACOUSTID,
            evidence=evidence,
            provider_recording_id=provider_recording_id,
            fingerprint_digest=fingerprint_digest,
            score=score,
            created_at=created_at,
        ).validated()
        return self.insert(record)

    def insert(self, record: DuplicateCandidate) -> int | None:
        validated = record.validated()
        cursor = self.conn.execute(
            """
            INSERT OR IGNORE INTO duplicates
                (run_id, candidate_item_id, matched_item_id, detector,
                 provider_recording_id, fingerprint_digest, score,
                 evidence_json, decision_status, created_at, evidence_identity)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                validated.run_id,
                validated.candidate_item_id,
                validated.matched_item_id,
                validated.detector,
                validated.provider_recording_id,
                validated.fingerprint_digest,
                validated.score,
                validated.evidence_json(),
                validated.decision_status,
                validated.created_at,
                validated.evidence_identity(),
            ),
        )
        return cursor.lastrowid if cursor.rowcount else None

    def pending(self, run_id: str | None = None) -> list[sqlite3.Row]:
        if run_id is None:
            return self.conn.execute(
                "SELECT * FROM duplicates WHERE decision_status = ? ORDER BY id",
                (DECISION_PENDING,),
            ).fetchall()
        return self.conn.execute(
            "SELECT * FROM duplicates WHERE decision_status = ? AND run_id = ? ORDER BY id",
            (DECISION_PENDING, run_id),
        ).fetchall()

    def count(self) -> int:
        return int(self.conn.execute("SELECT COUNT(*) FROM duplicates").fetchone()[0])
