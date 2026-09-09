"""The protected-artist list lives in exactly one place.

2026-09-05. The same list had been copied into three modules and DIVERGED:

    curator             12 names
    artist_consolidate   9 names
    organize             6 names          <- the stage that names folders
    union               13 names

organize.py was missing seven, including Big Brother & The Holding Company,
Dr. Hook & The Medicine Show and Of Monsters and Men -- so the code
deciding a folder's name held the shortest list of names it must not take
apart. curator.py's own comment had already stated the rule the code was
breaking: "the whole point of a guard list is that it is consulted by
everything, not just the stage it happened to be written in."

Every entry was added because something folded it in real life. That is
what makes a divergent copy expensive rather than untidy.
"""

from __future__ import annotations

import ast
from pathlib import Path

from musaeus.canon.protected_artists import PROTECTED_ARTIST_NAMES, is_protected

_ROOT = Path(__file__).resolve().parent.parent
_HOME = _ROOT / "musaeus" / "canon" / "protected_artists.py"

#: A copy is a collection that re-lists names ALREADY in the canonical set.
#:
#: Keyed on the canon itself rather than on "looks like a band name", which
#: was the first version and was far too loose: it flagged console menu
#: options ("Soft reset — re-queue processed files, KEEPS quarantine..."),
#: SQL CREATE TABLE text, and spellcheck's list of confusable artist PAIRS
#: -- none of them copies of anything. A guard that cries wolf gets deleted.
_MIN_SHARED_ENTRIES = 2


def _copies_of_the_canon(tree: ast.AST) -> list[int]:
    out: list[int] = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.Set, ast.List, ast.Tuple)):
            continue
        strings = [
            e.value.strip().lower()
            for e in node.elts
            if isinstance(e, ast.Constant) and isinstance(e.value, str)
        ]
        shared = [s for s in strings if s in PROTECTED_ARTIST_NAMES]
        if len(shared) >= _MIN_SHARED_ENTRIES:
            out.append(node.lineno)
    return out


def test_no_module_redefines_the_protected_list() -> None:
    offenders: list[str] = []
    for path in sorted((_ROOT / "musaeus").rglob("*.py")):
        if path == _HOME:
            continue
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        except SyntaxError:  # pragma: no cover
            continue
        for lineno in _copies_of_the_canon(tree):
            offenders.append(f"{path.relative_to(_ROOT)}:{lineno}")
    assert not offenders, (
        "a literal list of comma/ampersand band names outside "
        "musaeus/canon/protected_artists.py — import PROTECTED_ARTIST_NAMES "
        f"instead: {offenders}"
    )


def test_the_guard_can_actually_see_a_violation() -> None:
    """A guard nobody has watched fail is a guard nobody should trust."""
    tree = ast.parse('X = {\n    "earth, wind & fire",\n    "hall & oates",\n}\n')
    assert _copies_of_the_canon(tree), "the detector misses the pattern it guards"


def test_the_guard_does_not_fire_on_prose_or_sql() -> None:
    """The first version flagged console menus and CREATE TABLE text. A guard
    that cries wolf gets deleted, so this pins what it must ignore."""
    for src in (
        'X = ["Soft reset — re-queue files, KEEPS quarantine/dupe/ghost", "Back"]',
        'Y = ("""CREATE TABLE t (a TEXT, b TEXT NOT NULL, c TEXT)""",)',
        'Z = [("keith", "keith & kristyn getty"), ("bon jovi", "jon bon jovi")]',
    ):
        assert not _copies_of_the_canon(ast.parse(src)), src[:40]


def test_the_union_of_the_old_copies_is_all_present() -> None:
    """Nothing was dropped in the merge. These are the 13 names the three
    divergent copies held between them."""
    for name in [
        "adam & the ants",
        "andrews sisters (the)",
        "barney bentall & the legendary hearts",
        "big brother & the holding company",
        "crosby, stills & nash",
        "crosby, stills, nash & young",
        "dr. hook & the medicine show",
        "earth, wind & fire",
        "hall & oates",
        "keith & kristyn getty",
        "of monsters and men",
        "simon & garfunkel",
        "sly & the family stone",
    ]:
        assert name in PROTECTED_ARTIST_NAMES, name


def test_every_stage_that_folds_artists_sees_the_same_object() -> None:
    from musaeus.stages import artist_consolidate, curator, organize

    assert curator._PROTECTED_ARTIST_NAMES is PROTECTED_ARTIST_NAMES
    assert organize._PROTECTED_ARTIST_NAMES is PROTECTED_ARTIST_NAMES
    assert artist_consolidate.PROTECTED_FULL_ARTIST_NAMES is PROTECTED_ARTIST_NAMES


def test_is_protected_folds_case_so_callers_cannot_forget() -> None:
    assert is_protected("Earth, Wind & Fire")
    assert is_protected("  of monsters AND men  ")
    assert not is_protected("Bob Seger")
    assert not is_protected(None)


# ── the identifier, not just the contents ────────────────────────────────────
#
# M-10, 2026-09-08. `PROTECTED_ARTIST_NAMES` was defined in two modules with
# **disjoint** contents: the canon's ampersand bands, and normalize.py's
# foreign-article lookalikes ("De La Soul"). Two frozensets, one name, two
# jobs.
#
# The test above could not see it, and that is not a flaw in it — it guards
# duplicated CONTENTS, needs `_MIN_SHARED_ENTRIES` matches to fire, and these
# two sets share nothing. It answered its own question correctly. Nobody had
# asked the other question.
#
# Nothing was actually broken: normalize leaves every canon name untouched,
# because none of them match its article or caps patterns. The hazard was a
# reader importing the wrong one and getting a guard that is silently empty
# for the names they meant — which is this project's recurring shape, a
# check that looks present and measures nothing.
#
# normalize's is now ARTICLE_LOOKALIKE_ARTISTS, named for what it protects
# against rather than for "protected" in general.


def test_only_the_canon_defines_the_protected_name() -> None:
    """One identifier, one home — regardless of what is in it."""
    offenders: list[str] = []
    for path in sorted((_ROOT / "musaeus").rglob("*.py")):
        if path == _HOME:
            continue
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        except SyntaxError:  # pragma: no cover
            continue
        for node in ast.walk(tree):
            target = None
            if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
                target = node.target
            elif (
                isinstance(node, ast.Assign)
                and len(node.targets) == 1
                and isinstance(node.targets[0], ast.Name)
            ):
                target = node.targets[0]
            if target is None or target.id != "PROTECTED_ARTIST_NAMES":
                continue
            # Re-exporting the canon's own object is the intended pattern
            # (curator, organize and artist_consolidate all do it); defining
            # a fresh literal under that name is not.
            value = node.value
            if isinstance(value, (ast.Set, ast.List, ast.Tuple, ast.Dict)) or (
                isinstance(value, ast.Call)
                and any(isinstance(a, (ast.Set, ast.List, ast.Tuple, ast.Dict)) for a in value.args)
            ):
                offenders.append(f"{path.relative_to(_ROOT)}:{target.lineno}")
    assert not offenders, (
        "PROTECTED_ARTIST_NAMES is defined with fresh contents outside "
        "musaeus/canon/protected_artists.py: "
        + ", ".join(offenders)
        + ". Two sets under one name is M-10 -- import the canon, or give "
        "yours a name that says what it protects against."
    )
