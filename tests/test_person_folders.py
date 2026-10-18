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
    "Bob Seger\tBob Seger\tRock\n"
    "Bob Seger & The Silver Bullet Band\tBob Seger\n"
    "Jimi Hendrix\tJimi Hendrix\tBlues Rock\n"
    "The Jimi Hendrix Experience\tJimi Hendrix\tBlues Rock\n"
    "Paul McCartney\tPaul McCartney\tPop\n"
    "Wings\tPaul McCartney\n"
)


@pytest.fixture
def f(tmp_path: Path):
    meta = tmp_path / "MetaData"
    meta.mkdir()
    (meta / filing.PERSONS_FILENAME).write_text(LIST, encoding="utf-8")
    (meta / "Genre_Allowed.txt").write_text("Rock\nPop\nBlues Rock\n", encoding="utf-8")
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
    (meta / filing.PERSONS_FILENAME).write_text(
        "Paul McCartney\tPaul McCartney\nWings\tPaul McCartney\nWings\tDenny Laine\n"
    )
    with pytest.raises(filing.FilingError):
        filing.load(meta)


# Review of #208: one genre per person, and a list that cannot quietly go wrong.


def _load(tmp_path, text, allowed="Rock\nPop\nBlues Rock\nR&B/Funk/Soul\n"):
    meta = tmp_path / "MetaData"
    meta.mkdir()
    (meta / filing.PERSONS_FILENAME).write_text(text, encoding="utf-8")
    (meta / "Genre_Allowed.txt").write_text(allowed, encoding="utf-8")
    return filing.load(meta)


def test_a_genre_on_one_line_holds_for_all_the_persons_acts(tmp_path):
    f = _load(
        tmp_path, "Diana Ross\tDiana Ross\tR&B/Funk/Soul\nDiana Ross & The Supremes\tDiana Ross\n"
    )
    rel = library_relpath("Diana Ross & The Supremes", None, "Pop", "Hits", "Baby Love", ".m4a", f)
    assert rel.parts[:2] == ("R&B-Funk-Soul", "Diana Ross")


@pytest.mark.parametrize(
    "text",
    [
        "A\tX\tRock\nA & The B\tX\tPop\n",  # two genres for one person
        "A\tX\tRokc\nA & The B\tX\n",  # a genre that is not allowed (typo)
        "A\tX\tRock\nB\tY\tRock\n",  # a person with one act (typo in the person column)
        "A\tBob Seger\tRock\nA & The B\tBob seger\n",  # one person spelled two ways
        "A\tX\nA & The B\tX\n",  # no genre: each act would follow its own songs
    ],
)
def test_a_list_that_would_misfile_is_refused(tmp_path, text):
    with pytest.raises(filing.FilingError):
        _load(tmp_path, text)


