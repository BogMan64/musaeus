"""No stage may claim an effect and check nothing.

2026-09-05. Of 39 stages, 17 had a verify_effect and exactly one
(SpellCheck) had ever declared CLAIMS_EFFECT = False. The other 21 claimed
an effect and checked nothing -- and because the default returns
NO_VERIFICATION rather than an empty list, they were honest about it but
indistinguishable from stages nobody had got to yet.

After 2026-09-08 the seal is the only signal anyone reads. This test makes
"which is it" a decision on the record: every stage must either check its
effect, or say in one line that it makes no claim.
"""

from __future__ import annotations

import inspect

import musaeus.stages as S
from musaeus.stages.base import BaseStage


def _stages():
    for name in sorted(dir(S)):
        obj = getattr(S, name)
        if inspect.isclass(obj) and issubclass(obj, BaseStage) and obj is not BaseStage:
            yield name, obj


def test_no_stage_is_silent() -> None:
    """A stage with neither a check nor a declaration is an omission."""
    silent = [
        name for name, cls in _stages()
        if "verify_effect" not in cls.__dict__ and cls.CLAIMS_EFFECT
    ]
    assert not silent, (
        "these stages claim an effect but never check it — give them a "
        f"verify_effect(), or CLAIMS_EFFECT = False with a reason: {silent}"
    )


def test_a_stage_declaring_no_claim_says_so_in_its_own_body() -> None:
    """Inherited CLAIMS_EFFECT = True is the default, not a decision. A
    stage that makes no claim must set it itself, where a reader looking at
    that file can see it."""
    undeclared = [
        name for name, cls in _stages()
        if not cls.CLAIMS_EFFECT and "CLAIMS_EFFECT" not in cls.__dict__
    ]
    assert not undeclared, f"CLAIMS_EFFECT inherited rather than declared: {undeclared}"


def test_the_counts_are_what_we_think_they_are() -> None:
    """A canary. If this fails, a stage was added or removed -- decide what
    it claims rather than letting it default into silence."""
    stages = list(_stages())
    verified = [n for n, c in stages if "verify_effect" in c.__dict__]
    declared = [n for n, c in stages if not c.CLAIMS_EFFECT]
    assert len(stages) == len(verified) + len(declared), (
        f"{len(stages)} stages, {len(verified)} verify, {len(declared)} declare "
        "— every stage must be in exactly one of those groups"
    )
