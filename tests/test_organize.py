"""
Tests for OrganizeStage — rename/move CATALOGUED files into Artist/Album/.

Uses real files on disk (not mocked) because this stage's core correctness
claim is specifically about disk/DB staying in sync through a real
rename+DB-update sequence -- a prior bug here (row["rowid"] raising
IndexError on every live call, since archive.id is an INTEGER PRIMARY
KEY and SQLite exposes it under its real column name, not "rowid", once
selected alongside other columns) went undetected because no test here
exercised the actual live rename path end-to-end.
"""

from pathlib import Path

import pytest

from musaeus.config import MusicConfig
from musaeus.context import RunContext
from musaeus.db import open_db, upsert_archive
from musaeus.stages.organize import (
    OrganizeStage,
    build_track_filename,
    destination_root,
    sanitize_path_component,
    strip_track_number_prefix,
    unique_path,
)


@pytest.fixture
def cfg(tmp_path: Path) -> MusicConfig:
    return MusicConfig(
        vault_root=tmp_path,
        inbox=tmp_path / "INBOX",
        staging=tmp_path / "STAGING",
        quarantine=tmp_path / "QUARANTINE",
        runs_root=tmp_path / "RUNS",
        meta_dir=tmp_path / "MetaData",
        alac_library=tmp_path / "ALAC-Library",
        db_path=tmp_path / "musaeus.db",
    )


@pytest.fixture
def ctx(cfg: MusicConfig) -> RunContext:
    cfg.inbox.mkdir(parents=True, exist_ok=True)
    conn = open_db(cfg.db_path)
    return RunContext.new(cfg, conn, dry_run=False)


@pytest.fixture
def ctx_dry(cfg: MusicConfig) -> RunContext:
    cfg.inbox.mkdir(parents=True, exist_ok=True)
    conn = open_db(cfg.db_path)
    return RunContext.new(cfg, conn, dry_run=True)


def _make_track(ctx: RunContext, relpath: str, artist: str, album: str, title: str) -> Path:
    """Create a real (empty) file under INBOX and register it as CATALOGUED."""
    path = ctx.inbox / relpath
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"FAKE AUDIO DATA")
    upsert_archive(
        ctx.conn,
        {
            "file_path": str(path),
            "status": "CATALOGUED",
            "artist": artist,
            "album": album,
            "title": title,
        },
    )
    ctx.conn.commit()
    return path


# ── Naming helpers ────────────────────────────────────────────────────────────


class TestNamingHelpers:
    def test_strip_track_number_prefix(self):
        assert strip_track_number_prefix("171. Song Title") == "Song Title"
        assert strip_track_number_prefix("01 - Title") == "Title"
        assert strip_track_number_prefix("No Prefix Here") == "No Prefix Here"

    def test_sanitize_path_component_forbidden_chars(self):
        result = sanitize_path_component("AC/DC: Greatest?")
        assert "/" not in result
        assert ":" not in result
        assert "?" not in result

    def test_build_track_filename(self):
        assert (
            build_track_filename("The Beatles", "Come Together", ".m4a")
            == "The Beatles - Come Together.m4a"
        )

    def test_unique_path_no_collision(self, tmp_path):
        target = tmp_path / "file.m4a"
        assert unique_path(target) == target

    def test_unique_path_with_collision(self, tmp_path):
        target = tmp_path / "file.m4a"
        target.write_bytes(b"x")
        result = unique_path(target)
        assert result == tmp_path / "file (2).m4a"


# ── Live run: the actual regression this file guards against ────────────────