def test_organize_moves_the_path_named_in_the_side_tables_too(tmp_path):
    # Review of #208: only archive.file_path followed a move.
    from musaeus.config import MusicConfig
    from musaeus.context import RunContext
    from musaeus.db import open_db, upsert_archive
    from musaeus.stages.organize import OrganizeStage

    cfg = MusicConfig(
        vault_root=tmp_path, inbox=tmp_path / "INBOX", staging=tmp_path / "STAGING",
        quarantine=tmp_path / "Q", runs_root=tmp_path / "RUNS", meta_dir=tmp_path / "Meta",
        alac_library=tmp_path / "ALAC-Library", db_path=tmp_path / "musaeus.db",
    )  # fmt: skip
    cfg.inbox.mkdir(parents=True)
    cfg.meta_dir.mkdir()
    (cfg.meta_dir / "Genre_Allowed.txt").write_text("Rock\n", encoding="utf-8")
    (cfg.meta_dir / filing.PERSONS_FILENAME).write_text(
        "Bob Seger\tBob Seger\tRock\nBob Seger & The Silver Bullet Band\tBob Seger\n",
        encoding="utf-8",
    )
    ctx = RunContext.new(cfg, open_db(cfg.db_path), dry_run=False)
    old = cfg.inbox / "flat" / "nm.m4a"
    old.parent.mkdir()
    old.write_bytes(b"AUDIO")
    upsert_archive(ctx.conn, {"file_path": str(old), "status": "CATALOGUED", "genre": "Rock",
                              "artist": "Bob Seger & The Silver Bullet Band",
                              "album": "Night Moves", "title": "Night Moves"})  # fmt: skip
    solo = cfg.inbox / "flat" / "ttp.m4a"  # every listed act has a song
    solo.write_bytes(b"AUDIO2")
    upsert_archive(ctx.conn, {"file_path": str(solo), "status": "CATALOGUED", "genre": "Rock",
                              "artist": "Bob Seger", "album": "Live", "title": "Turn The Page"})  # fmt: skip
    ctx.conn.execute(
        "INSERT INTO validation_issues (file_path, issue) VALUES (?, ?)", (str(old), "x")
    )
    ctx.conn.execute(
        "INSERT INTO archive_tier_hashes (path, sha256) VALUES (?, 'MOVED_FILE')", (str(old),)
    )
    # Codex review of #208: a stale row already naming the destination made the
    # follow-the-move UPDATE OR IGNORE change nothing, so bitrot later checked
    # the moved master against the stale baseline.
    dest = cfg.inbox / "Rock/Bob Seger/Bob Seger & The Silver Bullet Band/Night Moves"
    stale = dest / "Bob Seger & The Silver Bullet Band - Night Moves.m4a"
    ctx.conn.execute(
        "INSERT INTO archive_tier_hashes (path, sha256) VALUES (?, 'STALE')", (str(stale),)
    )
    # Codex review of #208 (second round): review records already naming the
    # free destination are kept, not wiped; an issue the moved file already
    # carries is not written twice.
    ctx.conn.execute("INSERT INTO duplicates (group_id, file_path) VALUES ('g1', ?)", (str(stale),))
    ctx.conn.execute(
        "INSERT INTO validation_issues (file_path, issue) VALUES (?, 'x')", (str(stale),)
    )
    ctx.conn.execute(
        "INSERT INTO validation_issues (file_path, issue) VALUES (?, 'y')", (str(stale),)
    )
    ctx.conn.commit()

    OrganizeStage().run(ctx)

    new = ctx.conn.execute("SELECT file_path FROM archive WHERE title = 'Night Moves'").fetchone()[
        0
    ]
    assert new == str(stale)
    issues = sorted(
        tuple(r) for r in ctx.conn.execute("SELECT file_path, issue FROM validation_issues")
    )
    assert issues == [(new, "x"), (new, "y")], "an issue was lost or still names the old place"
    groups = [tuple(r) for r in ctx.conn.execute("SELECT group_id, file_path FROM duplicates")]
    assert groups == [("g1", new)], "a pending duplicate review was wiped"
    baselines = [tuple(r) for r in ctx.conn.execute("SELECT path, sha256 FROM archive_tier_hashes")]
    assert baselines == [(new, "MOVED_FILE")], "the moved master has a stale baseline"


def test_a_dated_batch_folder_is_not_mistaken_for_a_person(tmp_path):
    # Codex review of #208: with MUSAEUS_BATCH_FOLDERS the masters sit under a
    # dated folder; depth alone read the date level as a person.
    masters, car = tmp_path / "ALAC-Archival", tmp_path / "CAR_Library"
    flat = masters / "2026-01-15/Rock/Adele/21/x.m4a"
    nested = masters / "2026-01-15A/Rock/Bob Seger/Bob Seger/Night Moves/y.m4a"
    assert editions.artist_album_path(flat, car, masters) == car / "Adele/21/x.m4a"
    assert (
        editions.artist_album_path(nested, car, masters)
        == car / "Bob Seger/Bob Seger/Night Moves/y.m4a"
    )


def test_the_genre_takes_the_allowed_lists_spelling(tmp_path):
    # Review of #208: a lone "rock" passed the check and made a "rock" folder
    # beside "Rock"; "Rock" and "rock" together picked either, run to run.
    f = _load(tmp_path, "A\tX\trock\nA & The B\tX\tRock\n")
    rel = library_relpath("A & The B", None, "Pop", "Album", "Song", ".m4a", f)
    assert rel.parts[:2] == ("Rock", "X")


