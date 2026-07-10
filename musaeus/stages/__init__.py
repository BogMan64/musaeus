"""
MUSAEUS — Pipeline stages package.

Available stages:
  IngestStage    — scan inbox, register new files in archive
  SentinelStage  — compute audio + full hashes, detect exact duplicates
  ScholarStage   — extract ffprobe metadata, populate archive fields
  ForgeStage     — measure EBU R128 loudness, write ReplayGain tags
  TaggerStage    — write normalised metadata from DB back to file tags
  CuratorStage   — build car-library export with optional noise profiles

Run order: Ingest → Sentinel → Scholar → Forge → Tagger
(Curator is on-demand; run separately with `musaeus curator`)
"""

from .curator import CuratorStage
from .forge import ForgeStage
from .ingest import IngestStage
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