class TestOrganizeRunLive:
    def test_move_to_artist_album_folder(self, ctx):
        """
        The core regression test: a real file, a real CATALOGUED row, a
        real (not dry-run) OrganizeStage.run() call. Before the fix, this
        raised IndexError inside _apply_rename() the moment it tried to
        read row["rowid"] -- every live organize run crashed on the first
        file that needed a move, silently reverting nothing (the crash
        happened before any disk write in that particular bug, but the
        point of this test is that the stage must complete, not just not
        corrupt anything).
        """
        track = _make_track(ctx, "flat_name.m4a", "Test Artist", "Test Album", "Song One")

        result = OrganizeStage().execute(ctx)

        assert result.success is True
        assert result.files_changed == 1

        expected = ctx.inbox / "Test Artist" / "Test Album" / "Test Artist - Song One.m4a"
        assert expected.exists()
        assert not track.exists()

        # DB must point at the new location, not the old one.
        row = ctx.conn.execute(
            "SELECT file_path FROM archive WHERE artist='Test Artist' AND title='Song One'"
        ).fetchone()
        assert row["file_path"] == str(expected)

    def test_move_multiple_files_same_run(self, ctx):
        """Two files needing a move in the same run -- both must succeed,
        confirming the id lookup works across more than one row, not just
        coincidentally for a single-row table."""
        _make_track(ctx, "a.m4a", "Artist A", "Album A", "Title A")
        _make_track(ctx, "b.m4a", "Artist B", "Album B", "Title B")

        result = OrganizeStage().execute(ctx)

        assert result.success is True
        assert result.files_changed == 2
        assert (ctx.inbox / "Artist A" / "Album A" / "Artist A - Title A.m4a").exists()
        assert (ctx.inbox / "Artist B" / "Album B" / "Artist B - Title B.m4a").exists()

    def test_rename_only_same_directory(self, ctx):
        """When the target dir already matches, this is a pure rename
        (current_path.parent == target_path.parent branch), not a move --
        exercises the other _apply_rename call site."""
        target_dir = ctx.inbox / "Test Artist" / "Test Album"
        target_dir.mkdir(parents=True)
        path = target_dir / "wrong_name.m4a"
        path.write_bytes(b"DATA")
        upsert_archive(
            ctx.conn,
            {
                "file_path": str(path),
                "status": "CATALOGUED",
                "artist": "Test Artist",
                "album": "Test Album",
                "title": "Right Name",
            },
        )
        ctx.conn.commit()

        result = OrganizeStage().execute(ctx)

        assert result.success is True
        expected = target_dir / "Test Artist - Right Name.m4a"
        assert expected.exists()
        assert not path.exists()

    def test_already_organized_file_skipped(self, ctx):
        """A file already at its correct final path is a no-op, not an
        error and not a redundant move."""
        target_dir = ctx.inbox / "Test Artist" / "Test Album"
        target_dir.mkdir(parents=True)
        path = target_dir / "Test Artist - Already Right.m4a"
        path.write_bytes(b"DATA")
        upsert_archive(
            ctx.conn,
            {
                "file_path": str(path),
                "status": "CATALOGUED",
                "artist": "Test Artist",
                "album": "Test Album",
                "title": "Already Right",
            },
        )
        ctx.conn.commit()

        result = OrganizeStage().execute(ctx)

        assert result.success is True
        assert result.files_changed == 0
        assert path.exists()

    def test_missing_file_reported_as_error_not_crash(self, ctx):
        """A CATALOGUED row whose file vanished from disk must be reported
        via files_errored/result.errors, not crash the stage."""
        missing_path = ctx.inbox / "gone.m4a"
        upsert_archive(
            ctx.conn,
            {
                "file_path": str(missing_path),
                "status": "CATALOGUED",
                "artist": "Ghost Artist",
                "album": "Ghost Album",
                "title": "Ghost Title",
            },
        )
        ctx.conn.commit()

        result = OrganizeStage().execute(ctx)

        assert result.files_errored == 1
        assert any("missing" in e.lower() for e in result.errors)


# ── Dry run ───────────────────────────────────────────────────────────────────


class TestOrganizeDryRun:
    def test_dry_run_does_not_move_file(self, ctx_dry):
        track = _make_track(ctx_dry, "flat_name.m4a", "Test Artist", "Test Album", "Song One")

        result = OrganizeStage().execute(ctx_dry)

        assert result.dry_run is True
        assert result.files_changed == 1
        # Nothing should have actually moved.
        assert track.exists()
        expected = ctx_dry.inbox / "Test Artist" / "Test Album" / "Test Artist - Song One.m4a"
        assert not expected.exists()

        row = ctx_dry.conn.execute(
            "SELECT file_path FROM archive WHERE title='Song One'"
        ).fetchone()
        assert row["file_path"] == str(track)


# ── Root containment: the 10,660-file hazard ──────────────────────────────────
#
# Every target used to be built under ctx.inbox while the query selects
# status='CATALOGUED'. Measured 2026-08-24: that moves 10,660 of 10,660
# catalogued files OUT of ALAC-Library and into the INBOX, where the pipeline
# treats the whole finalized library as new arrivals.
#
# The assertion that catches this is not "did it move" -- it moved, briskly --
# but "is it still somewhere this run can reach" (finding #14).


