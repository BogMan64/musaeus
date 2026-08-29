"""Moving the article form out of `artist` and into `soar`, on disk."""

from __future__ import annotations

import importlib.util
import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

_SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "split_artist_sort_form.py"
_spec = importlib.util.spec_from_file_location("split_artist_sort_form", _SCRIPT)
assert _spec and _spec.loader
sp = importlib.util.module_from_spec(_spec)
sys.modules["split_artist_sort_form"] = sp
_spec.loader.exec_module(sp)

pytestmark = pytest.mark.skipif(
    not shutil.which("ffmpeg"), reason="ffmpeg needed to mint a real m4a"
)

_ATOMS = {"artist": "\xa9ART", "albumartist": "aART",
          "sort_artist": "soar", "sort_albumartist": "soaa"}


def _make_m4a(path: Path, **tags: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
         "-f", "lavfi", "-i", "anullsrc=r=44100:cl=stereo", "-t", "0.3",
         "-c:a", "alac", str(path)],
        check=True,
    )
    from mutagen.mp4 import MP4

    a = MP4(str(path))
    if a.tags is None:
        a.add_tags()
    for name, val in tags.items():
        a[_ATOMS[name]] = [val]
    a.save()
    return path


def _on_disk(path: Path) -> dict[str, str]:
    from mutagen.mp4 import MP4

    t = MP4(str(path)).tags or {}
    return {n: (str(t[a][0]) if t.get(a) else "") for n, a in _ATOMS.items()}


# ── the plan ──────────────────────────────────────────────────────────────────


def test_an_article_artist_is_split_across_both_fields():
    fields = {"artist": "Stooges, The", "albumartist": "", "sort_artist": "",
              "sort_albumartist": ""}
    assert sp.plan_for(fields) == {
        "artist": "The Stooges", "sort_artist": "Stooges, The"
    }


def test_a_name_with_no_article_is_not_planned():
    for name in ("Dusty Springfield", "TLC", "AC/DC"):
        fields = {"artist": name, "albumartist": "", "sort_artist": "",
                  "sort_albumartist": ""}
        assert sp.plan_for(fields) == {}


def test_a_stylized_name_is_not_planned():
    """"De La Soul" -> "La Soul, De" was live corruption, 2026-08-16."""
    for name in ("De La Soul", "Los Lobos", "La Roux"):
        fields = {"artist": name, "albumartist": "", "sort_artist": "",
                  "sort_albumartist": ""}
        assert sp.plan_for(fields) == {}


def test_an_already_split_file_is_not_replanned():
    fields = {"artist": "The Stooges", "albumartist": "",
              "sort_artist": "Stooges, The", "sort_albumartist": ""}
    assert sp.plan_for(fields) == {}


def test_albumartist_follows_only_when_it_already_agreed():
    agreed = {"artist": "Stooges, The", "albumartist": "Stooges, The",
              "sort_artist": "", "sort_albumartist": ""}
    plan = sp.plan_for(agreed)
    assert plan["albumartist"] == "The Stooges"
    assert plan["sort_albumartist"] == "Stooges, The"


def test_a_genuinely_different_albumartist_is_left_alone():
    """A compilation's albumartist is not the track artist."""
    fields = {"artist": "Stooges, The", "albumartist": "Various Artists",
              "sort_artist": "", "sort_albumartist": ""}
    plan = sp.plan_for(fields)
    assert "albumartist" not in plan
    assert "sort_albumartist" not in plan


def test_an_absent_albumartist_is_not_invented():
    fields = {"artist": "Stooges, The", "albumartist": "",
              "sort_artist": "", "sort_albumartist": ""}
    assert "albumartist" not in sp.plan_for(fields)


# ── scan ──────────────────────────────────────────────────────────────────────


def test_scan_selects_only_article_artists(tmp_path):
    lib = tmp_path / "lib"
    _make_m4a(lib / "1.m4a", artist="Stooges, The")
    _make_m4a(lib / "2.m4a", artist="Dusty Springfield")
    _make_m4a(lib / "3.m4a", artist="De La Soul")
    planned, tally = sp.scan(lib, None)
    assert [Path(r["path"]).name for r in planned] == ["1.m4a"]
    assert tally["WOULD SPLIT"] == 1
    assert tally["no article -- nothing to split"] == 2


