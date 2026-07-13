"""
MUSAEUS — Pipeline stages package.

Available stages:
  IngestStage    — scan inbox, register new files in archive
  SentinelStage  — compute audio + full hashes, detect exact duplicates
  ScholarStage   — extract ffprobe metadata, populate archive fields
  ForgeStage     — measure EBU R128 loudness, write ReplayGain tags
  TaggerStage    — write normalised metadata from DB back to file tags
  CuratorStage   — build car-library export with optional noise profiles
  PlaylistStage  — build per-genre M3U8 playlists from the archive
  GhostStage     — mark archive entries whose files no longer exist on disk
  HealthStage    — library-wide consistency and quality checks
  EnrichStage    — Last.fm genre enrichment for tracks with missing genre
  NearDupeStage  — metadata-based near-duplicate detection (fuzzy title match)

Run order: Ingest → Sentinel → Scholar → Forge → Tagger
On-demand: Curator, Playlist, Ghost, Health, Enrich, NearDupe
"""

from .curator import CuratorStage
from .enrich import EnrichStage
from .forge import ForgeStage
from .ghost import GhostStage
from .health import HealthStage
from .ingest import IngestStage
from .neardupe import NearDupeStage
from .playlist import PlaylistStage
from .scholar import ScholarStage
from .sentinel import SentinelStage
from .tagger import TaggerStage

__all__ = [
    "IngestStage",
    "SentinelStage",
    "ScholarStage",
    "ForgeStage",
    "TaggerStage",
    "CuratorStage",
    "PlaylistStage",
    "GhostStage",
    "HealthStage",
    "EnrichStage",
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
    ForgeStage,
    TaggerStage,
]

# Maintenance pipeline (run with `musaeus run --maintain`)
MAINTAIN_PIPELINE: list[type] = [
    GhostStage,
    HealthStage,
    EnrichStage,
    NearDupeStage,
]
