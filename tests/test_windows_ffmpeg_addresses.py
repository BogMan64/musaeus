"""Windows ffmpeg prints a log context's address without "0x" (mingw's %p):
"[mov,mp4,m4a,3gp,3g2,mj2 @ 0000018f5e7c2a40]". Both stderr readers only
knew "@ 0x56...", so on Windows a truncated song was measured as whole and a
broken cover picture counted as broken audio (Windows CI, 2026-11-08)."""

from __future__ import annotations

import pytest

from musaeus import loudness
from musaeus.stages import corrupt

ADDRESSES = ["0x56400c7dcb80", "0000018f5e7c2a40", "0000018F5E7C2A40"]


@pytest.mark.parametrize("addr", ADDRESSES)
def test_partial_file_is_damage_whatever_the_address_looks_like(monkeypatch, tmp_path, addr):
    """Loudness must see the "partial file" error on every platform's log format."""
    monkeypatch.setattr(corrupt, "audio_stream_index", lambda p: 0)
    line = f"[mov,mp4,m4a,3gp,3g2,mj2 @ {addr}] [error] stream 0, offset 0xeaa50: partial file\n"
    assert "partial file" in loudness._decode_damage(line, tmp_path / "a.m4a")


@pytest.mark.parametrize("addr", ADDRESSES)
def test_a_cover_picture_error_is_not_audio_damage(addr):
    """The artwork filter must recognise a decoder tag on every platform's log format."""
    line = f"[mjpeg @ {addr}] unable to decode APP fields: Invalid data found"
    assert corrupt.audio_relevant_stderr(line, 0) == ""
