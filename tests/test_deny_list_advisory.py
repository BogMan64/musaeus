"""An unverified deny-list reason must not quarantine.

All 82 entries reading "removed from the library by owner decision" were
written in the same second -- 2026-08-24 18:25:31 -- by a bulk backfill
seeded from "ledger entries with no live file". Absence is not a decision:
the corroborating case is The Communards, whose sibling track in the same
batch is GHOST carrying the pipeline's own note "file absent from the
vault after an interrupted run".

Cross-referenced against every removal log: 15 of the 82 were real
removals, 67 were not. Those 67 permanently refused genuine records with
a message asserting an intent the data could not support -- one of them a
1986 Communards single that MusicBrainz scores 100.

Re-labelled and downgraded to advisory 2026-08-31 on Grey's instruction.
The entries stay on the list, because the observation is still true and
worth surfacing; they just no longer act on their own.
"""

from __future__ import annotations

from musaeus.stages.deny_list import _ADVISORY_REASONS, _is_advisory

_BACKFILL = "absent from the library at the 2026-08-24 backfill; cause unrecorded"


def test_the_backfill_reason_is_advisory() -> None:
    assert _is_advisory(_BACKFILL) is True


def test_verified_owner_removal_still_binds() -> None:
    """The 15 that appear in a removal log must keep quarantining."""
    assert _is_advisory("removed from the library by owner decision") is False
    assert _is_advisory("removed as a lossy duplicate by owner decision") is False


def test_knockoff_reasons_still_bind() -> None:
    """The 25 from the actual knock-off review were per-item decisions."""
    assert _is_advisory("knock-off: karaoke/tribute/cover, not a true recording artist") is False
    assert _is_advisory("knock-off: karaoke/tribute/cover") is False


def test_unknown_and_empty_reasons_bind_by_default() -> None:
    """Advisory is an explicit allow-list. Anything unrecognised keeps the
    safe behaviour -- a new reason must not become advisory by accident."""
    assert _is_advisory("some future reason") is False
    assert _is_advisory("") is False
    assert _is_advisory(None) is False


def test_advisory_list_is_exact_not_substring() -> None:
    """Matching loosely would let a longer reason that merely contains the
    advisory text slip through unenforced."""
    assert _is_advisory(_BACKFILL + " and deliberately purged") is False
    assert _BACKFILL in _ADVISORY_REASONS
