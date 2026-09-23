"""An edition is built from the masters, never from another edition.

The rule is old (scope, "Editions": *"Masters are never baked. Each edition
bakes exactly once, from the masters. No edition is ever built from
another."*). It was being broken silently.

A baked row's `file_path` follows the *edition*, so once the LUFS bake has
run it names the -18 LUFS copy under ALAC_Library. `stage_from_catalogue`
symlinked exactly that while its own docstring said it staged "every
CATALOGUED master". Measured on the live vault 2026-09-14: 8,734 of 11,555
rows pointed at the library, so three-quarters of the car edition would have
been encoded out of the lossless edition.

Grey ruled 2026-09-14: build from the masters.

The bake maps master -> library by a plain relative-path swap
(build_alac_library.py::_library_path_for), so the inverse is the same swap
back. Verified against the live vault the same day: all 8,734 baked rows had
their master present at the mirrored path, none missing.
"""

from __future__ import annotations

import sys as _sys
from pathlib import Path

import pytest

_sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts" / "car_library"))

from musaeus.editions import master_path_for


@pytest.fixture
def trees(tmp_path):
    lib = tmp_path / "Libraries" / "ALAC_Library"
    arc = tmp_path / "Libraries" / "ALAC-Archival"
    for root in (lib, arc):
        (root / "Artist" / "Album").mkdir(parents=True)
    return lib, arc


def _touch(p: Path) -> Path:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(b"\0")
    return p


class TestItResolvesToTheMaster:
    def test_a_baked_row_resolves_to_its_master(self, trees):
        lib, arc = trees
        baked = _touch(lib / "Artist" / "Album" / "t.m4a")
        master = _touch(arc / "Artist" / "Album" / "t.m4a")

        res = master_path_for(baked, lib, arc)
        assert res.path == master
        assert res.is_master
        assert res.path != baked, "the -18 library copy must not be the source"

    def test_a_row_already_in_the_archive_is_its_own_master(self, trees):
        lib, arc = trees
        master = _touch(arc / "Artist" / "Album" / "u.m4a")
        res = master_path_for(master, lib, arc)
        assert res.path == master
        assert res.is_master

    def test_the_relative_shape_is_preserved(self, trees):
        """The mapping is a path swap, not a filename search -- two artists
        can hold the same track name."""
        lib, arc = trees
        _touch(arc / "A1" / "Al" / "same.m4a")
        _touch(arc / "A2" / "Al" / "same.m4a")
        res = master_path_for(lib / "A2" / "Al" / "same.m4a", lib, arc)
        assert res.path == arc / "A2" / "Al" / "same.m4a"


class TestTheFallbackIsVisible:
    def test_a_missing_master_falls_back_but_says_so(self, trees):
        """Encoding the edition copy beats skipping the track -- but it IS
        the thing the rule forbids, so it must be reported, not absorbed."""
        lib, arc = trees
        baked = _touch(lib / "Artist" / "Album" / "orphan.m4a")
        # no master written

        res = master_path_for(baked, lib, arc)
        assert res.path == baked
        assert res.is_master is False

    def test_a_path_outside_both_trees_is_left_alone(self, trees):
        lib, arc = trees
        stray = Path("/somewhere/else/x.m4a")
        res = master_path_for(stray, lib, arc)
        assert res.path == stray
        assert res.is_master

    def test_a_directory_at_the_master_path_is_not_mistaken_for_a_master(self, trees):
        lib, arc = trees
        baked = _touch(lib / "Artist" / "Album" / "d.m4a")
        (arc / "Artist" / "Album" / "d.m4a").mkdir(parents=True)
        res = master_path_for(baked, lib, arc)
        assert res.is_master is False, "is_file(), not exists()"
        assert res.path == baked


class TestItAcceptsWhatTheCatalogueStores:
    def test_a_string_file_path_works(self, trees):
        """archive.file_path is TEXT; callers pass it straight through."""
        lib, arc = trees
        _touch(lib / "Artist" / "Album" / "s.m4a")
        master = _touch(arc / "Artist" / "Album" / "s.m4a")
        res = master_path_for(str(lib / "Artist" / "Album" / "s.m4a"), lib, arc)
        assert res.path == master
        assert res.is_master


class TestPublishingOnlyTakesTheEdition:
    """The encoder's output tree is not all music.

    Beside its BATCH_nnn output it keeps ORPHEUS/Acoustic Treatment/ -- its
    own copies of the white/pink/brown noise beds used for masking. Those are
    working assets. Publishing "every .m4a under the output dir" filed all six
    into CAR_Library on 2026-09-14, 414 MB of noise sitting in the library as
    though it were an album. The originals in RUNS/Noise were untouched so
    nothing was lost, but an edition must hold the library and nothing else.
    """

    def _tree(self, tmp_path):
        out = tmp_path / "_output" / "encoded"
        (out / "BATCH_001" / "Artist" / "Album").mkdir(parents=True)
        (out / "BATCH_001" / "Artist" / "Album" / "song.m4a").write_bytes(b"\0")
        (out / "ORPHEUS" / "Acoustic Treatment").mkdir(parents=True)
        (out / "ORPHEUS" / "Acoustic Treatment" / "Brown_Noise_30min.m4a").write_bytes(b"\0")
        return out

    def test_batch_output_is_published(self, tmp_path):
        from build_car_library import publish_edition

        out = self._tree(tmp_path)
        dest = tmp_path / "CAR_Library"
        publish_edition(out, dest)
        assert (dest / "Artist" / "Album" / "song.m4a").is_file()

    def test_the_noise_beds_are_not_published(self, tmp_path):
        from build_car_library import publish_edition

        out = self._tree(tmp_path)
        dest = tmp_path / "CAR_Library"
        publish_edition(out, dest)
        assert not list(dest.rglob("Brown_Noise_30min.m4a")), (
            "a working asset must never be filed as music"
        )
        # and it stays where the encoder left it
        assert (out / "ORPHEUS" / "Acoustic Treatment" / "Brown_Noise_30min.m4a").is_file()

    def test_the_batch_layer_is_dropped(self, tmp_path):
        from build_car_library import publish_edition

        out = self._tree(tmp_path)
        dest = tmp_path / "CAR_Library"
        publish_edition(out, dest)
        assert not (dest / "BATCH_001").exists(), "the edition mirrors Artist/Album"