def test_a_database_error_mid_move_puts_the_file_and_its_row_back(tmp_path):
    # Review of #208: only a collision was caught. Any other database error
    # ("database is locked") left the file moved, the row naming the old
    # place, and the savepoint open for the next commit to keep half of.
    import contextlib
    import sqlite3

    from musaeus.config import MusicConfig
    from musaeus.context import RunContext
    from musaeus.db import open_db, upsert_archive
    from musaeus.stages.organize import OrganizeStage

    cfg = MusicConfig(
        vault_root=tmp_path, inbox=tmp_path / "INBOX", staging=tmp_path / "STAGING",
        quarantine=tmp_path / "Q", runs_root=tmp_path / "RUNS", meta_dir=tmp_path / "Meta",
        alac_library=tmp_path / "ALAC-Library", db_path=tmp_path / "musaeus.db",
    )  # fmt: skip
    cfg.inbox.mkdir(parents=True)
    cfg.meta_dir.mkdir()
    ctx = RunContext.new(cfg, open_db(cfg.db_path), dry_run=False)
    old = cfg.inbox / "flat" / "s.m4a"
    old.parent.mkdir()
    old.write_bytes(b"AUDIO")
    upsert_archive(ctx.conn, {"file_path": str(old), "status": "CATALOGUED", "genre": "Pop",
                              "artist": "Adele", "album": "21", "title": "Hello"})  # fmt: skip
    ctx.conn.commit()

    def deny(action, table, *_):
        refuse = action == sqlite3.SQLITE_DELETE and table == "metadata_cache"
        return sqlite3.SQLITE_DENY if refuse else sqlite3.SQLITE_OK

    ctx.conn.set_authorizer(deny)
    # Stopping is right; what matters is what it left behind.
    with contextlib.suppress(sqlite3.Error):
        OrganizeStage().run(ctx)
    ctx.conn.set_authorizer(None)
    ctx.conn.commit()  # what a later commit would keep

    assert old.exists(), "the file was left at the new place"
    row = ctx.conn.execute("SELECT file_path FROM archive").fetchone()[0]
    assert row == str(old), "the row names a place the file is not"


def test_a_person_list_without_the_allowed_genre_list_is_refused(tmp_path):
    # Codex review of #208: with Genre_Allowed.txt missing, "Rokc" made a new
    # top-level folder.
    meta = tmp_path / "MetaData"
    meta.mkdir()
    (meta / filing.PERSONS_FILENAME).write_text("A\tX\tRokc\nA & The B\tX\n", encoding="utf-8")
    with pytest.raises(filing.FilingError):
        filing.load(meta)


def test_a_listed_act_that_matches_no_song_is_named_before_anything_moves(tmp_path):
    # Codex review of #208: "Bob Segre & The Silver Bullet Band" (a typo) left
    # the band flat while the solo songs nested, without a word.
    from musaeus.config import MusicConfig
    from musaeus.context import RunContext
    from musaeus.db import open_db, upsert_archive
    from musaeus.stages.organize import OrganizeStage

    cfg = MusicConfig(
        vault_root=tmp_path, inbox=tmp_path / "INBOX", staging=tmp_path / "STAGING",
        quarantine=tmp_path / "Q", runs_root=tmp_path / "RUNS", meta_dir=tmp_path / "Meta",
        alac_library=tmp_path / "ALAC-Library", db_path=tmp_path / "musaeus.db",
    )  # fmt: skip
    cfg.inbox.mkdir(parents=True)
    cfg.meta_dir.mkdir()
    (cfg.meta_dir / "Genre_Allowed.txt").write_text("Rock\n", encoding="utf-8")
    (cfg.meta_dir / filing.PERSONS_FILENAME).write_text(
        "Bob Seger\tBob Seger\tRock\nBob Segre & The Silver Bullet Band\tBob Seger\n",
        encoding="utf-8",
    )
    ctx = RunContext.new(cfg, open_db(cfg.db_path), dry_run=True)
    song = cfg.inbox / "flat" / "s.m4a"
    song.parent.mkdir()
    song.write_bytes(b"AUDIO")
    upsert_archive(ctx.conn, {"file_path": str(song), "status": "CATALOGUED", "genre": "Rock",
                              "artist": "Bob Seger", "album": "Live", "title": "Turn The Page"})  # fmt: skip
    ctx.conn.commit()

    result = OrganizeStage().dry_run(ctx)

    assert any("Bob Segre & The Silver Bullet Band" in n for n in result.notes)
    assert not any("'Bob Seger'" in n for n in result.notes), "a matched act was named"
    assert song.exists(), "a dry run moved a file"

    # Codex review of #208 (second round): a note after the moves came too
    # late. A real run refuses before moving anything.
    ctx.dry_run = False
    result = OrganizeStage().run(ctx)
    assert not result.success
    assert song.exists(), "a real run moved files while the list named an unknown act"


def test_the_old_car_script_keeps_the_person_level_too(tmp_path):
    # Codex review of #208: output_path_for dropped the person level.
    from types import SimpleNamespace

    masters, car = tmp_path / "ALAC-Archival", tmp_path / "CAR"
    track = SimpleNamespace(file_path=str(masters / "Rock/Bob Seger/Bob Seger/Live/x.m4a"))
    got = editions.output_path_for(track, None, car, masters)  # type: ignore[arg-type]
    assert got == car / "Bob Seger/Bob Seger/Live/x.m4a"
