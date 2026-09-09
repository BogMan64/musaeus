"""`_allowed` and `_allowed_lower` must describe the same genres.

M-09 in the Repair Register, 2026-09-08.

`_load()` cleared `_allowed` unconditionally but only rebuilt
`_allowed_lower` *inside* `if self._allowed_path.exists():`. Remove the
allowed file and reload, and the two disagreed — `_allowed` held nothing
while `_allowed_lower` still held the previous run's entries.

`resolve()` then walks them together with `zip(..., strict=True)`, so the
next lookup raised

    ValueError: zip() argument 2 is longer than argument 1

where the documented contract is "returns None when no suitable match is
found (caller decides what to do)". A canon file going missing should
degrade to "no opinion", not to an exception in the middle of a stage.

**The strict zip is not the defect and is deliberately kept.** It converted a
silent mis-pairing into a loud one. Without it, the fuzzy matcher would have
scored each genre against some *other* genre's lower-cased text and returned
a confident wrong answer — this project's worst failure shape, and far harder
to notice than a traceback. The fix is to make the invariant impossible to
break, by deriving `_allowed_lower` from `_allowed` outside the branch, and
to leave the alarm in place.

That is the difference worth keeping in mind: `strict=True` is a guard that
*did* fire. Most of this register is guards that could not.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from musaeus.canon.genre import GenreCanon


@pytest.fixture
def canon(tmp_path: Path) -> GenreCanon:
    allowed = tmp_path / "Genre_Allowed.txt"
    allowed.write_text("Rock\nJazz\nBlues\n", encoding="utf-8")
    mapping = tmp_path / "Genre_Canonical_Map.txt"
    mapping.write_text("rok => Rock\n", encoding="utf-8")
    return GenreCanon(allowed, mapping)


def test_the_pair_starts_in_step(canon: GenreCanon) -> None:
    assert len(canon._allowed) == len(canon._allowed_lower) == 3


def test_a_reload_after_the_file_disappears_returns_none_not_an_exception(
    canon: GenreCanon,
) -> None:
    """The finding. Before the fix this raised ValueError."""
    canon._allowed_path.unlink()
    canon.reload()

    assert len(canon._allowed) == len(canon._allowed_lower) == 0
    # NOT "rok": the map file still exists and still says `rok => Rock`, and
    # an explicit map hit is answered before the fuzzy path is reached. The
    # desync only shows on a genre the map has no opinion about -- which is
    # what makes it easy to miss.
    assert canon.resolve("jaz") is None
    assert canon.resolve("anything at all") is None


def test_the_pair_stays_in_step_when_the_file_shrinks(canon: GenreCanon) -> None:
    """The same desync, without the file vanishing entirely."""
    canon._allowed_path.write_text("Rock\n", encoding="utf-8")
    canon.reload()
    assert len(canon._allowed) == len(canon._allowed_lower) == 1
    assert canon.resolve("rock") == "Rock"


def test_the_pair_stays_in_step_when_the_file_grows(canon: GenreCanon) -> None:
    canon._allowed_path.write_text("Rock\nJazz\nBlues\nSoul\nFunk\n", encoding="utf-8")
    canon.reload()
    assert len(canon._allowed) == len(canon._allowed_lower) == 5


def test_repeated_reloads_do_not_accumulate(canon: GenreCanon) -> None:
    """Clearing one collection and appending to the other would grow it."""
    for _ in range(4):
        canon.reload()
    assert len(canon._allowed) == len(canon._allowed_lower) == 3


def test_a_canon_built_with_no_allowed_file_at_all_is_usable(tmp_path: Path) -> None:
    """The cold-start case: no opinion, not a crash."""
    c = GenreCanon(tmp_path / "missing.txt", tmp_path / "also_missing.txt")
    assert c._allowed_lower == []
    assert c.resolve("rock") is None


def test_the_map_separator_the_vault_actually_uses_still_loads(canon: GenreCanon) -> None:
    """The " => " form, whose absence once made all 51 rules dead code."""
    assert canon.resolve("rok") == "Rock"


def test_a_tab_separated_map_still_loads(tmp_path: Path) -> None:
    """The old documented shape must keep working for a hand-written file."""
    allowed = tmp_path / "Genre_Allowed.txt"
    allowed.write_text("Rock\n", encoding="utf-8")
    mapping = tmp_path / "Genre_Canonical_Map.txt"
    mapping.write_text("rok\tRock\n", encoding="utf-8")
    assert GenreCanon(allowed, mapping).resolve("rok") == "Rock"


def test_the_docstring_names_files_that_exist() -> None:
    """It named `genre_map.tsv` with a tab; the vault has never had one.

    A docstring that names the wrong file is how the tab-only split survived
    as dead code long enough for all 51 map rules to never load.
    """
    doc = Path(GenreCanon.__module__.replace(".", "/") + ".py")
    text = (Path(__file__).resolve().parents[1] / doc).read_text()
    head = text.split('"""')[1]

    # Only the lines that NAME the backing files, not the paragraph that
    # explains what the old names cost -- that paragraph necessarily quotes
    # them. A first draft asserted over the whole docstring and failed on
    # its own history note, which is the third time in this session a guard
    # searched raw text and could not tell code from commentary.
    named = [ln for ln in head.splitlines() if "MetaData/" in ln]
    assert named, "the docstring no longer names its backing files"
    assert any("Genre_Allowed.txt" in ln for ln in named), named
    assert any("Genre_Canonical_Map.txt" in ln for ln in named), named
    assert not any("genre_map.tsv" in ln or "genre_allowed.txt" in ln
                   for ln in named), named
