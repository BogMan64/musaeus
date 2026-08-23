"""Do the new verify_effect hooks actually FAIL when the effect didn't happen?"""
import sqlite3, types, pytest
from musaeus.stages.sanitize import SanitizeStage
from musaeus.stages.genre_validate import GenreValidateStage
from musaeus.context import StageResult

def _db():
    c = sqlite3.connect(":memory:"); c.row_factory = sqlite3.Row
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
    r = StageResult(stage_name="x", success=True); r.files_changed = n; return r

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
