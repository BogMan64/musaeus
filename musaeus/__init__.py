"""
MUSAEUS — Music Library Pipeline Framework
A clean-room reference implementation of ORPHEUS/NexusII principles.

Mythology: Musaeus was the student of Orpheus — keeper of sacred knowledge,
translator of the master's wisdom into written form.

Architecture:
  - One RunContext shared across all pipeline stages
  - One DB connection, one scan pass, one log session per run
  - Content-addressed audio hashing (tags don't break identity)
  - Event log as the source of truth (DB is derived, always rebuildable)
  - Legacy CLI/console dry-run preview is temporarily unavailable pending the
    safe-preview repair

Version: 0.1.0
"""

__version__ = "0.1.0"
__author__ = "Grey + Claude"
