"""
MUSAEUS — Tests: DEFAULT_PIPELINE composition and order

Locks in the current pipeline shape so a future accidental reorder is
caught here rather than discovered live. History: 2026-08-17 moved
Permissions into Act 1, moved Enrich/MBEnrich to default-on (both
stand), and briefly moved Canonicalize ahead of dedup. That last change
was REVERTED 2026-08-18 once cross-format duplicate detection (the
motivating case) was confirmed to already work via PCM-based audio_hash
without it -- see musaeus/stages/__init__.py's module docstring.
"""
from __future__ import annotations

from musaeus.stages import DEFAULT_PIPELINE
from musaeus.stages.albumart import AlbumArtStage
from musaeus.stages.artist_consolidate import ArtistConsolidateStage
from musaeus.stages.audit import AuditStage
from musaeus.stages.canonicalize import CanonicalizeStage
from musaeus.stages.corrupt import CorruptStage
from musaeus.stages.cross_dupe import CrossDupeStage
from musaeus.stages.dupe_resolver import DupeResolverStage
from musaeus.stages.enrich import EnrichStage
from musaeus.stages.finalize import FinalizeStage
from musaeus.stages.forge import ForgeStage
from musaeus.stages.health import HealthStage
from musaeus.stages.ingest import IngestStage
from musaeus.stages.mb_enrich import MBEnrichStage
from musaeus.stages.neardupe import NearDupeStage
from musaeus.stages.normalize import NormalizeStage
from musaeus.stages.permissions import PermissionsStage
from musaeus.stages.preflight import PreflightStage
from musaeus.stages.sanitize import SanitizeStage
from musaeus.stages.scholar import ScholarStage
from musaeus.stages.sentinel import SentinelStage
from musaeus.stages.tagger import TaggerStage


def _index(cls: type) -> int:
    return DEFAULT_PIPELINE.index(cls)


def test_permissions_is_in_default_pipeline_right_after_ingest():
    assert _index(PermissionsStage) == _index(IngestStage) + 1


def test_dedup_runs_before_canonicalize_not_after():
    """REVERTED 2026-08-18: Canonicalize runs after dedup again, its
    original position -- so a confirmed duplicate is pulled before any
    ffmpeg conversion is wasted on it. The 2026-08-17 reversal was
    checked and found to fix nothing: sentinel.py's audio_hash is
    PCM-based, so cross-format duplicates (e.g. a FLAC and its future
    ALAC-in-.m4a twin) already match as EXACT duplicates without
    Canonicalize running first."""
    assert _index(CrossDupeStage) < _index(CanonicalizeStage)
    assert _index(NearDupeStage) < _index(CanonicalizeStage)
    assert _index(DupeResolverStage) < _index(CanonicalizeStage)


def test_canonicalize_still_runs_before_finalize():
    assert _index(CanonicalizeStage) < _index(FinalizeStage)


def test_enrichment_is_default_on_and_positioned_last():
    """2026-08-17, Grey's explicit call: default-on every run, after
    dedup -- implemented as strictly last (after Audit), deliberately
    isolated from the file-safety-critical stages. Unaffected by the
    2026-08-18 Canonicalize revert."""
    assert EnrichStage in DEFAULT_PIPELINE
    assert MBEnrichStage in DEFAULT_PIPELINE
    last_two = DEFAULT_PIPELINE[-2:]
    assert last_two == [EnrichStage, MBEnrichStage]
    assert _index(AuditStage) < _index(EnrichStage)


def test_full_default_pipeline_order_matches_current_design():
    expected = [
        PreflightStage,
        IngestStage,
        PermissionsStage,
        SentinelStage,
        ScholarStage,
        HealthStage,
        CorruptStage,
        AlbumArtStage,
        NormalizeStage,
        SanitizeStage,
        ArtistConsolidateStage,
        CrossDupeStage,
        NearDupeStage,
        DupeResolverStage,
        CanonicalizeStage,
        FinalizeStage,
        ForgeStage,
        TaggerStage,
        AuditStage,
        EnrichStage,
        MBEnrichStage,
    ]
    assert expected == DEFAULT_PIPELINE
