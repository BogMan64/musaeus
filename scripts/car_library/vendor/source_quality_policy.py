#!/usr/bin/env python3
"""
ORPHEUS — Source Quality Policy
Console: standalone — imported by build_aac_library.py, convert_flac_to_alac_v2.py
Purpose: Library wrapper around source_quality_gate.py that exposes probe_quality_policy() for callers to decide whether a file should be preserved lossless or converted to AAC.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

from lib.orpheus_paths import SCRIPTS_DIR

GATE_SCRIPT = SCRIPTS_DIR / "source_quality_gate.py"


def probe_quality_policy(file_path: str | Path) -> dict:
    file_path = str(Path(file_path))
    cmd = ["python3", str(GATE_SCRIPT), file_path, "--json"]
    result = subprocess.run(cmd, capture_output=True, text=True)

    if result.returncode != 0:
        return {
            "classification": "unknown",
            "preserve_lossless": False,
            "aac_allowed": True,
            "probe_error": result.stderr.strip() or "gate failed",
        }

    try:
        payload = json.loads(result.stdout)
        if not payload:
            return {
                "classification": "unknown",
                "preserve_lossless": False,
                "aac_allowed": True,
                "probe_error": "empty gate output",
            }

        item = payload[0]
        return {
            "classification": item.get("classification", "unknown"),
            "preserve_lossless": bool(item.get("preserve_lossless", False)),
            "aac_allowed": bool(item.get("aac_allowed", False)),
            "probe_error": None,
        }
    except Exception as exc:
        return {
            "classification": "unknown",
            "preserve_lossless": False,
            "aac_allowed": True,
            "probe_error": f"json parse failure: {exc}",
        }


def should_make_aac(file_path: str | Path) -> tuple[bool, dict]:
    """
    Decide whether to create a lossy AAC derivative copy of *file_path*
    (e.g. for the car/portable/ragtop libraries).

    This is a different question from the intake gate's own aac_allowed
    field, which decides whether an INCOMING file is trustworthy enough to
    promote into the lossless vault — not whether an ADDITIONAL compressed
    copy should be made from an already-vaulted source.

    The only case that should be refused here is a source that is already
    lossy: re-encoding lossy audio into another lossy format (AAC) is
    wasteful and quality-destructive. True-lossless, fake-lossless-suspected,
    and unknown sources are all fine to derive an AAC copy from — this never
    touches or replaces the source file, it only writes an additional
    compressed copy elsewhere.
    """
    policy = probe_quality_policy(file_path)
    classification = policy["classification"]

    if classification == "lossy":
        return False, policy

    return True, policy
