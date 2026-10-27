"""'feat.' needs its dot everywhere a credit is split or repaired.

artist_form._COLLAB learned this from Little Feat (cloud review of #39); the
other credit splitters and normalize's all-caps repair never did. A bare
"feat" cut "Little Feat" to "Little", and "Featuring" to "uring".
"""

from musaeus.art_sources import _lead_artist, _parts
from musaeus.stages.neardupe import _credit, _primary_artist_key
from musaeus.stages.normalize import _smart_title_case
from musaeus.stages.tagger import _CREDIT_SPLIT, _first_credit


def test_art_sources_keeps_little_feat_whole():
    assert _parts("Little Feat") == ["Little Feat"]
    assert _lead_artist("Little Feat") == "Little Feat"


def test_art_sources_splits_featuring_as_one_word():
    # Library credit: the guest was "uring Ziggy Marley", never matched.
    assert _parts("The Chieftains Featuring Ziggy Marley") == ["The Chieftains", "Ziggy Marley"]
    assert _parts("Santana feat. Rob Thomas") == ["Santana", "Rob Thomas"]


def test_neardupe_little_feat_names_one_act():
    assert _primary_artist_key("Little Feat") == ""
    assert _primary_artist_key("Santana feat. Rob Thomas") == "santana"


def test_neardupe_feat_of_strength_bracket_is_no_guest():
    # Codex review of #253: the subtitle was read as a guest credit, so "The
    # Trial" and "The Trial (Feat Of Strength)" were staged as one song.
    assert _credit("The Trial (Feat Of Strength)") == ""
    assert _credit("Smooth (feat. Rob Thomas)") == "feat rob thomas"


def test_tagger_keeps_little_feat_whole_and_drops_the_dot():
    assert _first_credit("Little Feat") == "Little Feat"
    parts = [p for p in _CREDIT_SPLIT.split("Santana feat. Rob Thomas") if p and p.strip()]
    assert parts == ["Santana", "Rob Thomas"]


def test_normalize_all_caps_little_feat_gets_no_dot():
    assert _smart_title_case("LITTLE FEAT") == "Little Feat"
    assert _smart_title_case("LITTLE FEAT - DIXIE CHICKEN") == "Little Feat - Dixie Chicken"


def test_normalize_ft_becomes_one_feat_dot():
    assert _smart_title_case("SANTANA FT ROB THOMAS") == "Santana feat. Rob Thomas"
    assert _smart_title_case("SANTANA FT. ROB THOMAS") == "Santana feat. Rob Thomas"
    assert _smart_title_case("SANTANA FEAT. ROB THOMAS") == "Santana feat. Rob Thomas"
