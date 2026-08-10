"""
MUSAEUS — Pipeline stages package.

Available stages:
  PreflightStage — environment sanity checks (commands, packages, disk,
                   DB integrity) -- report-only, runs first, never mutates
  IngestStage    — scan inbox, register new files in archive
  SentinelStage  — compute audio + full hashes, detect exact duplicates
  CrossDupeStage — flag files matching ALAC-Library content from a prior
                   batch, via the persistent cross-batch hash index (Act 2)
  ScholarStage   — extract ffprobe metadata, populate archive fields
  NormalizeStage — article-suffix fix + ALL-CAPS repair on archived metadata
  OrganizeStage  — rename and reorganize files into Artist/Album/ structure
  SanitizeStage  — filesystem-safe metadata (Windows/ExFAT/Android compatible)
  CanonicalizeStage — lossless->ALAC / sub-lossless->AAC, both as .m4a,
                   based on real ffprobe codec, not file extension (Act 3)
  FinalizeStage  — move canonicalized files INBOX -> ALAC-Library, the
                   trusted canonical library (Act 3)
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

Run order: Ingest → Sentinel → Scholar → Normalize → Organize → Forge → Tagger
On-demand: Auditor, Curator, Playlist, Ghost, Health, Enrich, MBEnrich,
           NearDupe, AcousticID, Transcode, Reviewer
"""

from .acousticid import AcousticIDStage
from .albumart import AlbumArtStage
from .artist_consolidate import ArtistConsolidateStage
from .auditor import AuditorStage
from .canonicalize import CanonicalizeStage
from .corrupt import CorruptStage
from .cross_dupe import CrossDupeStage
from .curator import CuratorStage
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
    "CanonicalizeStage",
    "FinalizeStage",
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

# Canonical run order for the default pipeline
DEFAULT_PIPELINE: list[type] = [
    PreflightStage,
    IngestStage,
    SentinelStage,
    ScholarStage,
]

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
