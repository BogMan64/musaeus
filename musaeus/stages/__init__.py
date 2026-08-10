"""
MUSAEUS — Pipeline stages package.

Available stages:
  PreflightStage — environment sanity checks (commands, packages, disk,
                   DB integrity) -- report-only, runs first, never mutates
  IngestStage    — scan inbox, register new files in archive
  SentinelStage  — compute audio + full hashes, detect exact duplicates
  CrossDupeStage — flag files matching ALAC-Library content from a prior
                   batch, via the persistent cross-batch hash index (Act 2)
  DupeResolverStage — physically relocate duplicate-group losers into
                   ALAC-Library/DUPES_MOVED_FOR_REVIEW/, mirroring
                   ALAC-Library's own Artist/Album/Track shape (Act 2)
  ScholarStage   — extract ffprobe metadata, populate archive fields
  NormalizeStage — article-suffix fix + ALL-CAPS repair on archived metadata
  OrganizeStage  — rename and reorganize files into Artist/Album/ structure
  SanitizeStage  — filesystem-safe metadata (Windows/ExFAT/Android compatible)
  CanonicalizeStage — lossless->ALAC / sub-lossless->AAC, both as .m4a,
                   based on real ffprobe codec, not file extension (Act 3)
  FinalizeStage  — move canonicalized files INBOX -> ALAC-Library, the
                   trusted canonical library (Act 3)
  AuditStage     — physical-presence verification gate before a batch's
                   DB can be snapshotted and wiped (Act 3)
  ForgeStage     — measure EBU R128 loudness, write ReplayGain tags
  TaggerStage    — write normalised metadata from DB back to file tags
  AuditorStage   — pre-forge LUFS audit (flags out-of-window files)
  CuratorStage   — build car-library export with optional noise profiles
  PlaylistStage  — build per-genre M3U8 playlists from the archive
  GhostStage     — mark archive entries whose files no longer exist on disk
  HealthStage    — library-wide consistency and quality checks
  EnrichStage    — Last.fm genre enrichment for tracks with missing genre
  MBEnrichStage  — MusicBrainz artist + release MBID enrichment
  NearDupeStage  — metadata-based near-duplicate detection (fuzzy title match)
  AcousticIDStage — acoustic fingerprint dedup via fpcalc + AcousticID API
  TranscodeStage  — lossless → 256k AAC export via ffmpeg
  ReviewerStage   — Groq AI metadata quality review

DEFAULT_PIPELINE (`musaeus run`) is the full Act 1/2/3 canonical chain:
  Act 1 (Intake & Correction): Preflight → Ingest → Sentinel → Scholar →
         Health → Corrupt → Normalize → Sanitize → ArtistConsolidate
  Act 2 (Dedup & Staging):     CrossDupe → NearDupe → DupeResolver
  Act 3 (Canonicalize/Finalize): Canonicalize → Finalize → Forge →
         Tagger → Audit
See ACT1_INTAKE_CORRECTION / ACT2_DEDUP_STAGING / ACT3_CANONICALIZE_FINALIZE
below for the named building blocks and the reasoning behind this order.
On-demand only (not part of the canonical chain): Auditor, Curator,
           Playlist, Ghost, Enrich, MBEnrich, AcousticID, Transcode,
           Reviewer, Organize, IntegrityStage, AlbumArt.
"""

from .acousticid import AcousticIDStage
from .albumart import AlbumArtStage
from .artist_consolidate import ArtistConsolidateStage
from .audit import AuditStage
from .auditor import AuditorStage
from .canonicalize import CanonicalizeStage
from .corrupt import CorruptStage
from .cross_dupe import CrossDupeStage
from .curator import CuratorStage
from .dupe_resolver import DupeResolverStage
from .enrich import EnrichStage
from .finalize import FinalizeStage
from .forge import ForgeStage
from .ghost import GhostStage
from .health import HealthStage
from .ingest import IngestStage
from .integrity import IntegrityStage
from .mb_enrich import MBEnrichStage
from .neardupe import NearDupeStage
from .normalize import NormalizeStage
from .organize import OrganizeStage
from .playlist import PlaylistStage
from .preflight import PreflightStage
from .reviewer import ReviewerStage
from .sanitize import SanitizeStage
from .scholar import ScholarStage
from .sentinel import SentinelStage
from .tagger import TaggerStage
from .transcode import TranscodeStage

__all__ = [
    "PreflightStage",
    "IngestStage",
    "SentinelStage",
    "ScholarStage",
    "NormalizeStage",
    "OrganizeStage",
    "SanitizeStage",
    "CrossDupeStage",
    "DupeResolverStage",
    "CanonicalizeStage",
    "FinalizeStage",
    "AuditStage",
    "ForgeStage",
    "TaggerStage",
    "AuditorStage",
    "CuratorStage",
    "PlaylistStage",
    "GhostStage",
    "HealthStage",
    "CorruptStage",
    "ArtistConsolidateStage",
    "EnrichStage",
    "MBEnrichStage",
    "NearDupeStage",
    "AcousticIDStage",
    "TranscodeStage",
    "ReviewerStage",
    "IntegrityStage",
    "AlbumArtStage",
]

