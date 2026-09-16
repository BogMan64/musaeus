"""CAR artist folders file under the sort form, as the other two tiers do.

Grey's rule, in his words: "really it's just the Artist folders I need the
article at the end. We can leave the article at the start. What I wish to
avoid is having hundreds of artist folders that start with The."

Three fields, three jobs -- the tag is natural form ("The Beatles") so
MusicBrainz can read it, and the FOLDER is sort form ("Beatles, The") so the
library browses under B. The 2026-09-16 article migration made the tag
natural, and the CAR encoder derives its folder from the tag, so CAR started
producing "The Beatles/" while ALAC-Archival and ALAC_Library had 0 such
folders between them. Measured before this fix: 84 CAR folders led with an
article, 63 of them "The ".

Two things here are easy to get wrong and expensive:

  MERGING    publish_edition merges into CAR_Library rather than clearing it,
             so an older "Beatles, The" can already exist beside a new "The
             Beatles". A blind rename raises, and a half-done one leaves one
             artist filed in two places -- the split-artist failure the
             filing rules exist to prevent.

  ORDERING   the rename must happen BEFORE car_export_path is recorded. The
             other way round, every row names a folder that no longer exists.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts" / "car_library"))

import build_car_library as B  # noqa: E402


def _track(root: Path, artist: str, album: str, name: str, data: bytes = b"\0") -> Path:
    p = root / artist / album / name
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(data)
    return p


class TestItFilesUnderTheSortForm:
    def test_a_leading_the_is_moved_to_the_end(self, tmp_path):
        _track(tmp_path, "The Beatles", "Revolver", "a.m4a")
        B._sort_artist_folders(tmp_path)
        assert (tmp_path / "Beatles, The" / "Revolver" / "a.m4a").is_file()
        assert not (tmp_path / "The Beatles").exists()

    @pytest.mark.parametrize(
        "given,wanted",
        [("The Who", "Who, The"), ("Los Bravos", "Bravos, Los"), ("El Gran Combo", "Gran Combo, El")],
    )
    def test_non_english_articles_move_too(self, tmp_path, given, wanted):
        """The library holds Spanish, French, Dutch and German names and the
        rule is about browsing, which does not care which language."""
        _track(tmp_path, given, "Alb", "t.m4a")
        B._sort_artist_folders(tmp_path)
        assert (tmp_path / wanted).is_dir(), f"{given!r} should file as {wanted!r}"

    def test_an_artist_with_no_article_is_untouched(self, tmp_path):
        _track(tmp_path, "Blur", "Parklife", "t.m4a")
        B._sort_artist_folders(tmp_path)
        assert (tmp_path / "Blur" / "Parklife" / "t.m4a").is_file()

    def test_a_protected_name_is_not_split(self, tmp_path):
        """De La Soul has been broken into "La Soul, De" three times. This
        reads the same transforms the migration used, so if the protection
        ever regresses it shows up here rather than in the library."""
        for name in ("De La Soul", "Los Lobos"):
            _track(tmp_path, name, "Alb", "t.m4a")
        B._sort_artist_folders(tmp_path)
        assert (tmp_path / "De La Soul").is_dir()
        assert (tmp_path / "Los Lobos").is_dir()

    def test_it_is_idempotent(self, tmp_path):
        _track(tmp_path, "The Beatles", "Revolver", "a.m4a")
        B._sort_artist_folders(tmp_path)
        assert B._sort_artist_folders(tmp_path) == 0


class TestMergingOntoAnExistingTwin:
    def test_contents_move_into_the_folder_that_is_already_there(self, tmp_path):
        _track(tmp_path, "Beatles, The", "Revolver", "old.m4a")
        _track(tmp_path, "The Beatles", "Revolver", "new.m4a")
        B._sort_artist_folders(tmp_path)
        d = tmp_path / "Beatles, The" / "Revolver"
        assert (d / "old.m4a").is_file() and (d / "new.m4a").is_file()
        assert not (tmp_path / "The Beatles").exists()

    def test_the_same_encode_published_twice_is_not_duplicated(self, tmp_path):
        _track(tmp_path, "Beatles, The", "Revolver", "a.m4a", b"xxxx")
        _track(tmp_path, "The Beatles", "Revolver", "a.m4a", b"xxxx")
        B._sort_artist_folders(tmp_path)
        assert (tmp_path / "Beatles, The" / "Revolver" / "a.m4a").read_bytes() == b"xxxx"
        assert not (tmp_path / "The Beatles").exists()

    def test_two_different_files_with_one_name_are_left_for_a_person(self, tmp_path):
        """Silently overwriting one edition with another is the kind of
        quiet loss that is only noticed months later."""
        _track(tmp_path, "Beatles, The", "Revolver", "a.m4a", b"original")
        _track(tmp_path, "The Beatles", "Revolver", "a.m4a", b"different-bytes")
        B._sort_artist_folders(tmp_path)
        assert (tmp_path / "Beatles, The" / "Revolver" / "a.m4a").read_bytes() == b"original"
        assert (tmp_path / "The Beatles" / "Revolver" / "a.m4a").is_file()


class TestItNeverFailsTheBuild:
    def test_a_missing_musaeus_import_is_a_no_op_not_a_crash(self, tmp_path, monkeypatch):
        """Nine hours of encoding must not be lost to a folder rename."""
        monkeypatch.setitem(sys.modules, "musaeus.artist_form", None)
        _track(tmp_path, "The Beatles", "Revolver", "a.m4a")
        assert B._sort_artist_folders(tmp_path) == 0

    def test_an_empty_tree_is_fine(self, tmp_path):
        assert B._sort_artist_folders(tmp_path) == 0


class TestItRunsBeforeThePathsAreRecorded:
    def test_the_rename_precedes_the_output_index(self):
        """Ordering, asserted on the source: recording car_export_path first
        would name folders that the rename then moves, turning every row into
        a phantom."""
        src = (Path(__file__).resolve().parents[1]
               / "scripts" / "car_library" / "build_car_library.py").read_text()
        call = src.index("_sort_artist_folders(final_dir)")
        index = src.index("output_index = _index_output_by_tags(final_dir)")
        assert call < index, "the folders must be filed before their paths are recorded"