def _make_track_under(
    ctx: RunContext, root: Path, relpath: str, artist: str, album: str, title: str
) -> Path:
    """A real file and a CATALOGUED row under an arbitrary root."""
    path = root / relpath
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"FAKE AUDIO DATA")
    upsert_archive(
        ctx.conn,
        {
            "file_path": str(path),
            "status": "CATALOGUED",
            "artist": artist,
            "album": album,
            "title": title,
        },
    )
    ctx.conn.commit()
    return path


class TestDestinationRoot:
    def test_picks_the_root_the_file_is_under(self, tmp_path):
        lib, inbox = tmp_path / "ALAC-Library", tmp_path / "INBOX"
        assert destination_root(lib / "a" / "b.m4a", [lib, inbox]) == lib
        assert destination_root(inbox / "a" / "b.m4a", [lib, inbox]) == inbox

    def test_a_file_under_no_root_gets_none(self, tmp_path):
        lib, inbox = tmp_path / "ALAC-Library", tmp_path / "INBOX"
        outside = tmp_path / "Projects" / "Antonio Vivaldi" / "x.m4a"
        assert destination_root(outside, [lib, inbox]) is None

    def test_the_most_specific_root_wins(self, tmp_path):
        """A nested root must beat its parent regardless of list order."""
        vault = tmp_path / "vault"
        lib = vault / "ALAC-Library"
        target = lib / "Artist" / "x.m4a"
        assert destination_root(target, [vault, lib]) == lib
        assert destination_root(target, [lib, vault]) == lib

    def test_an_unresolvable_path_is_none_not_an_exception(self, tmp_path):
        assert destination_root(Path("relative/x.m4a"), [tmp_path]) is None


class TestOrganizeStaysInsideItsRoot:
    def test_a_library_file_is_organized_within_the_library(self, ctx):
        """THE regression. A catalogued file must never land in the INBOX."""
        ctx.alac_library.mkdir(parents=True, exist_ok=True)
        src = _make_track_under(
            ctx, ctx.alac_library, "flat.m4a", "Test Artist", "Test Album", "Song One"
        )

        OrganizeStage().run(ctx)

        expected = ctx.alac_library / "Test Artist" / "Test Album" / "Test Artist - Song One.m4a"
        assert expected.exists(), "file left the library"
        assert not src.exists()

        # The real assertion: still reachable from a known root. Compared as
        # "under the library", not "root is exactly the library" -- _roots is
        # computed live, so a batch directory created by this very run is a
        # legitimate deeper answer.
        root = destination_root(expected, OrganizeStage._roots(ctx))
        assert root is not None
        assert str(root).startswith(str(ctx.alac_library))

        moved_into_inbox = list(ctx.inbox.rglob("*.m4a"))
        assert moved_into_inbox == [], f"files escaped into the INBOX: {moved_into_inbox}"

        row = ctx.conn.execute(
            "SELECT file_path FROM archive WHERE title = 'Song One'"
        ).fetchone()
        assert Path(row["file_path"]) == expected

    def test_an_inbox_file_is_still_organized_within_the_inbox(self, ctx):
        """The fix must not push INBOX files into the library either."""
        src = _make_track(ctx, "flat.m4a", "Test Artist", "Test Album", "Song One")
        OrganizeStage().run(ctx)

        expected = ctx.inbox / "Test Artist" / "Test Album" / "Test Artist - Song One.m4a"
        assert expected.exists()
        assert not src.exists()
        assert list(ctx.alac_library.rglob("*.m4a")) == []

    def test_a_file_outside_every_root_is_refused_not_relocated(self, ctx):
        """Finding #14: never invent a root for a file that is under none."""
        outside_root = ctx.vault_root.parent / "Elsewhere"
        stray = _make_track_under(
            ctx, outside_root, "stray.m4a", "Test Artist", "Test Album", "Stray"
        )

        result = OrganizeStage().run(ctx)

        assert stray.exists(), "a file with no safe destination must be left alone"
        assert result.files_errored == 1
        assert any("outside every known root" in e for e in result.errors)
        assert list(ctx.inbox.rglob("*.m4a")) == []
        assert list(ctx.alac_library.rglob("*.m4a")) == []

    def test_a_library_move_does_not_crash_on_logging(self, ctx, caplog):
        """`relative_to(ctx.inbox)` raised for any file outside the INBOX.

        Logger arguments are evaluated eagerly, so this fired BEFORE the move
        -- the traceback, not the relocation, was what a live run hit first.
        """
        ctx.alac_library.mkdir(parents=True, exist_ok=True)
        _make_track_under(
            ctx, ctx.alac_library, "flat.m4a", "Test Artist", "Test Album", "Song One"
        )
        with caplog.at_level("INFO"):
            result = OrganizeStage().run(ctx)
        assert result.success
        assert result.files_errored == 0

    def test_dry_run_moves_nothing_out_of_the_library(self, ctx_dry):
        ctx_dry.alac_library.mkdir(parents=True, exist_ok=True)
        src = _make_track_under(
            ctx_dry, ctx_dry.alac_library, "flat.m4a", "A", "B", "C"
        )
        OrganizeStage().dry_run(ctx_dry)
        assert src.exists()
        assert list(ctx_dry.inbox.rglob("*.m4a")) == []


