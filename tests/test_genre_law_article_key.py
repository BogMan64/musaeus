"""The law and the library must agree on what an artist is called.

MasterLaw held pre-normalisation spellings ("5th Dimension (the)", "The
Byrds") while the library stores the article as a suffix ("5th Dimension,
The"). 246 rules were dormant purely because their key never matched, and
a dormant rule is indistinguishable from an absent one -- nothing reported
it for as long as the file existed.
"""
import csv

from musaeus.canon.genre_law import GenreLaw


def test_article_spellings_share_one_key():
    k = GenreLaw._key
    assert k("The Byrds") == k("Byrds, The") == k("byrds (the)") == "byrds"
    assert k("5th Dimension (the)") == k("5th Dimension, The")


def test_key_does_not_strip_a_leading_the_inside_a_name():
    """Only a standalone article folds -- 'Theo' must not become 'o'."""
    assert GenreLaw._key("Theo Howard") == "theo howard"
    assert GenreLaw._key("Them") == "them"


def test_law_lookup_survives_either_spelling(tmp_path):
    p = tmp_path / "MasterLaw.csv"
    with p.open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["artist", "genre"])
        w.writerow(["Byrds, The", "Rock"])
    law = GenreLaw(p)
    for spelling in ("Byrds, The", "The Byrds", "the byrds", "Byrds (the)"):
        assert law.genre_for(spelling) == "Rock", spelling


def test_duplicate_article_spellings_collapse_to_one_rule(tmp_path):
    """Two spellings of one artist must not become two competing rules.

    MasterLaw carried 41 such pairs -- "Beach Boys"/Pop alongside
    "Beach Boys, The"/Surf Rock -- which under the old key were simply two
    unrelated artists, so whichever sorted last silently won.
    """
    p = tmp_path / "MasterLaw.csv"
    with p.open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["artist", "genre"])
        w.writerow(["Beach Boys", "Pop"])
        w.writerow(["Beach Boys, The", "Surf Rock"])
    law = GenreLaw(p)
    assert len(law) == 1, "one artist, one rule"
    assert law.genre_for("Beach Boys") == law.genre_for("Beach Boys, The")