# ── Act 1/2/3 pipeline (canonical, dependency-respecting order) ─────────────
#
# Confirmed with Grey (2026-08-09/10 session) and verified end-to-end via a
# real full-chain dry run against a scratch vault (Preflight through Audit,
# covering PASSTHROUGH/CONVERT/TRANSCODE, an EXACT duplicate, and a flagged
# file) before being written here. Two real bugs were found and fixed
# during that verification run rather than assumed away -- see git log for
# "archive.file_path didn't follow files moved by DupeResolver/Corrupt" and
# "keeper selection ignored codec, could discard the lossless copy".
#
# Act 1 - Intake & Correction. Corrupt/Health/Normalize/Sanitize/
#   ArtistConsolidate all read fields Scholar populates (status=
#   'CATALOGUED', codec, bitrate, duration), so despite being conceptually
#   "intake", they run AFTER Sentinel+Scholar, not before -- a real data
#   dependency, not a stylistic choice.
# Act 2 - Dedup & Staging. CrossDupe needs audio_hash (Sentinel), so it
#   can't literally run before Sentinel either -- it runs as early after
#   Sentinel as a hash-based check can. NearDupe benefits from running
#   after ArtistConsolidate (canon-resolved artist names). DupeResolver
#   runs LAST in this Act, once every dedup check has had a chance to flag
#   something for this batch -- so a confirmed duplicate is physically
#   pulled out before Act 3 wastes any ffmpeg conversion or loudness
#   measurement on it.
# Act 3 - Canonicalize, Finalize, Forge, Tagger, Audit. Finalize runs
#   BEFORE Forge/Tagger per Grey's explicit request: this lets an
#   external archival copy be made of the canonicalized-but-not-yet-
#   loudness-tagged file, straight out of its permanent ALAC-Library
#   location, before Forge's ReplayGain tags get burned into it. Audit
#   runs last, as the gate before a future DB-snapshot-and-wipe step.

ACT1_INTAKE_CORRECTION: list[type] = [
    PreflightStage,
    IngestStage,
    SentinelStage,
    ScholarStage,
    HealthStage,
    CorruptStage,
    NormalizeStage,
    SanitizeStage,
    ArtistConsolidateStage,
]

ACT2_DEDUP_STAGING: list[type] = [
    CrossDupeStage,
    NearDupeStage,
    DupeResolverStage,
]

ACT3_CANONICALIZE_FINALIZE: list[type] = [
    CanonicalizeStage,
    FinalizeStage,
    ForgeStage,
    TaggerStage,
    AuditStage,
]

# The full canonical pipeline: Act 1 + Act 2 + Act 3, in order. This is
# what `musaeus run` executes by default going forward, and what
# musaeus_overnight.sh's cron entry point should call stage-by-stage in
# this same order.
CANONICAL_PIPELINE: list[type] = (
    ACT1_INTAKE_CORRECTION + ACT2_DEDUP_STAGING + ACT3_CANONICALIZE_FINALIZE
)

# Canonical run order for the default pipeline -- the full Act 1/2/3 chain.
# Was previously just [Preflight, Ingest, Sentinel, Scholar]; expanded to
# the complete chain now that Canonicalize/Finalize/CrossDupe/DupeResolver/
# Audit exist and have been verified end-to-end (see CANONICAL_PIPELINE
# comment above for the full reasoning). `musaeus run --dry-run` remains
# the safe way to preview this before ever running it live.
DEFAULT_PIPELINE: list[type] = CANONICAL_PIPELINE

# Extended pipeline (run with `musaeus run --full`)
FULL_PIPELINE: list[type] = [
    PreflightStage,
    IngestStage,
    SentinelStage,
    ScholarStage,
    NormalizeStage,
    SanitizeStage,
    ForgeStage,
    TaggerStage,
]

# Archive pipeline (run with `musaeus run --archive`) — full minus LUFS/ReplayGain
ARCHIVE_PIPELINE: list[type] = [
    PreflightStage,
    IngestStage,
    SentinelStage,
    ScholarStage,
    NormalizeStage,
    SanitizeStage,
    GhostStage,
    HealthStage,
    IntegrityStage,
    EnrichStage,
    MBEnrichStage,
    NearDupeStage,
    AcousticIDStage,
    AlbumArtStage,
    TaggerStage,
    ReviewerStage,
]

# Big Kahuna (run with `musaeus run --big-kahuna`) — everything including LUFS
BIG_KAHUNA_PIPELINE: list[type] = [
    PreflightStage,
    IngestStage,
    SentinelStage,
    ScholarStage,
    NormalizeStage,
    SanitizeStage,
    GhostStage,
    HealthStage,
    IntegrityStage,
    EnrichStage,
    MBEnrichStage,
    NearDupeStage,
    AcousticIDStage,
    AlbumArtStage,
    ForgeStage,
    TaggerStage,
    CuratorStage,
    PlaylistStage,
    ReviewerStage,
]

# Maintenance pipeline (run with `musaeus run --maintain`)
MAINTAIN_PIPELINE: list[type] = [
    PreflightStage,
    GhostStage,
    HealthStage,
    NormalizeStage,
    SanitizeStage,
    ArtistConsolidateStage,
    EnrichStage,
    MBEnrichStage,
    NearDupeStage,
]

# Enrichment pipeline (run with `musaeus run --enrich`)
ENRICH_PIPELINE: list[type] = [
    EnrichStage,
    MBEnrichStage,
    AcousticIDStage,
    ReviewerStage,
]