# ── paths use the sort form, whatever the tag holds ──────────────────────────
#
# The `artist` TAG is migrating to the natural form ("The Stooges") because
# that is what MusicBrainz and every player expect. The PATH must keep the
# sort form ("Stooges, The"), which is the entire reason the convention
# exists. Deriving the path from sort_form rather than from the tag makes
# the on-disk layout identical before and after that migration.


class TestPathsUseTheSortForm:
    def test_a_natural_form_artist_still_files_under_the_sort_form(self, ctx):
        """THE guard on the tag migration: no file moves because of it."""
        _make_track(ctx, "flat.m4a", "The Stooges", "Fun House", "Down on the Street")
        OrganizeStage().run(ctx)

        expected = (
            ctx.inbox / "Stooges, The" / "Fun House"
            / "Stooges, The - Down on the Street.m4a"
        )
        assert expected.exists(), "path must not follow the natural form"
        assert not (ctx.inbox / "The Stooges").exists()

    def test_both_artist_forms_land_on_the_same_path(self, ctx):
        """A library mid-migration holds both; they must not split in two."""
        _make_track(ctx, "a.m4a", "Stooges, The", "Fun House", "Loose")
        _make_track(ctx, "b.m4a", "The Stooges", "Fun House", "Dirt")
        OrganizeStage().run(ctx)

        folder = ctx.inbox / "Stooges, The" / "Fun House"
        assert sorted(f.name for f in folder.glob("*.m4a")) == [
            "Stooges, The - Dirt.m4a",
            "Stooges, The - Loose.m4a",
        ]
        assert not (ctx.inbox / "The Stooges").exists()

    def test_a_stylized_name_is_not_rearranged_into_a_folder(self, ctx):
        """"De La Soul" -> "La Soul, De" was live corruption, 2026-08-16."""
        _make_track(ctx, "c.m4a", "De La Soul", "3 Feet High", "Me Myself and I")
        OrganizeStage().run(ctx)
        assert (ctx.inbox / "De La Soul" / "3 Feet High").is_dir()
        assert not (ctx.inbox / "La Soul, De").exists()

    def test_a_name_with_no_article_is_unaffected(self, ctx):
        _make_track(ctx, "d.m4a", "Dusty Springfield", "Dusty in Memphis", "Son of a Preacher Man")
        OrganizeStage().run(ctx)
        assert (ctx.inbox / "Dusty Springfield" / "Dusty in Memphis").is_dir()


# ── smart-quote normalisation, which was silently a no-op ────────────────────
#
# The three replace() lines in sanitize_path_component were written out by
# hand and the file had lost its non-ASCII characters. Confirmed by AST
# 2026-08-29:
#
#     .replace("'", "'")                 ASCII -> ASCII. A no-op. Twice.
#     .replace(', \'"\').replace(', '"')  a stray `"""` opened a triple-quoted
#                                        string, so this line replaced the
#                                        literal text `, '"').replace(` with `"`
#
# So no curly quote was ever normalised and one line was nonsense, while the
# function looked entirely reasonable. Only the dash line survived intact.
#
# Found by an independent review on a 2026-08-21 base; the defect was still
# live on this branch eight days later.


