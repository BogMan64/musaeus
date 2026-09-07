"""Do the new verify_effect hooks actually FAIL when the effect didn't happen?"""
import sqlite3
import types

from musaeus.context import StageResult
from musaeus.stages.genre_validate import GenreValidateStage
from musaeus.stages.sanitize import SanitizeStage


def _db():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    c.execute("CREATE TABLE archive (file_path TEXT, artist TEXT, album TEXT, "
              "title TEXT, genre TEXT, status TEXT)")
    return c

def _ctx(conn, cfg=None):
    return types.SimpleNamespace(conn=conn, config=cfg, run_id="r1")

class _EmptyLaw:
    """A law with no entries: len()==0, so the vocabulary check is skipped."""
    def __len__(self): return 0
    def permits(self, g): return True

def _res(n):
    r = StageResult(stage_name="x", success=True)
    r.files_changed = n
    return r

def test_sanitize_flags_unsanitised_row():
    c = _db()
    c.execute("INSERT INTO archive VALUES ('/a.m4a','Bad\x07Name','Al','Ti','Rock','CATALOGUED')")
    problems = SanitizeStage().verify_effect(_ctx(c), _res(1))
    assert problems, "control char left in place must be reported"
    assert "unsafe metadata" in problems[0]

def test_sanitize_passes_when_clean():
    c = _db()
    c.execute("INSERT INTO archive VALUES ('/a.m4a','AC/DC','Al','Ti','Rock','CATALOGUED')")
    # AC/DC must NOT be flagged -- "/" is a path rule, not a metadata rule.
    assert SanitizeStage().verify_effect(_ctx(c), _res(1)) == []

def test_genre_validate_flags_split_artist():
    c = _db()
    c.execute("INSERT INTO archive VALUES ('/a.m4a','Queen','A','T','Rock','CATALOGUED')")
    c.execute("INSERT INTO archive VALUES ('/b.m4a','Queen','B','U','Pop','CATALOGUED')")
    st = GenreValidateStage()
    st._law = lambda ctx: _EmptyLaw()
    problems = st.verify_effect(_ctx(c), _res(2))
    assert problems and "more than one genre" in problems[0]
    assert "Queen" in problems[0]

def test_genre_validate_passes_one_genre_per_artist():
    c = _db()
    c.execute("INSERT INTO archive VALUES ('/a.m4a','Queen','A','T','Rock','CATALOGUED')")
    c.execute("INSERT INTO archive VALUES ('/b.m4a','Queen','B','U','Rock','CATALOGUED')")
    st = GenreValidateStage()
    st._law = lambda ctx: _EmptyLaw()
    assert st.verify_effect(_ctx(c), _res(2)) == []


def test_normalize_flags_unstable_name():
    from musaeus.stages.normalize import NormalizeStage
    c = _db()
    # Lowercase artist that normalize would still title-case: not a fixed point.
    c.execute("INSERT INTO archive VALUES ('/a.m4a','the beatles','A','T','Rock','CATALOGUED')")
    problems = NormalizeStage().verify_effect(_ctx(c), _res(1))
    assert problems and "would still change" in problems[0]


def test_normalize_leaves_protected_names_alone():
    from musaeus.stages.normalize import NormalizeStage
    c = _db()
    for a in ("AC/DC", "Beatles, The", "R.E.M."):
        c.execute("INSERT INTO archive VALUES ('/x.m4a',?,'A','T','Rock','CATALOGUED')", (a,))
    assert NormalizeStage().verify_effect(_ctx(c), _res(1)) == []


class _RealLaw:
    """A law with a real vocabulary, for the canonical-spelling check."""
    def __init__(self, genres): self._g = set(genres)
    def __len__(self): return len(self._g)
    @property
    def genres(self): return self._g
    def permits(self, g):
        def n(s):
            return s.replace("/", "-").strip().lower()

        return n(g) in {n(x) for x in self._g}


def test_genre_validate_flags_non_canonical_spelling():
    """'pop' is legal under permits() but is not how the law spells it."""
    c = _db()
    c.execute("INSERT INTO archive VALUES ('/a.m4a','Gwen Stefani','A','Hollaback Girl','pop','CATALOGUED')")
    st = GenreValidateStage()
    st._law = lambda ctx: _RealLaw({"Pop", "Rock"})
    problems = st.verify_effect(_ctx(c), _res(1))
    assert problems, "a case variant must not be absorbed silently"
    assert "not spelled canonically" in problems[0] and "'pop'" in problems[0]


def test_genre_validate_accepts_slash_variant():
    """The '/' vs '-' fold is deliberate and must NOT be reported."""
    c = _db()
    c.execute("INSERT INTO archive VALUES ('/a.m4a','James Brown','A','T','R&B/Funk/Soul','CATALOGUED')")
    st = GenreValidateStage()
    st._law = lambda ctx: _RealLaw({"R&B/Funk/Soul"})
    assert st.verify_effect(_ctx(c), _res(1)) == []
