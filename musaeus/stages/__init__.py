"""
MUSAEUS — Pipeline stages package.

Available stages:
  IngestStage    — scan inbox, register new files in archive
  SentinelStage  — compute audio + full hashes, detect exact duplicates
  ScholarStage   — extract ffprobe metadata, populate archive fields
  NormalizeStage — article-suffix fix + ALL-CAPS repair on archived metadata
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

Run order: Ingest → Sentinel → Scholar → Normalize → Forge → Tagger
On-demand: Auditor, Curator, Playlist, Ghost, Health, Enrich, MBEnrich, NearDupe
"""

from .auditor import AuditorStage
from .curator import CuratorStage
from .enrich import EnrichStage
from .forge import ForgeStage
from .ghost import GhostStage
from .health import HealthStage
from .ingest import IngestStage
from .mb_enrich import MBEnrichStage
from .neardupe import NearDupeStage
from .normalize import NormalizeStage
from .playlist import PlaylistStage
from .scholar import ScholarStage
from .sentinel import SentinelStage
from .tagger import TaggerStage

__all__ = [
    "IngestStage",
    "SentinelStage",
    "ScholarStage",
    "NormalizeStage",
    "ForgeStage",
    "TaggerStage",
    "AuditorStage",
    "CuratorStage",
    "PlaylistStage",
    "GhostStage",
    "HealthStage",
    "EnrichStage",
    "MBEnrichStage",
    "NearDupeStage",
]

# Canonical run order for the default pipeline
DEFAULT_PIPELINE: list[type] = [
    IngestStage,
    SentinelStage,
    ScholarStage,
]

# Extended pipeline (run with `musaeus run --full`)
FULL_PIPELINE: list[type] = [
    IngestStage,
    SentinelStage,
    ScholarStage,
    NormalizeStage,
    ForgeStage,
    TaggerStage,
]

# Maintenance pipeline (run with `musaeus run --maintain`)
MAINTAIN_PIPELINE: list[type] = [
    GhostStage,
    HealthStage,
    NormalizeStage,
    EnrichStage,
    MBEnrichStage,
    NearDupeStage,
]
