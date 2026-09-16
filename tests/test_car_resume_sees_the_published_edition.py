""""Already encoded?" must be asked of where the edition LIVES.

Measured on the 2026-09-15 car build, and it went wrong in both directions.

The encoder's resume check looked only at the STAGING tree
(`RUNS/AAC-Car-Masked/_output/encoded/BATCH_nnn/...`). But
`build_car_library.publish_edition` MOVES the finished tree into
`Libraries/CAR_Library` and drops the BATCH layer, so staging is empty once a
publish succeeds.

    too little:  a later run finds no staged output and re-encodes the whole
                 library -- nine hours reproducing files already correct on
                 disk, which is the exact waste the resume check exists to
                 prevent.

    too much:    a run started while a PREVIOUS staging tree still existed
                 reported "SKIP DONE | already encoded" for 819 tracks, then
                 published a tree that did not contain them. The skip was true
                 of a directory about to be emptied. Those 819 ended with no
                 CAR file and no car_export_path, and NOTHING reported a
                 failure -- the build said it succeeded.

The second is the dangerous one: a confident wrong answer with no error.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts" / "car_library" / "vendor"))

import build_aac_library as B  # noqa: E402


@pytest.fixture
def published_root(tmp_path, monkeypatch):
    root = tmp_path / "CAR_Library"
    root.mkdir()
    monkeypatch.setenv("MUSAEUS_PUBLISHED_ROOT", str(root))
    return root


class TestItFindsThePublishedTwin:
    def test_the_batch_layer_is_dropped(self, published_root):
        staged = Path("/vault/RUNS/AAC-Car-Masked/_output/encoded/BATCH_001/Blur/Parklife/x.m4a")
        assert B._published_twin(staged) == published_root / "Blur" / "Parklife" / "x.m4a"

    def test_a_later_batch_number_maps_the_same_way(self, published_root):
        staged = Path("/vault/RUNS/_output/encoded/BATCH_017/A/B/c.m4a")
        assert B._published_twin(staged) == published_root / "A" / "B" / "c.m4a"

    def test_with_no_batch_layer_it_falls_back_to_artist_album_file(self, published_root):
        """Keeps working against a layout this script did not write."""
        staged = Path("/somewhere/Artist/Album/song.m4a")
        assert B._published_twin(staged) == published_root / "Artist" / "Album" / "song.m4a"


class TestItStaysStandalone:
    def test_no_published_root_configured_means_no_opinion(self, monkeypatch):
        """The vendored encoder must still run outside MUSAEUS, where nothing
        sets this variable. Returning None leaves the old staging-only
        behaviour exactly as it was."""
        monkeypatch.delenv("MUSAEUS_PUBLISHED_ROOT", raising=False)
        staged = Path("/vault/_output/encoded/BATCH_001/A/B/c.m4a")
        assert B._published_twin(staged) is None

    def test_an_empty_published_root_is_treated_as_unset(self, monkeypatch):
        monkeypatch.setenv("MUSAEUS_PUBLISHED_ROOT", "")
        assert B._published_twin(Path("/x/BATCH_001/A/B/c.m4a")) is None


class TestTheWrapperTellsTheEncoderWhereToLook:
    def test_the_wrapper_sets_the_variable(self):
        """A unit test on _published_twin cannot catch the wrapper failing to
        pass the root -- which is the half that actually broke the build."""
        src = (Path(__file__).resolve().parents[1]
               / "scripts" / "car_library" / "build_car_library.py").read_text()
        assert 'env["MUSAEUS_PUBLISHED_ROOT"]' in src

    def test_the_wrapper_does_not_use_dest_root_before_it_exists(self):
        """dest_root is assigned AFTER the encode step. Referencing it where
        the environment is built was a NameError waiting for the next run;
        caught before it fired, and pinned so it cannot come back."""
        src = (Path(__file__).resolve().parents[1]
               / "scripts" / "car_library" / "build_car_library.py").read_text()
        env_line = src.index('env["MUSAEUS_PUBLISHED_ROOT"]')
        assign = src.index("dest_root = cfg.iphone_library")
        assert env_line < assign, "test premise: the env line comes first"
        window = src[env_line:env_line + 400]
        assert "str(dest_root)" not in window, (
            "the environment must not read dest_root before it is assigned"
        )
