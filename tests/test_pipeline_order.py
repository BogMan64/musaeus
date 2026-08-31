"""
MUSAEUS — Tests: DEFAULT_PIPELINE composition and order

Locks in the current pipeline shape so a future accidental reorder is
caught here rather than discovered live. History: 2026-08-17 moved
Permissions into Act 1, moved Enrich/MBEnrich to default-on (both
stand), and briefly moved Canonicalize ahead of dedup. That last change
was REVERTED 2026-08-18 once cross-format duplicate detection (the
motivating case) was confirmed to already work via PCM-based audio_hash
without it -- see musaeus/stages/__init__.py's module docstring.
2026-08-19: BPM and VariousArtistsFix moved from on-demand-only to
default-on (Grey's explicit call) -- BPM after Finalize near Forge,
VariousArtistsFix at the end of Act 1 right after ArtistConsolidate.
"""

from __future__ import annotations

from musaeus.stages import DEFAULT_PIPELINE
from musaeus.stages.acousticid import AcousticIDStage
from musaeus.stages.albumart import AlbumArtStage
from musaeus.stages.artist_consolidate import ArtistConsolidateStage
from musaeus.stages.audit import AuditStage
from musaeus.stages.bpm import BPMStage
from musaeus.stages.canonicalize import CanonicalizeStage
from musaeus.stages.classical_composer import ClassicalComposerStage
from musaeus.stages.corrupt import CorruptStage
from musaeus.stages.cross_dupe import CrossDupeStage
from musaeus.stages.deny_list import DenyListStage
from musaeus.stages.dupe_resolver import DupeResolverStage
from musaeus.stages.enrich import EnrichStage
from musaeus.stages.finalize import FinalizeStage
from musaeus.stages.forge import ForgeStage
from musaeus.stages.genre_validate import GenreValidateStage
from musaeus.stages.health import HealthStage
from musaeus.stages.identity_tag import IdentityTagStage
from musaeus.stages.ingest import IngestStage
from musaeus.stages.mb_enrich import MBEnrichStage
from musaeus.stages.neardupe import NearDupeStage
from musaeus.stages.normalize import NormalizeStage
from musaeus.stages.organize import OrganizeStage
from musaeus.stages.permissions import PermissionsStage
from musaeus.stages.preflight import PreflightStage
from musaeus.stages.sanitize import SanitizeStage
from musaeus.stages.scholar import ScholarStage
from musaeus.stages.sentinel import SentinelStage
from musaeus.stages.spellcheck import SpellCheckStage
from musaeus.stages.tagger import TaggerStage
from musaeus.stages.various_artists_fix import VariousArtistsFixStage


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


def test_bpm_runs_after_finalize_before_forge():
    """2026-08-19, Grey's explicit call: BPM near Forge, after Finalize
    -- its tag-read-first shortcut + bpm_analyzed_at resumability mean
    the Essentia cost is paid once per new file, not on every run."""
    assert _index(FinalizeStage) < _index(BPMStage) < _index(ForgeStage)


def test_various_artists_fix_runs_at_end_of_act1_before_dedup():
    """2026-08-19, Grey's explicit call: resolve the real artist before
    Act 2's dedup runs, same logic that already put ArtistConsolidate
    ahead of dedup."""
    assert _index(ArtistConsolidateStage) < _index(VariousArtistsFixStage)
    assert _index(VariousArtistsFixStage) < _index(CrossDupeStage)


def test_enrichment_is_default_on_and_positioned_last():
    """2026-08-17, Grey's explicit call: default-on every run, after
    dedup -- implemented as strictly last (after Audit), deliberately
    isolated from the file-safety-critical stages. Unaffected by the
    2026-08-18 Canonicalize revert."""
    assert EnrichStage in DEFAULT_PIPELINE
    assert MBEnrichStage in DEFAULT_PIPELINE

    # Enrichment grew on 2026-08-30 (AcousticID, IdentityTag), so this
    # asserts the PROPERTY Grey's call was about -- the whole enrichment
    # block is contiguous and strictly last, isolated from the
    # file-safety-critical stages -- rather than a fixed pair.
    from musaeus.stages import ENRICHMENT

    assert DEFAULT_PIPELINE[-len(ENRICHMENT):] == ENRICHMENT
    assert _index(AuditStage) < _index(EnrichStage)

    # Within the block, order is a dependency chain, not a preference.
    assert _index(MBEnrichStage) < _index(AcousticIDStage), (
        "text lookup is cheap and settles most rows; fingerprinting should "
        "ask about the remainder, not the library"
    )
    assert _index(AcousticIDStage) < _index(IdentityTagStage), (
        "identity is written to the files last, after everything that "
        "resolves it -- otherwise it writes what the run is about to learn"
    )


def test_full_default_pipeline_order_matches_current_design():
    expected = [
        PreflightStage,
        IngestStage,
        PermissionsStage,
        SentinelStage,
        # Immediately after Sentinel: that is the first point an audio_hash
        # exists, and it is before any stage invests work in a file that is
        # about to be refused. Added 2026-08-24.
        DenyListStage,
        ScholarStage,
        HealthStage,
        CorruptStage,
        AlbumArtStage,
        NormalizeStage,
        # Report-only, added 2026-08-22. Sits after Normalize so it compares
        # names in canonical article form, and before Sanitize/dedupe so a
        # flagged misspelling is visible before anything groups on it.
        SpellCheckStage,
        SanitizeStage,
        ArtistConsolidateStage,
        VariousArtistsFixStage,
        # Genre is settled at the end of intake, once the artist is
        # canonical: the law is keyed on artist. Added 2026-08-24 -- it had
        # only ever run on demand, so a new file's genre came from its own
        # tags and nothing checked it against MasterLaw.
        GenreValidateStage,
        CrossDupeStage,
        NearDupeStage,
        DupeResolverStage,
        # Composer attribution AFTER dedup. Filing classical under the
        # composer collapses performers into a few artists, and NearDupe
        # compares titles within an artist -- which quarantined 15 distinct
        # recordings as near-duplicates on 2026-08-25, including two
        # different movements of the same Handel suite at 89%.
        ClassicalComposerStage,
        CanonicalizeStage,
        FinalizeStage,
        BPMStage,
        ForgeStage,
        TaggerStage,
        # Organize BEFORE Audit: Audit is the documented gate, and with
        # Organize after it every audit result described a layout Organize
        # then rewrote.
        OrganizeStage,
        AuditStage,
        EnrichStage,
        MBEnrichStage,
        AcousticIDStage,
        IdentityTagStage,
    ]
    assert expected == DEFAULT_PIPELINE
