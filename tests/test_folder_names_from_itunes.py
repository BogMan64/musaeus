"""Grey, 2026-10-17: artist FOLDER names follow the folders Grey made in Windows
iTunes, song by song (MetaData/folder_names.tsv), and are written in the natural
form -- "The Alan Parsons Project", not "Alan Parsons Project, The". Folder only:
the artist tag and the title are not changed. "&" stays (2026-10-12 ruling).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from musaeus import filing
from musaeus.stages.organize import library_artist, library_relpath

H = "a" * 64


def _meta(tmp_path: Path, songs: str = "", rulings: str = "") -> Path:
    meta = tmp_path / "MetaData"
    meta.mkdir()
    if songs:
        (meta / filing.SONGS_FILENAME).write_text(songs, encoding="utf-8")
    if rulings:
        (meta / filing.FILENAME).write_text(rulings, encoding="utf-8")
    return meta


def test_a_band_folder_is_written_the_x(tmp_path):
    rel = library_relpath("The Alan Parsons Project", None, "Rock", "Eye In The Sky",
                          "Sirius", ".m4a", filing.load(_meta(tmp_path)))  # fmt: skip
    assert rel.parts[1] == "The Alan Parsons Project"


def test_a_tag_in_the_old_sort_form_files_the_same(tmp_path):
    rel = library_relpath("Alan Parsons Project, The", None, "Rock", "Eye In The Sky",
                          "Sirius", ".m4a", filing.load(_meta(tmp_path)))  # fmt: skip
    assert rel.parts[1] == "The Alan Parsons Project"


def test_greys_song_folder_wins_over_the_artist_and_any_ruling(tmp_path):
    meta = _meta(
        tmp_path,
        songs=f"# comment\n{H}\tWings\tJet (was Paul McCartney)\n",
        rulings="Paul McCartney\tPaul McCartney\n",
    )
    f = filing.load(meta)
    jet = library_relpath("Paul McCartney", None, "Pop", "Band On The Run", "Jet", ".m4a", f, H)
    other = library_relpath("Paul McCartney", None, "Pop", "Ram", "Too Many People", ".m4a", f,
                            "b" * 64)  # fmt: skip
    assert jet.parts[1] == "Wings" and other.parts[1] == "Paul McCartney"


def test_the_song_folder_keeps_the_ampersand_rule(tmp_path):
    f = filing.load(_meta(tmp_path, songs=f"{H}\tTommy James and the Shondells\n"))
    rel = library_relpath("Tommy James", None, "Pop", "Hits", "Mony Mony", ".m4a", f, H)
    assert rel.parts[1] == "Tommy James & The Shondells"  # MUSAEUS capitals kept


def test_without_a_hash_nothing_changes(tmp_path):
    f = filing.load(_meta(tmp_path, songs=f"{H}\tWings\n"))
    assert library_artist("Paul McCartney", None, f) == "Paul McCartney"


@pytest.mark.parametrize("bad", ["onlyonecolumn\n", f"{H}\tWings\n{H}\tPaul McCartney\n"])
def test_an_unreadable_or_ambiguous_list_is_refused(tmp_path, bad):
    with pytest.raises(filing.FilingError):
        filing.load(_meta(tmp_path, songs=bad))
