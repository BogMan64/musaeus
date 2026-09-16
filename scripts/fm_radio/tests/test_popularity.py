"""The per-artist popularity floor.

The bug being fixed: an absolute floor of 100 listens silenced obscure artists
entirely, and reported them identically to a ListenBrainz outage. The risk being
guarded against: a relative floor is capable of being worse, because it can
filter out the very song it was asked about.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fmradio.popularity import (  # noqa: E402
    ABSOLUTE_NOISE_FLOOR,
    DEFAULT_QUANTILE,
    PEAK_FRACTION,
    apply_floor,
    artist_floor,
    quantile,
)


class TestQuantile:
    def test_empty_is_zero_not_an_exception(self):
        """A reporting tool must not crash on an artist with no counts."""
        assert quantile([], 0.9) == 0.0

    def test_single_value(self):
        assert quantile([42], 0.9) == 42.0

    def test_interpolates(self):
        assert quantile([0, 10], 0.5) == 5.0

    def test_top_decile_of_a_flat_run(self):
        """1..100 at q=0.9. Position is (n-1)*q = 89.1, so the result sits a
        tenth of the way from the 90th value to the 91st: 90.1, not 91."""
        assert quantile(list(range(1, 101)), 0.9) == pytest.approx(90.1)

    def test_clamps_out_of_range_q(self):
        xs = [1, 2, 3]
        assert quantile(xs, 1.5) == 3.0
        assert quantile(xs, -1.0) == 1.0


class TestArtistFloor:
    def test_an_obscure_artist_is_no_longer_silenced(self):
        """THE BUG. Barney Bentall's best recording at 40 listens returned
        nothing under the old absolute floor of 100, and the run reported
        'no ListenBrainz signal' -- the same words an outage produces."""
        counts = [40, 30, 20, 10, 5]
        f = artist_floor(counts)
        assert f.value <= 40, "the artist's best recording must survive its own floor"
        kept, waived = apply_floor(counts, f, lambda c: c)
        assert kept, "an obscure artist must still yield candidates"
        assert not waived

    def test_a_huge_artist_still_sheds_the_bootleg_tail(self):
        """The Beatles: 9,057 records, 2,249 of them with exactly one listen.
        The floor must still bury those."""
        counts = [1] * 2249 + [500] * 100 + [2_093_966]
        f = artist_floor(counts)
        assert f.value > 1
        kept, waived = apply_floor(counts, f, lambda c: c)
        assert not waived
        assert len(kept) < len(counts) / 2
        assert 1 not in kept

    def test_a_massive_junk_tail_does_not_invert_the_floor(self):
        """REGRESSION. A rank-based quantile alone breaks on exactly the
        distribution it was added for. With 2,249 one-listen records against two
        millionsellers, the 90th percentile BY RANK is 1, so the floor came out
        at 5 -- LOWER than the floor for an obscure artist whose best track had
        40 listens. The statistic inverted, and the noisiest artist in the
        library got the most permissive filter.

        Fixed by taking the higher of the quantile and a fraction of the
        artist's peak, which cannot be dragged down by tail volume."""
        beatles = artist_floor([2_093_966, 2_080_106] + [1] * 2249)
        obscure = artist_floor([40, 30, 20, 12])
        assert beatles.value > obscure.value
        # int() truncates, so compare against the truncated expectation.
        assert beatles.value == int(2_093_966 * PEAK_FRACTION)
        kept, waived = apply_floor(
            [2_093_966, 2_080_106] + [1] * 2249, beatles, lambda c: c
        )
        assert kept == [2_093_966, 2_080_106], "the junk tail must be gone"
        assert not waived

    def test_floor_scales_with_the_artist(self):
        small = artist_floor([40, 30, 20, 10])
        large = artist_floor([400_000, 300_000, 200_000, 100_000])
        assert small.value < large.value

    def test_no_counts_means_no_floor(self):
        f = artist_floor([])
        assert f.value == 0
        assert f.n_counts == 0
        kept, waived = apply_floor([{"x": 1}], f, lambda r: None)
        assert kept and not waived

    def test_catalogue_entirely_under_the_noise_floor_keeps_its_best(self):
        """Every count below ABSOLUTE_NOISE_FLOOR. Taking max(noise_floor, ...)
        would cut the whole artist; capping at their best count does not."""
        counts = [4, 3, 2, 1]
        assert max(counts) < ABSOLUTE_NOISE_FLOOR
        f = artist_floor(counts)
        kept, _ = apply_floor(counts, f, lambda c: c)
        assert kept, "an artist must never be filtered down to nothing"
        assert 4 in kept

    def test_describe_explains_the_number(self):
        f = artist_floor([100, 200, 300])
        text = f.describe()
        assert "floor" in text and "top" in text
        assert artist_floor([]).describe().startswith("no listen counts")


class TestApplyFloorNeverEmpties:
    def test_an_album_cut_below_the_decile_is_kept_and_flagged(self):
        """THE RISK IN THE FIX. Ask about a deep cut and the artist's 90th
        percentile sits far above it. Filtering strictly would report 'no
        candidates' for a song plainly in the library."""
        floor = artist_floor([1_000_000, 900_000, 800_000, 700_000])
        deep_cut_pressings = [1200, 900]
        kept, waived = apply_floor(deep_cut_pressings, floor, lambda c: c)
        assert kept == deep_cut_pressings, "the subject of the question is never filtered out"
        assert waived is True, "and the caller is told the floor was waived"

    def test_a_missing_count_never_fails_the_floor(self):
        """MusicBrainz pressings have no ListenBrainz count. Dropping them for
        having no popularity data would make pass 2 useless."""
        floor = artist_floor([100_000, 90_000, 80_000])
        items = [{"c": None}, {"c": 5}]
        kept, waived = apply_floor(items, floor, lambda r: r["c"])
        assert {"c": None} in kept
        assert not waived

    def test_default_quantile_is_the_top_decile(self):
        assert DEFAULT_QUANTILE == 0.90
