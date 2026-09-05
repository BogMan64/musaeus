"""How far a duration may drift before it means something.

Deliberately its own file: test_duration.py and test_duration_delegation.py
both carry a module-level skipif on ffmpeg/ffprobe being present, and these
are pure arithmetic. A rule this load-bearing should not stop being checked
because a binary is missing.
"""

from __future__ import annotations

import pytest

from musaeus.duration import TOLERANCE_SEC, tolerance_for


class TestToleranceFor:
    """P2-3, 2026-09-05. `max(2.0, recorded * 0.02)` was a sixth copy of the
    duration tolerance, sitting in canonicalize.verify_effect twelve lines
    below that file's own `from ..duration import TOLERANCE_SEC`. The repo's
    CLAUDE.md lists this exact defect as recurring -- "5 copies, 1.5 four
    times, 2.0 once, same stated rationale"."""

    def test_the_flat_tolerance_is_a_floor_not_the_whole_rule(self):
        # Short tracks: the floor governs.
        assert tolerance_for(30) == TOLERANCE_SEC
        assert tolerance_for(100) == TOLERANCE_SEC
        # Long ones: 2% scales past it. Collapsing this to a flat 2s would
        # make the check far stricter than the rule it replaced, on exactly
        # the long tracks where container rounding and encoder padding are
        # largest.
        assert tolerance_for(300) == pytest.approx(6.0)
        assert tolerance_for(486) == pytest.approx(9.72)

    @pytest.mark.parametrize("bad", [None, 0, -5])
    def test_a_missing_or_absurd_duration_falls_back_to_the_floor(self, bad):
        assert tolerance_for(bad) == TOLERANCE_SEC

    def test_it_matches_the_literal_it_replaced(self):
        """Behaviour-preserving by construction, pinned so it stays that way."""
        for recorded in (1, 30, 99, 100, 101, 160, 300, 486, 3600):
            assert tolerance_for(recorded) == pytest.approx(
                max(2.0, recorded * 0.02)
            ), recorded
