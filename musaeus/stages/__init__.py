"""
MUSAEUS — Pipeline stages package.

Available stages:
  IngestStage    — scan inbox, register new files in archive
  SentinelStage  — compute audio + full hashes, detect exact duplicates
  ScholarStage   — extract ffprobe metadata, populate archive fields

Run order: Ingest → Sentinel → Scholar
"""

from .ingest import IngestStage
from .scholar import ScholarStage
from .sentinel import SentinelStage

__all__ = ["IngestStage", "SentinelStage", "ScholarStage"]

# Canonical run order for the default pipeline
DEFAULT_PIPELINE: list[type] = [IngestStage, SentinelStage, ScholarStage]
