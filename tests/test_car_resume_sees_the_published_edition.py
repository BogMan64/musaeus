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


class TestItAgreesWithThePublisherAboutTheLayout:
    """F8, 2026-09-16: two functions encoded one layout rule differently.

    `publish_edition` writes `tail = rel[-3:]`. `_published_twin` kept
    everything after the BATCH layer. On the flat layout derive_output_path
    writes today those are the same three components, so nothing broke -- but
    one level deeper they diverge, the twin is never found, and the track
    re-encodes on every run for ever while the build reports success.

    The rule is "the last three components", in both places.
    """

    def test_a_deeper_path_lands_where_the_publisher_puts_it(self, published_root):
        """publish_edition would write <root>/Album/Disc 1/track.m4a -- the
        last three -- so that is where the twin must look."""
        staged = Path("/vault/RUNS/_output/encoded/BATCH_001/Artist/Album/Disc 1/track.m4a")
        assert B._published_twin(staged) == published_root / "Album" / "Disc 1" / "track.m4a"

    def test_the_batch_segment_is_matched_regardless_of_case(self, published_root):
        """publish_edition uppercases before testing; this did not. A case
        difference deciding whether nine hours of encoding is reused is not a
        distinction anyone meant to make."""
        for seg in ("BATCH_001", "batch_001", "Batch_001"):
            staged = Path(f"/vault/_output/encoded/{seg}/Blur/Parklife/x.m4a")
            assert B._published_twin(staged) == published_root / "Blur" / "Parklife" / "x.m4a", seg

    def test_the_two_agree_on_a_real_staged_path(self):
        """Asserted against publish_edition's actual source rather than a
        restatement of it, so the two cannot drift apart again quietly."""
        src = (Path(__file__).resolve().parents[1]
               / "scripts" / "car_library" / "build_car_library.py").read_text()
        assert "tail = rel[-3:]" in src, (
            "publish_edition's rule changed -- _published_twin must change with it"
        )

    def test_a_path_too_short_to_have_artist_album_file_is_no_opinion(self, published_root):
        assert B._published_twin(Path("/a/b.m4a")) is None


class TestAMaskingRunDoesNotInheritThePreviousEdition:
    """F4, 2026-09-16. The dangerous direction of the same skip.

    `_output_matches_source` compares duration, sample rate and channel count.
    The noise masker preserves all three on purpose -- `amix=...:duration=
    first`, `-ar <src_rate>`, no `-ac` -- so a published MASKED file and a
    published UNMASKED one are indistinguishable to it. Bitrate and target
    LUFS are not checked either.

    On a masking run that makes the skip a confident wrong answer: every
    track returns "already published" before anything reaches encoded_dir,
    the masker runs against an empty tree, publish moves nothing, and the DB
    loop still matches every row through the published index and records
    noise_profile='dual'. The edition stays unmasked, the catalogue says
    otherwise, and the build reports success.

    Re-encoding is expensive. Publishing a lie is worse.
    """

    def test_the_wrapper_withholds_the_published_root_when_masking(self):
        src = (Path(__file__).resolve().parents[1]
               / "scripts" / "car_library" / "build_car_library.py").read_text()
        assert "if not apply_masking:" in src, (
            "the published-twin skip must be conditional on masking"
        )
        guard = src.index("if not apply_masking:")
        assign = src.index('env["MUSAEUS_PUBLISHED_ROOT"]')
        assert guard < assign, "the guard must come before the assignment it guards"

    def test_apply_masking_is_decided_before_the_environment_is_built(self):
        """The dest_root lesson, applied to the variable this now depends on:
        a name used before it is assigned is a NameError waiting for the next
        real run, and a nine-hour job is a bad place to find one."""
        src = (Path(__file__).resolve().parents[1]
               / "scripts" / "car_library" / "build_car_library.py").read_text()
        decided = min(src.index("apply_masking = True"), src.index("apply_masking = False"))
        used = src.index("if not apply_masking:")
        assert decided < used, "apply_masking must be decided before it is read"

    def test_the_skip_is_explicitly_cleared_not_merely_left_unset(self):
        """env is a copy of os.environ, so a stale MUSAEUS_PUBLISHED_ROOT in
        the caller's shell would otherwise leak in and re-enable the very
        skip this turns off."""
        src = (Path(__file__).resolve().parents[1]
               / "scripts" / "car_library" / "build_car_library.py").read_text()
        assert 'env.pop("MUSAEUS_PUBLISHED_ROOT", None)' in src