class TestSmartQuoteNormalisation:
    def test_curly_single_quotes_become_ascii(self):
        assert sanitize_path_component("Hello ‘Cause’") == "Hello 'Cause'"

    def test_curly_double_quotes_are_normalised_then_stripped(self):
        # -> ASCII '"', which _FORBIDDEN_RE then replaces: '"' is not legal
        # in a Windows/ExFAT path.
        assert sanitize_path_component("Say “Hi”") == "Say -Hi-"

    def test_dashes_become_hyphens(self):
        assert sanitize_path_component("A – B — C − D") == "A - B - C - D"

    def test_backtick_becomes_an_apostrophe(self):
        assert sanitize_path_component("back`tick") == "back'tick"

    def test_a_plain_apostrophe_is_untouched(self):
        assert sanitize_path_component("It's Fine") == "It's Fine"

    def test_a_comma_apostrophe_sequence_survives(self):
        """The mangled line replaced a literal `, '"').replace(` fragment.

        Nothing real contained it, which is why the damage stayed invisible --
        but the normalisation it was supposed to perform never happened.
        """
        assert sanitize_path_component("Hello, 'Cause") == "Hello, 'Cause"

    def test_the_function_no_longer_contains_a_mangled_replace(self):
        """Pins the parse itself, since the source is what went wrong.

        A string comparison would pass against a file that still parsed into
        nonsense; this asserts what Python actually built.
        """
        import ast
        import inspect

        from musaeus.stages import organize as _organize

        tree = ast.parse(inspect.getsource(_organize.sanitize_path_component))
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "replace"
                and len(node.args) == 2
                and all(isinstance(a, ast.Constant) for a in node.args)
            ):
                old, new = (a.value for a in node.args)
                assert old != new, f"no-op replace still present: {old!r}"
                assert ".replace(" not in str(old), f"mangled literal: {old!r}"


# ── the batch tier is a root of its own ──────────────────────────────────────
#
# FinalizeStage writes ALAC-Library/<batch>/<artist>/<album>/. Treating
# ALAC-Library itself as the root makes Organize rebuild every path as
# ALAC-Library/<artist>/<album>/ -- flattening the batch directory and moving
# the whole finalized library. Measured on the real layout 2026-08-30, just
# before wiring this stage into DEFAULT_PIPELINE: one finalized file in, one
# move out. That is 10,588 moves on the live vault.


class TestBatchTierIsPreserved:
    def _finalized(self, ctx, batch, artist, album, title):
        path = ctx.alac_library / batch / artist / album / f"{artist} - {title}.m4a"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"FAKE AUDIO DATA")
        upsert_archive(ctx.conn, {
            "file_path": str(path), "status": "CATALOGUED",
            "artist": artist, "album": album, "title": title,
        })
        ctx.conn.commit()
        return path

    def test_an_already_organized_finalized_file_is_left_alone(self, ctx):
        ctx.alac_library.mkdir(parents=True, exist_ok=True)
        src = self._finalized(ctx, "2026-08-27A", "Cranberries, The", "Unsorted", "Zombie")

        OrganizeStage().run(ctx)

        assert src.exists(), "the batch directory was flattened"
        assert not (ctx.alac_library / "Cranberries, The").exists()

    def test_a_messy_file_is_tidied_inside_its_own_batch(self, ctx):
        ctx.alac_library.mkdir(parents=True, exist_ok=True)
        batch = ctx.alac_library / "2026-08-27A"
        batch.mkdir(parents=True, exist_ok=True)
        flat = batch / "flat.m4a"
        flat.write_bytes(b"FAKE AUDIO DATA")
        upsert_archive(ctx.conn, {
            "file_path": str(flat), "status": "CATALOGUED",
            "artist": "Weezer", "album": "Blue Album", "title": "Buddy Holly",
        })
        ctx.conn.commit()

        OrganizeStage().run(ctx)

        assert (batch / "Weezer" / "Blue Album" / "Weezer - Buddy Holly.m4a").exists()
        assert not (ctx.alac_library / "Weezer").exists(), "escaped its batch"

    def test_two_batches_do_not_merge(self, ctx):
        """Each batch organizes independently; neither absorbs the other."""
        ctx.alac_library.mkdir(parents=True, exist_ok=True)
        a = self._finalized(ctx, "2026-08-27A", "Weezer", "Blue Album", "Undone")
        b = self._finalized(ctx, "2026-08-28B", "Weezer", "Blue Album", "Say It Aint So")

        OrganizeStage().run(ctx)

        assert a.exists() and b.exists()
        assert not (ctx.alac_library / "Weezer").exists()

    def test_an_artist_directory_is_not_mistaken_for_a_batch(self, ctx):
        """A library shaped ALAC-Library/<artist>/<album>/ must still work.

        An earlier draft enumerated EVERY child of ALAC-Library as a root, so
        the artist directory became the root and Organize nested artist
        inside artist. Batches are matched on FinalizeStage's own
        YYYY-MM-DD[suffix] shape instead.
        """
        ctx.alac_library.mkdir(parents=True, exist_ok=True)
        path = ctx.alac_library / "Weezer" / "Blue Album" / "Weezer - Undone.m4a"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"FAKE AUDIO DATA")
        upsert_archive(ctx.conn, {
            "file_path": str(path), "status": "CATALOGUED",
            "artist": "Weezer", "album": "Blue Album", "title": "Undone",
        })
        ctx.conn.commit()

        OrganizeStage().run(ctx)

        assert path.exists(), "already correct; should not have moved"
        assert not (ctx.alac_library / "Weezer" / "Weezer").exists(), "nested artist"

    def test_review_folders_beside_the_batches_are_not_roots(self, ctx):
        """DUPES_MOVED_FOR_REVIEW sits next to the batches and is not one."""
        from musaeus.stages.organize import _BATCH_DIR_RE

        assert _BATCH_DIR_RE.match("2026-08-27A")
        assert not _BATCH_DIR_RE.match("DUPES_MOVED_FOR_REVIEW")
        assert not _BATCH_DIR_RE.match("TRIBUTE_REMOVED_FOR_REVIEW")


