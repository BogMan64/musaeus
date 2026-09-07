"""
Tests for SanitizeStage's metadata/path split.
"""

from __future__ import annotations

from musaeus.stages.sanitize import needs_sanitization, sanitize_value


class TestMetadataVsPathSplit:
    """Sanitize must not apply PATH rules to METADATA.

    Grey's call, 2026-08-22: match MusicBrainz exactly in the tag, and
    sanitize only when a path is actually built. MB has no house rule --
    it records each artist's own styling ("Simon & Garfunkel", "Peter,
    Paul and Mary", "AC/DC") -- so inheriting it is only possible if this
    stage stops overwriting it.

    The cost of the old behaviour: "AC/DC" became the stored artist
    "Ac-dc" on 92 files (Normalize then title-cased what it no longer
    recognised), and "R&B/Funk/Soul" became "R&B-Funk-Soul" on 916,
    inventing a genre matching no canon. Neither protected anything --
    every path is built through sanitize_path_component() regardless.
    """

    def test_slash_survives_in_metadata(self):
        assert sanitize_value("AC/DC") == "AC/DC"
        assert sanitize_value("R&B/Funk/Soul") == "R&B/Funk/Soul"

    def test_slash_is_still_removed_from_a_path(self):
        from musaeus.stages.organize import sanitize_path_component

        assert sanitize_path_component("AC/DC") == "AC-DC"

    def test_path_uses_hyphen_not_underscore(self):
        """The library on disk already holds "AC-DC" folders.

        Switching the replacement to "_" would rename ~90 directories for
        no gain and invalidate every stored file_path pointing at them.
        """
        from musaeus.stages.organize import sanitize_path_component

        assert "_" not in sanitize_path_component("AC/DC")

    def test_ampersand_and_comma_are_never_touched(self):
        """MusicBrainz spellings must round-trip unchanged."""
        for name in ("Simon & Garfunkel", "Peter, Paul and Mary", "Earth, Wind & Fire"):
            assert sanitize_value(name) == name
            assert not needs_sanitization(name)

    def test_control_characters_are_still_stripped(self):
        """Narrowing the scope must not stop it doing its real job."""
        assert needs_sanitization("Bad\x01Name") is True
        assert sanitize_value("Bad\x01Name") == "BadName"

    def test_smart_quotes_and_trailing_dots_still_fixed(self):
        assert sanitize_value("Smart’quote") == "Smart'quote"
        assert sanitize_value("Trailing. ") == "Trailing"
