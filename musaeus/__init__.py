"""
MUSAEUS — Music Library Pipeline Framework
A clean-room reference implementation of ORPHEUS/NexusII principles.

Mythology: Musaeus was the student of Orpheus — keeper of sacred knowledge,
translator of the master's wisdom into written form.

Architecture:
  - One RunContext shared across all pipeline stages
  - One DB connection, one scan pass, one log session per run
  - Content-addressed audio hashing (tags don't break identity)
  - `archive` is primary. The event log is an audit trail, NOT a source
    the archive can be rebuilt from: hashes were written truncated to 16
    characters, and album, genre, year, track, duration, sample_rate,
    channels and codec were never recorded at all. `rebuild.py` has been
    disabled since 2026-08-21 for exactly that reason. Back up `archive`.
    (This line said the opposite until 2026-09-09 -- P2-C.)
  - Every stage MUST implement dry_run() — it is never optional

Version: 0.1.0
"""

__version__ = "0.1.0"
__author__ = "MUSAEUS contributors"


def utf8_stdio() -> None:
    """Write UTF-8 to stdout and stderr. Windows writes piped output in cp1252,
    which has no "✓", so a log file or the Companion's pipe crashed the run."""
    import sys

    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="backslashreplace")