# ── deliberate set-aside folders are not content to tidy ─────────────────────
#
# DUPES_MOVED_FOR_REVIEW and TRIBUTE_REMOVED_FOR_REVIEW sit beside the batch
# directories under ALAC-Library. Files were deliberately moved OUT of the
# library into them. Because they are not batches, _BATCH_DIR_RE does not
# match, so destination_root fell through to ALAC-Library itself and Organize
# "tidied" a quarantined file into ALAC-Library/<Artist>/<Album>/ --
# re-merging a duplicate dupe_resolver had deliberately set aside.
#
# Reproduced 2026-08-31: one file in, one move out. The console soft reset
# resets every row to PENDING with no WHERE clause, so that is all it takes
# to walk the whole review folder back into the library.


class TestSetAsideFoldersAreLeftAlone:
    @pytest.mark.parametrize(
        "folder", ["DUPES_MOVED_FOR_REVIEW", "TRIBUTE_REMOVED_FOR_REVIEW", "QUARANTINE"]
    )
    def test_a_set_aside_file_is_never_moved(self, ctx, folder):
        ctx.alac_library.mkdir(parents=True, exist_ok=True)
        src = ctx.alac_library / folder / "Weezer - Undone.m4a"
        src.parent.mkdir(parents=True, exist_ok=True)
        src.write_bytes(b"FAKE AUDIO DATA")
        upsert_archive(ctx.conn, {
            "file_path": str(src), "status": "CATALOGUED",
            "artist": "Weezer", "album": "Blue Album", "title": "Undone",
        })
        ctx.conn.commit()

        OrganizeStage().run(ctx)

        assert src.exists(), f"a file in {folder} was moved"
        assert not (ctx.alac_library / "Weezer").exists(), "re-merged into the library"

    def test_a_real_batch_file_is_still_organized(self, ctx):
        """The guard must not stop Organize doing its job."""
        ctx.alac_library.mkdir(parents=True, exist_ok=True)
        batch = ctx.alac_library / "2026-08-27A"
        batch.mkdir(parents=True, exist_ok=True)
        flat = batch / "flat.m4a"
        flat.write_bytes(b"FAKE AUDIO DATA")
        upsert_archive(ctx.conn, {
            "file_path": str(flat), "status": "CATALOGUED",
            "artist": "Weezer", "album": "Blue Album", "title": "Undone",
        })
        ctx.conn.commit()

        OrganizeStage().run(ctx)
        assert (batch / "Weezer" / "Blue Album" / "Weezer - Undone.m4a").exists()


