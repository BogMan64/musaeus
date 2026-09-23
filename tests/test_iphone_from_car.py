"""The iPhone edition can be COPIED from the car edition, unless it is masked.

Grey, 2026-09-16: "can Musaeus ask the enduser which library it wishes to use?
If we were doing it today i'd pick car library."

The two specs are byte-identical -- EditionSpec("car") and EditionSpec
("iphone") are both aac / -14.0 LUFS / 256 kbps / 48 kHz -- so re-encoding
from the masters spends hours producing the same files a second time.

"No edition is ever built from another" exists to stop an edition being BAKED
from another: building the car from the -18 LUFS lossless applies loudnorm
twice and is measurably worse. A byte copy of an identical spec is not a
second bake.

THE GUARD. Masking exists to sit under road noise in the Sebring. On
headphones it is noise mixed into the music, which is why the iPhone edition
turns masking off. A masked car edition must therefore never be copied, and
the check reads the catalogue's own noise_profile rather than trusting that
someone remembered to pass a flag.
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts" / "car_library"))

import build_car_library as B  # noqa: E402


@pytest.fixture
def setup(tmp_path):
    car = tmp_path / "CAR_Library"
    iph = tmp_path / "iPHONE_Library"
    car.mkdir()
    iph.mkdir()
    conn = sqlite3.connect(":memory:")
    conn.execute(
        "CREATE TABLE archive (status TEXT, car_export_path TEXT, noise_profile TEXT, genre TEXT)"
    )
    cfg = type("C", (), {"car_library": car, "iphone_library": iph})()
    return cfg, conn, car, iph


def _track(
    conn, car: Path, name: str, size: int = 1000, profile: str = "clean", genre: str = "Rock"
) -> Path:
    p = car / name
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(b"\0" * size)
    conn.execute("INSERT INTO archive VALUES ('CATALOGUED', ?, ?, ?)", (str(p), profile, genre))
    conn.commit()
    return p


class TestItCopiesAnUnmaskedEdition:
    def test_every_track_arrives(self, setup):
        cfg, conn, car, iph = setup
        _track(conn, car, "A/Al/one.m4a")
        _track(conn, car, "B/Bl/two.m4a")
        rc = B._copy_edition_from_car(cfg, conn, iph, None, dry_run=False)
        assert rc == 0
        assert (iph / "A" / "Al" / "one.m4a").is_file()
        assert (iph / "B" / "Bl" / "two.m4a").is_file()

    def test_the_tree_shape_is_preserved(self, setup):
        cfg, conn, car, iph = setup
        _track(conn, car, "Artist/Album/t.m4a")
        B._copy_edition_from_car(cfg, conn, iph, None, dry_run=False)
        assert (iph / "Artist" / "Album" / "t.m4a").is_file()

    def test_a_dry_run_writes_nothing(self, setup):
        cfg, conn, car, iph = setup
        _track(conn, car, "A/Al/one.m4a")
        B._copy_edition_from_car(cfg, conn, iph, None, dry_run=True)
        assert not any(iph.rglob("*.m4a"))

    def test_rerunning_does_not_recopy_identical_files(self, setup):
        cfg, conn, car, iph = setup
        _track(conn, car, "A/Al/one.m4a")
        B._copy_edition_from_car(cfg, conn, iph, None, dry_run=False)
        before = (iph / "A" / "Al" / "one.m4a").stat().st_mtime_ns
        B._copy_edition_from_car(cfg, conn, iph, None, dry_run=False)
        assert (iph / "A" / "Al" / "one.m4a").stat().st_mtime_ns == before


class TestItRefusesAMaskedEdition:
    def test_one_masked_track_stops_the_whole_copy(self, setup):
        """Masking is for road noise. On headphones it is noise mixed into the
        music, and the iPhone spec turns it off for that reason."""
        cfg, conn, car, iph = setup
        _track(conn, car, "A/Al/clean.m4a")
        _track(conn, car, "A/Al/masked.m4a", profile="dual")
        rc = B._copy_edition_from_car(cfg, conn, iph, None, dry_run=False)
        assert rc == 1
        assert not any(iph.rglob("*.m4a")), "nothing may be copied once it refuses"

    def test_an_unset_noise_profile_is_not_treated_as_masked(self, setup):
        """Empty means 'never recorded', not 'masked'. Refusing on it would
        make the feature unusable on a catalogue that predates the column."""
        cfg, conn, car, iph = setup
        _track(conn, car, "A/Al/one.m4a", profile="")
        assert B._copy_edition_from_car(cfg, conn, iph, None, dry_run=False) == 0


class TestTheBudgetUsesGenrePriority:
    def test_a_low_priority_genre_is_dropped_first(self, setup):
        """DEFAULT_GENRE_PRIORITY says which music goes first when there is
        not room for all of it. A copy that filled in catalogue order would
        put a different answer on the iPhone than an encode of the same size.
        """
        cfg, conn, car, iph = setup
        _track(conn, car, "z/classical.m4a", size=1000, genre="Classical")
        _track(conn, car, "a/rock.m4a", size=1000, genre="Rock")
        B._copy_edition_from_car(cfg, conn, iph, 1000, dry_run=False)
        assert (iph / "a" / "rock.m4a").is_file(), "Rock outranks Classical"
        assert not (iph / "z" / "classical.m4a").exists()

    def test_no_budget_copies_everything(self, setup):
        cfg, conn, car, iph = setup
        _track(conn, car, "a/rock.m4a", genre="Rock")
        _track(conn, car, "z/classical.m4a", genre="Classical")
        B._copy_edition_from_car(cfg, conn, iph, None, dry_run=False)
        assert len(list(iph.rglob("*.m4a"))) == 2


class TestTheMaskerCannotClip:
    def test_the_filter_chain_ends_in_a_limiter(self):
        """amix with normalize=0 does not reduce gain -- that is what keeps the
        music at the level the encoder baked -- so the noise adds on top and
        the sum can exceed 0 dBFS. Measured on the live edition: masking costs
        ~0.6 dB, and 19% of a 120-file sample peaked above -0.6 dBFS, the
        loudest at exactly 0.0. Unlimited, masking would distort ~2,100
        tracks."""
        src = (
            Path(__file__).resolve().parents[1]
            / "scripts"
            / "car_library"
            / "vendor"
            / "orpheus_noise_masker.py"
        ).read_text()
        assert "CEILING_LINEAR" in src
        # The FILTER STRING, not the prose around it. The first draft of this
        # test used src.index("alimiter"), which found the explanatory comment
        # above the filter and compared two pieces of documentation.
        start = src.index("filt = (")
        filt = src[start : src.index(")", src.index("[out]", start))]
        assert "alimiter" in filt, "the masked output has no peak ceiling"
        assert filt.index("amix=inputs=2") < filt.index("alimiter"), (
            "the limiter must come after the mix, not before"
        )
        assert "[mixed]" in filt, "the mix must be named so the limiter can take it"

    def test_the_ceiling_is_below_full_scale(self):
        sys.path.insert(
            0, str(Path(__file__).resolve().parents[1] / "scripts" / "car_library" / "vendor")
        )
        import orpheus_noise_masker as M

        assert 0 < M.CEILING_LINEAR < 1.0
