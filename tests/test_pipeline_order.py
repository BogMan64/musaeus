"""
MUSAEUS — Tests: DEFAULT_PIPELINE composition and order

Locks in the 2026-08-17 reorder (Grey's explicit call) so a future
accidental reorder is caught here rather than discovered live:
Permissions joined Act 1 right after Ingest; Canonicalize moved ahead of
dedup; Enrich/MBEnrich joined as default-on, positioned last.
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


def test_canonicalize_runs_before_dedup_not_after():
    """The 2026-08-17 reorder's core change: Canonicalize used to run
    after DupeResolver (Act 3), specifically to avoid wasting ffmpeg
    conversion on a file that gets deduped out. Grey's explicit call
    reverses this."""
    assert _index(CanonicalizeStage) < _index(CrossDupeStage)
    assert _index(CanonicalizeStage) < _index(NearDupeStage)
    assert _index(CanonicalizeStage) < _index(DupeResolverStage)


def test_dedup_still_runs_before_finalize():
    """Finalize's irreversible move-into-ALAC-Library + INBOX delete must
    still happen only after dedup has had its chance to pull a
    duplicate-loser out -- only Canonicalize's position changed, not
    dedup's position relative to Finalize."""
    assert _index(DupeResolverStage) < _index(FinalizeStage)


def test_enrichment_is_default_on_and_positioned_last():
    """2026-08-17, Grey's explicit call: default-on every run, after
    dedup -- implemented as strictly last (after Audit), deliberately
    isolated from the file-safety-critical stages."""
    assert EnrichStage in DEFAULT_PIPELINE
    assert MBEnrichStage in DEFAULT_PIPELINE
    last_two = DEFAULT_PIPELINE[-2:]
    assert last_two == [EnrichStage, MBEnrichStage]
    assert _index(AuditStage) < _index(EnrichStage)


def test_full_default_pipeline_order_matches_the_2026_08_17_reorder():
    assert [
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
        CanonicalizeStage,
        CrossDupeStage,
        NearDupeStage,
        DupeResolverStage,
        FinalizeStage,
        ForgeStage,
        TaggerStage,
        AuditStage,
        EnrichStage,
        MBEnrichStage,
    ] == DEFAULT_PIPELINE