def test_a_path_component_never_exceeds_the_filesystem_byte_limit() -> None:
    """Linux caps each path component at 255 BYTES, whatever the total path
    length. Nothing enforced it, so the 388-byte 24-artist credit on
    "Why Me (Live)" raised OSError 36 from unique_path()'s exists() check
    and took dupe-resolver down mid-run on 2026-09-03. Six stages build
    paths through these helpers, so the cap belongs here.

    Asserts bytes, not characters: s[:255] counts codepoints and would
    still overflow for any non-ASCII name."""
    from musaeus.stages.organize import (
        build_track_filename,
        sanitize_path_component,
        truncate_to_bytes,
    )

    long_artist = (
        "Kris Kristofferson, Alison Krauss, Reba McEntire, Lady A, Willie Nelson, "
        "Jon Randall, Larry Gatlin, Jessi Alexander, Jessi Colter, Jack Ingram, "
        "Buddy Miller, Martina McBride, Ryan Bingham, Lee Ann Womack, Jennifer "
        "Nettles, Rosanne Cash, Emmylou Harris, Rodney Crowell, Dierks Bentley & "
        "The Travelin' McCourys, Darius Rucker, Jamey Johnson, Hank Williams Jr., "
        "Eric Church, Shooter Jennings"
    )
    assert len(long_artist.encode("utf-8")) > 255, "fixture must actually be over the limit"

    assert len(sanitize_path_component(long_artist).encode("utf-8")) <= 255
    assert len(build_track_filename(long_artist, "Why Me (Live)", ".flac").encode("utf-8")) <= 255
    # the extension survives -- a truncated one would break codec routing
    assert build_track_filename(long_artist, "Why Me (Live)", ".flac").endswith(".flac")

    # multi-byte characters are cut on a character boundary, never mid-sequence
    cut = truncate_to_bytes("é" * 400)
    assert len(cut.encode("utf-8")) <= 255
    assert cut == "é" * (255 // 2)

    # short names are returned untouched
    assert sanitize_path_component("Neil Young") == "Neil Young"
    assert truncate_to_bytes("Neil Young") == "Neil Young"


def test_the_cap_survives_the_collision_it_creates(tmp_path: Path) -> None:
    """Capping at exactly 255 put the ENAMETOOLONG crash straight back.

    build_track_filename returned 255 bytes, unique_path appended " (2)",
    and the os.stat() inside .exists() raised errno 36 -- pathlib does not
    swallow it, _IGNORED_ERRNOS is (ENOENT, ENOTDIR, EBADF, ELOOP). Worse,
    truncation MANUFACTURES that collision: every over-long credit truncates
    to the same prefix, so for exactly the inputs needing the cap, colliding
    is the normal case. Found in review 2026-09-04, hours after the cap
    shipped."""
    from musaeus.stages.organize import build_track_filename, unique_path

    name = build_track_filename("A" * 400, "Why Me (Live)", ".flac")
    assert len(name.encode("utf-8")) <= 255 - 6, "no headroom left for ' (999)'"

    first = tmp_path / name
    first.write_bytes(b"x")

    # deep enough that the counter goes two digits and the budget shrinks
    for _ in range(12):
        out = unique_path(first)          # must not raise OSError 36
        assert len(out.name.encode("utf-8")) <= 255
        out.write_bytes(b"x")


def test_unique_path_shortens_whatever_it_is_handed(tmp_path: Path) -> None:
    """Seven stages call unique_path. It cannot rely on its caller having
    left room -- finalize, organize, tribute_quarantine, various_artists_fix,
    classical_composer and deny_list all call it with no try/except."""
    from musaeus.stages.organize import unique_path

    over = tmp_path / ("B" * 250 + ".m4a")     # already at the limit
    over.write_bytes(b"x")
    out = unique_path(over)
    assert len(out.name.encode("utf-8")) <= 255
    assert out.name.endswith(".m4a")


def test_a_multibyte_name_is_never_cut_mid_codepoint(tmp_path: Path) -> None:
    from musaeus.stages.organize import build_track_filename, unique_path

    name = build_track_filename("é" * 400, "é" * 40, ".m4a")
    assert name.encode("utf-8").decode("utf-8") == name
    f = tmp_path / name
    f.write_bytes(b"x")
    out = unique_path(f)
    assert len(out.name.encode("utf-8")) <= 255
    assert out.name.encode("utf-8").decode("utf-8") == out.name
