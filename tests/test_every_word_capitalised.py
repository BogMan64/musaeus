"""Every word capitalised, in artist names and song titles (Grey, 2026-09-25).

He found "Dead Or Alive" and "Dead or Alive" as two folders and asked for
one standard, applied in Act 1, to artist names and song titles:

    every word capitalised     "Dead Or Alive", "Early In The Morning"

    with the exceptions he agreed:
      special spellings kept   ABBA, McCartney, AC/DC
      after an apostrophe      "Don't", never "Don'T"
      feat. and vs.            left exactly as written

Album names take it too since 2026-10-11 (Grey), without the ALL-CAPS
repair: RAM, OU812 and BEYONCÉ are written so on purpose.

The rule has to be the LAST word on a name, or two steps rewrite each other
on every run. Normalize applies it to every catalogued row, and every other
step that writes an artist or title -- the canon pass, the Various Artists
fix, the classical composer step -- writes it in this form too, and compares
in this form, so none of them undoes another.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from musaeus.config import MusicConfig
from musaeus.context import RunContext
from musaeus.db import open_db, upsert_archive
from musaeus.stages.artist_consolidate import ArtistConsolidateStage
from musaeus.stages.classical_composer import ClassicalComposerStage
from musaeus.stages.normalize import NormalizeStage
from musaeus.stages.organize import OrganizeStage, library_relpath
from musaeus.stages.various_artists_fix import VariousArtistsFixStage


def _ctx(tmp_path: Path) -> RunContext:
    cfg = MusicConfig(
        vault_root=tmp_path,
        inbox=tmp_path / "INBOX",
        staging=tmp_path / "STAGING",
        quarantine=tmp_path / "QUARANTINE",
        runs_root=tmp_path / "RUNS",
        meta_dir=tmp_path / "MetaData",
        alac_library=tmp_path / "Libraries" / "ALAC_Library",
        alac_archive=tmp_path / "Libraries" / "ALAC-Archival",
        db_path=tmp_path / "musaeus.db",
    )
    cfg.ensure_dirs()
    return RunContext.new(cfg, open_db(cfg.db_path), dry_run=False)


def _row(ctx: RunContext, path: Path, artist, title, album="Album", genre="Rock", **extra):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"x")
    upsert_archive(
        ctx.conn,
        {
            "file_path": str(path),
            "status": "CATALOGUED",
            "artist": artist,
            "title": title,
            "album": album,
            "genre": genre,
            "audio_hash": path.name,
            **extra,
        },
    )
    ctx.conn.commit()


def _one(ctx: RunContext) -> tuple:
    return tuple(ctx.conn.execute("SELECT artist, title, album FROM archive").fetchone())


def _normalized(tmp_path: Path, artist: str, title: str, album: str = "Album") -> tuple:
    ctx = _ctx(tmp_path)
    _row(ctx, ctx.inbox / "a.m4a", artist, title, album)
    NormalizeStage().run(ctx)
    return _one(ctx)


def test_small_words_get_their_capital_in_artists_and_titles(tmp_path):
    got = _normalized(tmp_path, "Dead or Alive", "You Spin Me Round (Like a Record)")
    assert got[:2] == ("Dead Or Alive", "You Spin Me Round (Like A Record)")


def test_a_lowercase_title_is_capitalised_word_by_word(tmp_path):
    assert _normalized(tmp_path, "Dion", "early in the morning")[1] == "Early In The Morning"


@pytest.mark.parametrize(
    ("artist", "title", "want_title"),
    [
        ("ABBA", "Take a Chance on Me", "Take A Chance On Me"),
        ("AC/DC", "Back in Black", "Back In Black"),
        ("Paul McCartney", "Mull of Kintyre", "Mull Of Kintyre"),
        ("Gorillaz feat. Bobby Womack", "Stylo", "Stylo"),
        ("Dion", "Stylo (feat. Mos Def)", "Stylo (feat. Mos Def)"),
        ("Dion", "Freddie vs. Jason", "Freddie vs. Jason"),
        ("Dion", "Don't Stop Me Now", "Don't Stop Me Now"),
        ("Dion", "Rock 'n' Roll High School", "Rock 'n' Roll High School"),
        ("Weird Al Yankovic", "eBay", "eBay"),
    ],
)
def test_the_agreed_exceptions_are_left_as_written(tmp_path, artist, title, want_title):
    # The artist column is unchanged in every case: ABBA, AC/DC, McCartney
    # and "feat." are exactly the spellings the rule must not touch.
    assert _normalized(tmp_path, artist, title)[:2] == (artist, want_title)


def test_after_an_apostrophe_the_letter_stays_small(tmp_path):
    assert _normalized(tmp_path, "Dion", "don't stop")[1] == "Don't Stop"


def test_album_names_take_every_word_capitalised_too(tmp_path):
    """Grey, 2026-10-11: albums follow artists and titles, in tags and folders.
    The 2026-09-16 small-words rule was never wired into any step."""
    assert (
        _normalized(tmp_path, "AC/DC", "Hells Bells", album="Back in Black")[2] == "Back In Black"
    )
    assert _normalized(tmp_path, "Elton John", "Daniel",
                       album="Don't Shoot Me I'm only the Piano Player")[2] == (
        "Don't Shoot Me I'm Only The Piano Player"
    )  # fmt: skip


def test_act_one_settles_in_one_pass_and_stays_settled(tmp_path):
    # The canon writes its own spelling. If that spelling is not in the
    # every-word form, Normalize changes it on the next run, the canon pass
    # changes it back, and the tracks are renamed on every Act 3.
    ctx = _ctx(tmp_path)
    (ctx.config.meta_dir / "artist_canon.tsv").write_text(
        "pp&m\tPeter, Paul and Mary\n", encoding="utf-8"
    )
    _row(ctx, ctx.inbox / "a.m4a", "pp&m", "Leaving on a Jet Plane")
    for _ in range(2):
        NormalizeStage().run(ctx)
        consolidate = ArtistConsolidateStage().execute(ctx)
        assert consolidate.verified is not False, consolidate.verify_notes
    before = _one(ctx)
    changes = [
        NormalizeStage().run(ctx).files_changed,
        ArtistConsolidateStage().run(ctx).files_changed,
    ]
    assert changes == [0, 0], "a second pass still rewrote names"
    assert before[:2] == ("Peter, Paul And Mary", "Leaving On A Jet Plane")


def test_the_various_artists_fix_writes_the_same_form(tmp_path):
    ctx = _ctx(tmp_path)
    ctx.set("various_artists_no_mb", True)
    _row(
        ctx,
        ctx.inbox / "Various Artists - Dead or Alive - Lover Come Back to Me.m4a",
        "Various Artists",
        "Dead or Alive - Lover Come Back to Me",
    )
    VariousArtistsFixStage().run(ctx)
    assert _one(ctx)[:2] == ("Dead Or Alive", "Lover Come Back To Me")


def test_the_classical_step_does_not_undo_the_rule(tmp_path):
    ctx = _ctx(tmp_path)
    (ctx.config.meta_dir / "Composer_Canon.tsv").write_text(
        "beethoven\tLudwig van Beethoven\nludwig van beethoven\tLudwig van Beethoven\n",
        encoding="utf-8",
    )
    _row(ctx, ctx.inbox / "a.m4a", "Ludwig Van Beethoven", "Symphony No. 5", genre="Classical")
    _row(ctx, ctx.inbox / "b.m4a", "Beethoven", "Symphony No. 6", genre="Classical")
    ClassicalComposerStage().run(ctx)
    artists = {r[0] for r in ctx.conn.execute("SELECT artist FROM archive")}
    assert artists == {"Ludwig Van Beethoven"}


def test_a_filing_ruling_still_applies_whatever_the_capitals(tmp_path):
    ruling = {"Glenn Miller and His Orchestra": "Glenn Miller"}
    as_ruled = library_relpath(
        "Glenn Miller and His Orchestra", None, "Jazz", "A", "Moonlight Serenade", ".m4a", ruling
    )
    capitalised = library_relpath(
        "Glenn Miller And His Orchestra", None, "Jazz", "A", "Moonlight Serenade", ".m4a", ruling
    )
    assert capitalised.parent == as_ruled.parent, "the ruling was missed because of a capital"


def test_organize_refiles_under_the_new_capitals_and_clears_the_old_folder(tmp_path):
    ctx = _ctx(tmp_path)
    old = ctx.config.alac_archive / "Rock" / "Dead or Alive" / "Album" / "Dead or Alive - Song.m4a"
    _row(ctx, old, "Dead Or Alive", "Song")
    ctx.conn.execute(
        "UPDATE archive SET canonicalized_at = datetime('now'), canon_action = 'PASSTHROUGH', "
        "finalized_at = datetime('now')"
    )
    ctx.conn.commit()
    OrganizeStage().run(ctx)
    (fp,) = ctx.conn.execute("SELECT file_path FROM archive").fetchone()
    new = ctx.config.alac_archive / "Rock" / "Dead Or Alive" / "Album" / "Dead Or Alive - Song.m4a"
    assert Path(fp) == new and new.is_file()
    assert not (ctx.config.alac_archive / "Rock" / "Dead or Alive").exists(), (
        "the emptied old-capitals folder was left behind"
    )


@pytest.mark.parametrize(
    "album", ["RAM", "OU812", "CSN", "X&Y", "BEYONCÉ", "DAMN.", "E=MC²", "SOUR"]
)
def test_an_album_written_in_capitals_keeps_them(tmp_path, album):
    """Pre-merge review of #190: the ALL-CAPS repair flattened all 23 live
    ones ("Ram", "Ou812"), undoing Grey's 2026-10-06 fixes."""
    assert _normalized(tmp_path, "Paul McCartney", "Too Many People", album=album)[2] == album


