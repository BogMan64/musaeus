"""A held duplicate is finalized and legitimately placed -- not misplaced.

DUPE_REVIEW rows carry finalized_at, so audit's "must live under a final
root" check applies to them. The review queue used to sit inside
ALAC-Archival and passed for free; after it moved to vault_root/REVIEW on
2026-09-20 every held row was reported misplaced (198 errors, audit FAILED).
"""

from pathlib import Path


def test_audit_counts_the_review_queues_among_its_final_roots():
    src = (Path(__file__).resolve().parent.parent / "musaeus" / "stages" / "audit.py").read_text()
    block = src.split("final_roots = [", 1)[1].split("]", 1)[0]
    assert "dupes_review_dir" in block, "held dupes would be reported misplaced"
    assert "tribute_review_dir" in block
    assert "alac_library" in block and "alac_archive" in block


def test_the_roots_are_config_derived_not_folder_names():
    src = (Path(__file__).resolve().parent.parent / "musaeus" / "stages" / "audit.py").read_text()
    block = src.split("final_roots = [", 1)[1].split("]", 1)[0]
    assert "DUPES_MOVED" not in block, "name-based root reintroduced"
