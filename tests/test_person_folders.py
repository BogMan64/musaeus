"""Grey, 2026-10-18: a person with two or more act folders gets a person folder
holding them -- Genre/Person/Act/Album/song -- in the library and in the car and
iPhone editions, under the person's one genre (MetaData/person_folders.tsv).
Grey searches by the person ("Bob Seger") and does not remember which band a
record was with. Everyone else keeps one level."""

from __future__ import annotations

from pathlib import Path

import pytest

from musaeus import editions, filing
from musaeus.stages.organize import library_relpath

LIST = (
    "# act TAB person TAB genre\n"
    "Bob Seger\tBob Seger\n"
    "Bob Seger & The Silver Bullet Band\tBob Seger\n"
    "Jimi Hendrix\tJimi Hendrix\tBlues Rock\n"
    "The Jimi Hendrix Experience\tJimi Hendrix\tBlues Rock\n"
    "Wings\tPaul McCartney\n"
)


@pytest.fixture
def f(tmp_path: Path):
    meta = tmp_path / "MetaData"
    meta.mkdir()
    (meta / filing.PERSONS_FILENAME).write_text(LIST, encoding="utf-8")
    return filing.load(meta)


def test_a_listed_act_sits_in_its_persons_folder(f):
    rel = library_relpath("Bob Seger & The Silver Bullet Band", None, "Rock", "Night Moves",
                          "Night Moves", ".m4a", f)  # fmt: skip
    assert rel.parts[:4] == (
        "Rock",
        "Bob Seger",
        "Bob Seger & The Silver Bullet Band",
        "Night Moves",
    )
    solo = library_relpath("Bob Seger", None, "Rock", "The Fire Inside", "Real Love", ".m4a", f)
    assert solo.parts[:3] == ("Rock", "Bob Seger", "Bob Seger")


def test_an_unlisted_act_keeps_one_level(f):
    rel = library_relpath("Adele", None, "Pop", "21", "Rolling In The Deep", ".m4a", f)
    assert rel.parts[:3] == ("Pop", "Adele", "21")


def test_the_person_is_found_whatever_form_the_act_folder_takes(f):
    # The folder is "Jimi Hendrix Experience, The"; the list says "The Jimi ...".
    rel = library_relpath("The Jimi Hendrix Experience", None, "Psychedelic Rock",
                          "Are You Experienced", "Fire", ".m4a", f)  # fmt: skip
    assert rel.parts[:3] == ("Blues Rock", "Jimi Hendrix", "Jimi Hendrix Experience, The")


def test_the_file_name_keeps_the_act(f):
    rel = library_relpath("Wings", None, "Pop", "Band On The Run", "Jet", ".m4a", f)
    assert rel.parts[1] == "Paul McCartney" and rel.name == "Wings - Jet.m4a"


def test_the_car_keeps_the_person_level_and_one_level_stays_one(tmp_path):
    masters, car = tmp_path / "ALAC-Archival", tmp_path / "CAR_Library"
    nested = masters / "Rock/Bob Seger/Bob Seger & The Silver Bullet Band/Night Moves/x.m4a"
    flat = masters / "Pop/Adele/21/y.m4a"
    assert editions.artist_album_path(nested, car, masters) == (
        car / "Bob Seger/Bob Seger & The Silver Bullet Band/Night Moves/x.m4a"
    )
    assert editions.artist_album_path(flat, car, masters) == car / "Adele/21/y.m4a"


def test_the_car_build_and_its_preview_place_a_song_the_same_way(tmp_path):
    # One rule (cloud review of #53): every edition placement goes through Kind.place.
    from musaeus.edition_build import KINDS

    masters, car = tmp_path / "ALAC-Archival", tmp_path / "CAR_Library"
    nested = masters / "Rock/Bob Seger/Bob Seger/Stranger In Town/z.m4a"
    for kind in ("car", "iphone"):
        assert (
            KINDS[kind].place(masters, car, nested)
            == car / "Bob Seger/Bob Seger/Stranger In Town/z.m4a"
        )


def test_an_ambiguous_person_list_is_refused(tmp_path):
    meta = tmp_path / "MetaData"
    meta.mkdir()
    (meta / filing.PERSONS_FILENAME).write_text("Wings\tPaul McCartney\nWings\tDenny Laine\n")
    with pytest.raises(filing.FilingError):
        filing.load(meta)
