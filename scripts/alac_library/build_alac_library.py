#!/usr/bin/env python3
"""Phase 2A LUFS bake -- the command-line entry point.

The bake itself lives in musaeus/library_bake.py since 2026-09-25, because
the pipeline's LibraryBakeStage uses it too: a run now ends with masters in
ALAC-Archival and the -18 LUFS copies built from them, instead of leaving
this as a separate manual step. This file keeps the old command working.

    python3 scripts/alac_library/build_alac_library.py            # dry run
    python3 scripts/alac_library/build_alac_library.py --execute
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from musaeus.library_bake import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
