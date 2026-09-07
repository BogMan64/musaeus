"""A stage must not claim verification it did not perform.

`verify_effect` returned `[]` by default, and `_check_effect` did
`verified = not problems`. An empty list means "I looked and found nothing
wrong", so every stage that implemented no check inherited a ✓verified
seal: 25 of 39 stages.

AlbumArt was one of them. On 2026-08-31 a run in which every single embed
failed still reported:

    albumart: OK ✓verified | processed=10554 changed=10549

ART_EMBEDDED had been zero for the project's entire history. The seal was
the only thing anyone would have read.

A verification system whose default is a false positive is worse than
having none, because it converts silence into evidence. The fix is to
distinguish "I looked and found nothing" from "I did not look" -- an
empty list versus NO_VERIFICATION -- and to leave `verified` as None for
the second, so the seal goes unprinted and the result reads as no claim.

This is the same rule the session harness applies to itself: never claim
verification that was not actually observed.
"""

from __future__ import annotations

from musaeus.stages.base import NO_VERIFICATION, BaseStage


class TestSentinel:
    def test_no_verification_is_not_an_empty_list(self) -> None:
        """The whole point: these must be distinguishable."""
        assert NO_VERIFICATION is not []
        assert NO_VERIFICATION != []

    def test_it_is_falsy_so_careless_checks_still_read_as_no_problems(self) -> None:
        """`if problems:` must not treat "did not look" as "found faults"."""
        assert not NO_VERIFICATION
        assert bool(NO_VERIFICATION) is False

    def test_it_reprs_legibly_in_a_log(self) -> None:
        assert repr(NO_VERIFICATION) == "NO_VERIFICATION"


class TestDefaultMakesNoClaim:
    def test_base_verify_effect_returns_the_sentinel_not_an_empty_list(self) -> None:
        """The one-line change that stopped 25 stages claiming a check they
        never ran."""
        assert BaseStage.verify_effect(None, None, None) is NO_VERIFICATION  # type: ignore[arg-type]

    def test_a_stage_with_no_check_is_unverified_not_verified(self) -> None:
        from musaeus.context import StageResult

        class Bare(BaseStage):
            NAME = "bare"
            def run(self, ctx): ...
            def dry_run(self, ctx): ...
            def validate(self, ctx): ...

        result = StageResult(stage_name="bare", success=True)
        result.files_changed = 5
        Bare()._check_effect(None, result)  # type: ignore[arg-type]
        assert result.verified is None, "silence must not read as success"
        assert any("no effect verification" in n for n in result.verify_notes)

    def test_a_stage_that_checks_and_passes_is_verified(self) -> None:
        from musaeus.context import StageResult

        class Checks(BaseStage):
            NAME = "checks"
            def run(self, ctx): ...
            def dry_run(self, ctx): ...
            def validate(self, ctx): ...
            def verify_effect(self, ctx, result): return []

        result = StageResult(stage_name="checks", success=True)
        result.files_changed = 5
        Checks()._check_effect(None, result)  # type: ignore[arg-type]
        assert result.verified is True

    def test_a_stage_that_checks_and_finds_problems_is_not_verified(self) -> None:
        from musaeus.context import StageResult

        class Fails(BaseStage):
            NAME = "fails"
            def run(self, ctx): ...
            def dry_run(self, ctx): ...
            def validate(self, ctx): ...
            def verify_effect(self, ctx, result): return ["nothing changed on disk"]

        result = StageResult(stage_name="fails", success=True)
        result.files_changed = 5
        Fails()._check_effect(None, result)  # type: ignore[arg-type]
        assert result.verified is False
        assert "nothing changed on disk" in result.verify_notes


class TestSealReflectsTheClaim:
    def test_no_claim_prints_no_seal(self) -> None:
        """None must not render as ✓verified -- that is the whole failure."""
        from musaeus.context import StageResult

        r = StageResult(stage_name="x", success=True)
        r.verified = None
        assert "✓verified" not in r.summarise()

    def test_a_real_verification_does_print_the_seal(self) -> None:
        from musaeus.context import StageResult

        r = StageResult(stage_name="x", success=True)
        r.verified = True
        assert "✓verified" in r.summarise()
