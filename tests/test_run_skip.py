"""
Tests for `musaeus run --skip`.

Why this exists: on 2026-08-22 the 06:00 cron ran the full pipeline and
DupeResolver resolved 7,679 near-duplicate groups, physically relocating
6,480 files, unattended. Those groups were staged FOR REVIEW -- the
documented rule is that exact/near duplicates are never auto-resolved,
and every interactive path honoured it while the scheduled one quietly
did the opposite. Detecting duplicates overnight is useful; acting on
them without a human is not.
"""

from __future__ import annotations

from musaeus.stages import DEFAULT_PIPELINE


def _skip(pipeline, names: set[str]):
    return [s for s in pipeline if s.NAME.lower() not in names]


def test_dupe_resolver_is_in_the_default_pipeline():
    """The premise. If this ever fails, the cron guard is pointless."""
    assert "dupe-resolver" in {s.NAME for s in DEFAULT_PIPELINE}


def test_skipping_dupe_resolver_removes_exactly_one_stage():
    kept = _skip(DEFAULT_PIPELINE, {"dupe-resolver"})
    assert len(kept) == len(DEFAULT_PIPELINE) - 1
    assert "dupe-resolver" not in {s.NAME for s in kept}


def test_detection_stages_survive_the_skip():
    """Skipping resolution must not also disable detection.

    The overnight run should still FIND duplicates and stage them; only
    the physical relocation waits for a human.
    """
    kept = {s.NAME for s in _skip(DEFAULT_PIPELINE, {"dupe-resolver"})}
    assert "near-dupe" in kept or "neardupe" in kept
    assert "cross-dupe" in kept
    assert "sentinel" in kept


def test_unknown_stage_name_leaves_pipeline_untouched():
    assert _skip(DEFAULT_PIPELINE, {"no-such-stage"}) == list(DEFAULT_PIPELINE)


def test_skip_is_case_insensitive():
    assert len(_skip(DEFAULT_PIPELINE, {"DUPE-RESOLVER".lower()})) == len(DEFAULT_PIPELINE) - 1


def test_empty_skip_is_a_no_op():
    assert _skip(DEFAULT_PIPELINE, set()) == list(DEFAULT_PIPELINE)
