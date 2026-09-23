"""Filing names: which folder an artist's tracks land in.

The whole feature exists because no rule works. A pattern that correctly
turns `Bill Haley & His Comets` into `Bill Haley` also turns `Kool & The
Gang` into `Kool`, and nothing in the string distinguishes a backing
credit from a band name. So this is a hand-ruled list, and the tests here
are mostly about the ways a hand-edited file goes wrong.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from musaeus.filing import FILENAME, FilingError, check, folder_for, load


def write(meta: Path, body: str) -> Path:
    meta.mkdir(parents=True, exist_ok=True)
    (meta / FILENAME).write_text(body, encoding="utf-8")
    return meta


class TestLoad:
    def test_a_missing_file_is_an_empty_map_not_an_error(self, tmp_path):
        """No file means no artist has a special folder. That is the correct
        starting state, and it is what every caller falls back to anyway."""
        assert load(tmp_path / "MetaData") == {}

    def test_tab_separated_pairs(self, tmp_path):
        meta = write(
            tmp_path / "MetaData",
            "Gladys Knight & The Pips\tGladys Knight\nTom Petty And The Heartbreakers\tTom Petty\n",
        )
        assert load(meta) == {
            "Gladys Knight & The Pips": "Gladys Knight",
            "Tom Petty And The Heartbreakers": "Tom Petty",
        }

    def test_comments_and_blank_lines_are_ignored(self, tmp_path):
        meta = write(tmp_path / "MetaData", "# a comment\n\n   \nA\tB\n  # indented comment\n")
        assert load(meta) == {"A": "B"}

    def test_a_space_separated_line_is_refused_not_guessed(self, tmp_path):
        """The dangerous one. Artist names contain spaces, so splitting on
        whitespace would file `Gladys Knight & The Pips` under `Gladys`
        while looking entirely successful."""
        meta = write(tmp_path / "MetaData", "Gladys Knight & The Pips Gladys Knight\n")
        with pytest.raises(FilingError, match="no tab"):
            load(meta)

    @pytest.mark.parametrize("body", ["A\t\n", "\tB\n", "A\t   \n", "   \tB\n"])
    def test_an_empty_side_is_refused(self, tmp_path, body):
        """One real name and one blank is a half-written line, and filing an
        artist under "" would put their tracks at the library root."""
        meta = write(tmp_path / "MetaData", body)
        with pytest.raises(FilingError, match="non-empty"):
            load(meta)

    def test_a_whitespace_only_line_is_blank_not_an_error(self, tmp_path):
        """`   \t   ` carries a tab but no content. Treating it as a blank
        line is right -- it is what a stray keystroke leaves behind, and
        refusing to load the whole authority file over it would be worse
        than ignoring it."""
        meta = write(tmp_path / "MetaData", "A\tB\n   \t   \n")
        assert load(meta) == {"A": "B"}

    def test_a_contradictory_duplicate_is_refused_not_last_one_wins(self, tmp_path):
        """Two answers for one artist is an ambiguous file. Silently taking
        the last would make the folder depend on line order."""
        meta = write(tmp_path / "MetaData", "A\tB\nA\tC\n")
        with pytest.raises(FilingError, match="already filed"):
            load(meta)

    def test_a_harmless_exact_duplicate_is_allowed(self, tmp_path):
        meta = write(tmp_path / "MetaData", "A\tB\nA\tB\n")
        assert load(meta) == {"A": "B"}

    def test_a_folder_name_may_contain_an_ampersand(self, tmp_path):
        """`Earth, Wind & Fire` files under its own full name. The feature
        must not assume the folder is always the shorter string."""
        meta = write(
            tmp_path / "MetaData", "Earth, Wind & Fire, The Emotions\tEarth, Wind & Fire\n"
        )
        assert load(meta)["Earth, Wind & Fire, The Emotions"] == "Earth, Wind & Fire"


class TestFolderFor:
    def test_an_unlisted_artist_is_filed_under_their_own_tag(self):
        assert folder_for("Kool & The Gang", {}) == "Kool & The Gang"

    def test_a_listed_artist_is_filed_under_the_ruling(self):
        assert (
            folder_for("Gladys Knight & The Pips", {"Gladys Knight & The Pips": "Gladys Knight"})
            == "Gladys Knight"
        )

    def test_a_missing_artist_is_named_not_blank(self):
        """A blank folder component would put tracks at the library root."""
        for empty in (None, ""):
            assert folder_for(empty, {}) == "Unknown Artist"

    def test_filing_does_not_follow_chains(self):
        """A -> B -> C files under B, not C. Stated as a test because the
        opposite is a reasonable expectation, and check() reports it."""
        m = {"A": "B", "B": "C"}
        assert folder_for("A", m) == "B"


class TestCheck:
    def test_a_clean_map_reports_an_empty_list(self):
        """Empty list means checked and found nothing -- which a caller must
        be able to tell apart from never having looked."""
        assert check({"Gladys Knight & The Pips": "Gladys Knight"}) == []

    def test_a_self_mapping_is_reported_as_pointless(self):
        assert any("no effect" in p for p in check({"A": "A"}))

    def test_a_chain_is_reported(self):
        problems = check({"A": "B", "B": "C"})
        assert any("does not follow chains" in p for p in problems)

    def test_a_key_that_artist_canon_would_rewrite_is_reported(self):
        """The cross-authority collision. artist_canon expands `KC` to
        `KC & The Sunshine Band` BEFORE filing runs, so a filing line keyed
        on `KC` never fires -- and would look correct in the file forever."""
        problems = check({"KC": "KC"}, canon={"KC": "KC & The Sunshine Band"})
        assert any("never applies" in p for p in problems)

    def test_a_canon_entry_that_maps_to_itself_is_not_a_collision(self):
        assert check({"A": "B"}, canon={"A": "A"}) == []