def test_album_names_settle_with_sanitize_and_the_spelling_step_between():
    """Act 1's real order: Normalize, Sanitize, then one spelling per album.
    A second pass changes nothing (pre-merge review of #190)."""
    from musaeus.album_spelling import plan_album_spelling
    from musaeus.stages.normalize import stored_album
    from musaeus.stages.sanitize import sanitize_value

    raw = [("Gary Moore", "Still Got The Blues & More", True), ("Gary Moore", "still got the blues and more", False),
           ("Lynyrd Skynyrd", "Pronounced ’Lĕh-’nérd ’Skin-’nérd", True),
           ("Lynyrd Skynyrd", "Pronounced 'Lĕh-'nérd 'Skin-'nérd", False)]  # fmt: skip

    def one_pass(rows):
        rows = [(a, sanitize_value(stored_album(al)) or stored_album(al), f) for a, al, f in rows]
        recs = [{"file_path": f"{a}/{i}", "artist": a, "album": al, "finalized_at": "x" if f else None,
                 "mb_artist_name": None} for i, (a, al, f) in enumerate(rows)]  # fmt: skip
        best = {r["file_path"]: b for r, b in plan_album_spelling(recs)}
        return [(a, best.get(f"{a}/{i}", al), f) for i, (a, al, f) in enumerate(rows)]

    once = one_pass(raw)
    assert len({al for a, al, _ in once if a == "Gary Moore"}) == 1
    assert len({al for a, al, _ in once if a == "Lynyrd Skynyrd"}) == 1
    assert one_pass(once) == once
