"""The length check measures the master's audio, not its header (2026-10-06).

Three AAC masters were refused as "length changed" on every build: their headers count
encoder padding, so they claim more than they hold (Elvis Costello "The Monkey" claims
191.90 s and decodes to 191.71 s; its copy was 191.77 s). A master whose claim is far from
its audio is damaged, and says so: Harold Faltermeyer "Axel F" claims 181.7 s, holds 109.8 s.
"""

from __future__ import annotations

import pytest

from musaeus import edition_bake as eb


def _info(seconds, filename="/m/master.m4a"):
    return {
        "streams": [{"codec_type": "audio", "sample_rate": "44100"}],
        "format": {"duration": str(seconds), "filename": filename},
    }


def test_a_copy_matching_the_masters_real_audio_passes(tmp_path, monkeypatch):
    monkeypatch.setattr(eb, "probe", lambda p: _info(191.77))
    monkeypatch.setattr(eb, "decoded_seconds", lambda p: 191.71)
    eb.verify(_info(191.90), tmp_path / "c.m4a", -18.0, length_tolerance=0.1)


def test_a_copy_off_the_real_audio_is_still_refused(tmp_path, monkeypatch):
    monkeypatch.setattr(eb, "probe", lambda p: _info(191.40))
    monkeypatch.setattr(eb, "decoded_seconds", lambda p: 191.71)
    with pytest.raises(eb.BakeError, match="length changed"):
        eb.verify(_info(191.90), tmp_path / "c.m4a", -18.0, length_tolerance=0.1)


def test_a_master_holding_far_less_than_it_claims_is_named_damaged(tmp_path, monkeypatch):
    monkeypatch.setattr(eb, "probe", lambda p: _info(109.89))
    monkeypatch.setattr(eb, "decoded_seconds", lambda p: 109.84)
    with pytest.raises(
        eb.BakeError, match="the master is damaged: it claims 181.70s but holds 109.84s"
    ):
        eb.verify(_info(181.70), tmp_path / "c.m4a", -18.0, length_tolerance=0.1)


def test_when_the_master_cannot_be_measured_the_old_refusal_stands(tmp_path, monkeypatch):
    monkeypatch.setattr(eb, "probe", lambda p: _info(191.77))
    monkeypatch.setattr(eb, "decoded_seconds", lambda p: None)
    with pytest.raises(eb.BakeError, match="length changed"):
        eb.verify(_info(191.90), tmp_path / "c.m4a", -18.0, length_tolerance=0.1)


def test_a_matching_length_never_decodes_the_master(tmp_path, monkeypatch):
    monkeypatch.setattr(eb, "probe", lambda p: _info(200.0))

    def boom(p):
        raise AssertionError("decoded a master whose copy already matched")

    monkeypatch.setattr(eb, "decoded_seconds", boom)
    eb.verify(_info(200.0), tmp_path / "c.m4a", -18.0, length_tolerance=0.1)


def test_decoded_seconds_counts_the_audio(tmp_path):
    import subprocess

    path = tmp_path / "m.m4a"
    subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi",
         "-i", "sine=frequency=440:duration=8:sample_rate=96000", "-c:a", "alac", str(path)],
        check=True,
    )  # fmt: skip
    assert eb.decoded_seconds(path) == pytest.approx(8.0, abs=0.01)
