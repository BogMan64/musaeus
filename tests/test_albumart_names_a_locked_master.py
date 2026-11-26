"""A locked archive is named in albumart's own line, not only in the log.

On 2026-11-25 a locked master made `_embed_art` log `Operation not permitted`
per file while the stage summary said "7,975 embed failure(s) — check logs.",
so the run output never said why. These pin the two halves: the reason is
carried out of `_embed_art`, and the stage's line shows it.

On 6cad046 both fail for the right reason: `_embed_art` takes no reason list,
and no summary line exists to test.
"""

from __future__ import annotations

from pathlib import Path

from musaeus import safe_save
from musaeus.stages import albumart


def test_the_reason_for_a_failed_embed_is_carried_to_the_caller(tmp_path, monkeypatch):
    audio = tmp_path / "Artist - Song.m4a"
    audio.write_bytes(b"x")
    cover = tmp_path / "cover.jpg"
    cover.write_bytes(b"\xff\xd8\xff" + b"jpeg body")

    def refused(*_args, **_kwargs):
        raise safe_save.MastersLocked("the file is immutable (chattr +i)")

    monkeypatch.setattr(safe_save, "change_beside", refused)

    why: list[str] = []
    assert albumart._embed_art(str(audio), cover, why) is False
    assert why and "immutable" in why[0]


def test_the_summary_names_the_first_cause():
    note = albumart._embed_failure_note(
        ["a.m4a", "b.m4a"], ["the file is immutable (chattr +i)", "another reason"]
    )
    assert note == "2 embed failure(s): the file is immutable (chattr +i)"


def test_without_a_reason_the_summary_still_points_at_the_log():
    assert albumart._embed_failure_note(["a.m4a"], []) == ("1 embed failure(s) — check logs.")


def test_a_very_long_reason_is_cut_so_it_does_not_swallow_the_line():
    note = albumart._embed_failure_note(["a"], ["x" * 500])
    assert note.endswith("x" * 200) and len(note) < 250


def test_a_successful_embed_records_no_reason(tmp_path, monkeypatch):
    audio = tmp_path / "s.m4a"
    audio.write_bytes(b"x")
    cover = tmp_path / "cover.jpg"
    cover.write_bytes(b"\xff\xd8\xff" + b"jpeg body")

    monkeypatch.setattr(safe_save, "change_beside", lambda *a, **k: None)

    why: list[str] = []
    assert albumart._embed_art(str(audio), cover, why) is True
    assert why == []
    assert Path(audio).exists()