def test_limit_caps_the_plan_but_not_the_count(tmp_path):
    lib = tmp_path / "lib"
    for i in range(4):
        _make_m4a(lib / f"{i}.m4a", artist="Stooges, The")
    planned, tally = sp.scan(lib, 2)
    assert len(planned) == 2
    assert tally["WOULD SPLIT"] == 4


# ── apply / undo ──────────────────────────────────────────────────────────────


def test_apply_writes_both_forms_to_disk(tmp_path):
    lib = tmp_path / "lib"
    p = _make_m4a(lib / "1.m4a", artist="Stooges, The", albumartist="Stooges, The")
    planned, _ = sp.scan(lib, None)
    assert sp.apply(planned, tmp_path / "j.jsonl")["written and verified"] == 1

    got = _on_disk(p)
    assert got["artist"] == "The Stooges"
    assert got["sort_artist"] == "Stooges, The"
    assert got["albumartist"] == "The Stooges"
    assert got["sort_albumartist"] == "Stooges, The"


def test_running_twice_is_a_no_op(tmp_path):
    lib = tmp_path / "lib"
    p = _make_m4a(lib / "1.m4a", artist="Stooges, The")
    sp.apply(sp.scan(lib, None)[0], tmp_path / "j1.jsonl")
    planned, tally = sp.scan(lib, None)
    assert planned == []
    assert tally["already split"] == 1 or tally["no article -- nothing to split"] == 0
    assert _on_disk(p)["artist"] == "The Stooges"


def test_a_silent_no_op_write_is_caught(tmp_path, monkeypatch):
    from mutagen.mp4 import MP4

    lib = tmp_path / "lib"
    _make_m4a(lib / "1.m4a", artist="Stooges, The")
    planned, _ = sp.scan(lib, None)
    monkeypatch.setattr(MP4, "save", lambda self, *a, **k: None)
    result = sp.apply(planned, tmp_path / "j.jsonl")
    assert result["FAILED"] == 1
    assert not result["written and verified"]


def test_the_journal_is_durable_before_the_file_is_touched(tmp_path, monkeypatch):
    lib = tmp_path / "lib"
    _make_m4a(lib / "1.m4a", artist="Stooges, The")
    planned, _ = sp.scan(lib, None)
    journal = tmp_path / "j.jsonl"
    monkeypatch.setattr(sp, "write_fields", lambda p, v: (_ for _ in ()).throw(
        KeyboardInterrupt("power cut")))
    with pytest.raises(KeyboardInterrupt):
        sp.apply(planned, journal)
    assert json.loads(journal.read_text().strip())["before"]["artist"] == "Stooges, The"


def test_undo_restores_every_changed_field(tmp_path):
    lib = tmp_path / "lib"
    p = _make_m4a(lib / "1.m4a", artist="Stooges, The", albumartist="Stooges, The")
    journal = tmp_path / "j.jsonl"
    sp.apply(sp.scan(lib, None)[0], journal)
    assert sp.undo(journal)["restored"] == 1

    got = _on_disk(p)
    assert got["artist"] == "Stooges, The"
    assert got["albumartist"] == "Stooges, The"
    assert got["sort_artist"] == ""


def test_undo_is_idempotent(tmp_path):
    lib = tmp_path / "lib"
    _make_m4a(lib / "1.m4a", artist="Stooges, The")
    journal = tmp_path / "j.jsonl"
    sp.apply(sp.scan(lib, None)[0], journal)
    sp.undo(journal)
    assert sp.undo(journal)["already back"] == 1


def test_undo_refuses_a_file_something_else_changed(tmp_path):
    lib = tmp_path / "lib"
    p = _make_m4a(lib / "1.m4a", artist="Stooges, The")
    journal = tmp_path / "j.jsonl"
    sp.apply(sp.scan(lib, None)[0], journal)
    sp.write_fields(p, {"artist": "Someone Else"})
    assert sp.undo(journal)["REFUSED: changed by something else"] == 1
    assert _on_disk(p)["artist"] == "Someone Else"


def test_dry_run_writes_nothing(tmp_path):
    lib = tmp_path / "lib"
    p = _make_m4a(lib / "1.m4a", artist="Stooges, The")
    out = subprocess.run(
        [sys.executable, str(_SCRIPT), "--root", str(lib)],
        capture_output=True, text=True,
        env={**__import__("os").environ, "MUSAEUS_VAULT_ROOT": str(tmp_path)},
    )
    assert "DRY RUN" in out.stdout, out.stdout + out.stderr
    assert _on_disk(p)["artist"] == "Stooges, The"
