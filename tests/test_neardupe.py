"""
Tests for NearDupeStage — metadata-based near-duplicate detection.

Requires rapidfuzz; tests skip if not installed.
"""

from pathlib import Path

import pytest

from musaeus.config import MusicConfig
from musaeus.context import RunContext
from musaeus.db import open_db, upsert_archive
from musaeus.stages.neardupe import NearDupeStage, _group_id, _is_different_piece, _normalise

# Skip all tests if rapidfuzz is not available
rapidfuzz = pytest.importorskip("rapidfuzz")


@pytest.fixture
def cfg(tmp_path: Path) -> MusicConfig:
    # Create MetaData dir with an empty artist_canon.tsv
    meta_dir = tmp_path / "MetaData"
    meta_dir.mkdir()
    (meta_dir / "artist_canon.tsv").write_text("", encoding="utf-8")
    return MusicConfig(
        vault_root=tmp_path,
        inbox=tmp_path / "INBOX",
        staging=tmp_path / "STAGING",
        quarantine=tmp_path / "QUARANTINE",
        runs_root=tmp_path / "RUNS",
        meta_dir=meta_dir,
        alac_library=tmp_path / "ALAC-Library",
        db_path=tmp_path / "musaeus.db",
    )


@pytest.fixture
def ctx(cfg: MusicConfig) -> RunContext:
    conn = open_db(cfg.db_path)
    return RunContext.new(cfg, conn, dry_run=False)


@pytest.fixture
def ctx_dry(cfg: MusicConfig) -> RunContext:
    conn = open_db(cfg.db_path)
    return RunContext.new(cfg, conn, dry_run=True)


def _insert_catalogued(ctx: RunContext, file_path: str, artist: str, title: str) -> None:
    upsert_archive(
        ctx.conn,
        {
            "file_path": file_path,
            "status": "CATALOGUED",
            "artist": artist,
            "title": title,
            "bitrate": 320000,
            "size_bytes": 5000000,
        },
    )
    ctx.conn.commit()


# ── _normalise helper ─────────────────────────────────────────────────────────


class TestNormalise:
    def test_lowercase(self):
        assert _normalise("HELLO") == "hello"

    def test_strip_punctuation(self):
        assert _normalise("rock'n'roll") == "rock n roll"

    def test_collapse_whitespace(self):
        assert _normalise("  hello   world  ") == "hello world"

    def test_strip_leading_the(self):
        assert _normalise("The Beatles") == "beatles"

    def test_unicode_nfd(self):
        # Accented characters normalised
        result = _normalise("cafe\u0301")  # café with combining accent
        assert "cafe" in result


# ── _group_id helper ──────────────────────────────────────────────────────────


class TestGroupId:
    def test_stable(self):
        gid1 = _group_id("/a.flac", "/b.flac")
        gid2 = _group_id("/a.flac", "/b.flac")
        assert gid1 == gid2

    def test_order_independent(self):
        gid1 = _group_id("/a.flac", "/b.flac")
        gid2 = _group_id("/b.flac", "/a.flac")
        assert gid1 == gid2

    def test_prefix(self):
        gid = _group_id("/x.flac", "/y.flac")
        assert gid.startswith("near_")


# ── Validate ──────────────────────────────────────────────────────────────────


class TestNearDupeValidate:
    def test_validate_passes_with_rapidfuzz(self, ctx, cfg):
        NearDupeStage().validate(ctx)


# ── Dry run ───────────────────────────────────────────────────────────────────


class TestNearDupeDryRun:
    def test_dry_run_detects_near_dupes(self, ctx_dry, cfg, tmp_path):
        # Two tracks, same artist, very similar titles
        _insert_catalogued(ctx_dry, str(tmp_path / "a.flac"), "The Beatles", "Yesterday")
        _insert_catalogued(ctx_dry, str(tmp_path / "b.flac"), "The Beatles", "Yesterday (Remaster)")

        result = NearDupeStage().execute(ctx_dry)
        assert result.dry_run is True
        # Should detect the near-dupe pair
        assert result.files_changed >= 1

    def test_dry_run_no_db_writes(self, ctx_dry, cfg, tmp_path):
        _insert_catalogued(ctx_dry, str(tmp_path / "a.flac"), "Artist", "Song A")
        _insert_catalogued(ctx_dry, str(tmp_path / "b.flac"), "Artist", "Song A Remix")

        NearDupeStage().execute(ctx_dry)

        count = ctx_dry.conn.execute("SELECT COUNT(*) FROM duplicates").fetchone()[0]
        assert count == 0

    def test_dry_run_no_matches(self, ctx_dry, cfg, tmp_path):
        _insert_catalogued(ctx_dry, str(tmp_path / "a.flac"), "Artist A", "Totally Different")
        _insert_catalogued(ctx_dry, str(tmp_path / "b.flac"), "Artist B", "Completely Other")

        result = NearDupeStage().execute(ctx_dry)
        assert result.files_changed == 0
        assert any("No new near-duplicates" in n for n in result.notes)


# ── Run ───────────────────────────────────────────────────────────────────────


class TestNearDupeRun:
    def test_run_writes_to_duplicates_table(self, ctx, cfg, tmp_path):
        _insert_catalogued(ctx, str(tmp_path / "a.flac"), "Pink Floyd", "Comfortably Numb")
        _insert_catalogued(ctx, str(tmp_path / "b.flac"), "Pink Floyd", "Comfortably Numb (Live)")

        result = NearDupeStage().execute(ctx)
        assert result.files_changed >= 1

        dupes = ctx.conn.execute("SELECT * FROM duplicates WHERE duplicate_type='NEAR'").fetchall()
        assert len(dupes) >= 2  # Both files in the group

    def test_run_different_artists_not_matched(self, ctx, cfg, tmp_path):
        _insert_catalogued(ctx, str(tmp_path / "a.flac"), "Artist A", "Same Title")
        _insert_catalogued(ctx, str(tmp_path / "b.flac"), "Artist B", "Same Title")

        NearDupeStage().execute(ctx)
        # Different artists → no match (unless artists are very similar)
        dupes = ctx.conn.execute("SELECT * FROM duplicates WHERE duplicate_type='NEAR'").fetchall()
        assert len(dupes) == 0

    def test_run_idempotent(self, ctx, cfg, tmp_path):
        _insert_catalogued(ctx, str(tmp_path / "a.flac"), "Artist", "Song Title")
        _insert_catalogued(ctx, str(tmp_path / "b.flac"), "Artist", "Song Title Remix")

        NearDupeStage().execute(ctx)
        count1 = ctx.conn.execute("SELECT COUNT(*) FROM duplicates").fetchone()[0]

        # Run again — should not create duplicates
        NearDupeStage().execute(ctx)
        count2 = ctx.conn.execute("SELECT COUNT(*) FROM duplicates").fetchone()[0]
        assert count2 == count1

    def test_run_skips_exact_dupes(self, ctx, cfg, tmp_path):
        # Pre-insert an EXACT duplicate
        _insert_catalogued(ctx, str(tmp_path / "a.flac"), "Artist", "Song")
        _insert_catalogued(ctx, str(tmp_path / "b.flac"), "Artist", "Song (same)")
        ctx.conn.execute(
            "INSERT INTO duplicates (group_id, file_path, duplicate_type, confidence, run_id) VALUES (?, ?, 'EXACT', 1.0, ?)",
            ("dup_exact", str(tmp_path / "a.flac"), ctx.run_id),
        )
        ctx.conn.commit()

        NearDupeStage().execute(ctx)
        # a.flac is in exact duplicates → should be skipped in neardupe
        # The pair should not be flagged since a.flac is already EXACT
        near_dupes = ctx.conn.execute(
            "SELECT * FROM duplicates WHERE duplicate_type='NEAR'"
        ).fetchall()
        assert len(near_dupes) == 0

    def test_run_empty_archive(self, ctx, cfg):
        result = NearDupeStage().execute(ctx)
        assert result.files_processed == 0
        assert result.files_changed == 0


class TestAStalePairFoundAgain:
    def test_a_partner_whose_row_has_gone_is_added_back(self, ctx, tmp_path):
        """2026-10-25 (Prince "Sign O' The Times"): a stale pair had lost one
        member's row; found again, only the row left was reopened, and the
        resolver moved that lone library copy with no keeper. Both rows open."""
        a, b = str(tmp_path / "a.flac"), str(tmp_path / "b.flac")
        _insert_catalogued(ctx, a, "The Beatles", "Yesterday")
        _insert_catalogued(ctx, b, "The Beatles", "Yesterday (Remaster)")
        gid = _group_id(a, b)
        ctx.conn.execute(
            "INSERT INTO duplicates (group_id, file_path, duplicate_type, confidence, run_id, status) "
            "VALUES (?, ?, 'NEAR', 0.9, 'old', 'stale')",
            (gid, a),
        )
        ctx.conn.commit()
        NearDupeStage().execute(ctx)
        rows = ctx.conn.execute(
            "SELECT file_path, status FROM duplicates WHERE group_id = ? ORDER BY file_path", (gid,)
        ).fetchall()
        assert [tuple(r) for r in rows] == [(a, "pending"), (b, "pending")]


class TestClassicalWorksAreNotDuplicates:
    """Different movements of one work are different pieces of music.

    Found 2026-08-23 in the pending queue: Vivaldi's "Violin Concerto
    No. 1 in G minor, RV 317 - I. Allegro" and "- II. Adagio" were grouped
    as a duplicate pair. They share a long work title, so a stripped
    fuzz.ratio scores them in the 90s -- the threshold cannot separate
    them, only the marker can.

    A classical title carries TWO independent identifiers and conflating
    them was the first version's bug: a work number (No./Op./RV/BWV) says
    WHICH PIECE, a movement marker (I./II./Act 2) says WHICH PART. The
    first attempt matched whichever appeared first and returned the work
    number for both movements, leaving the pair grouped -- exactly the
    case it was written for.
    """

    def test_different_movements_of_one_work_are_different_pieces(self):
        assert _is_different_piece(
            "Violin Concerto No. 1 in G minor, RV 317 - I. Allegro",
            "Violin Concerto No. 1 in G minor, RV 317 - II. Adagio",
        )

    def test_different_work_numbers_are_different_pieces(self):
        assert _is_different_piece("Symphony No. 5", "Symphony No. 9")

    def test_different_acts_are_different_pieces(self):
        assert _is_different_piece("Serse - Act 1 Ombra mai fu", "Serse - Act 2 Aria")

    def test_every_marker_counts_not_only_the_first(self):
        assert _is_different_piece("Part 1, RV 269 - I. Allegro", "Part 1, RV 269 - II. Allegro")
        assert _is_different_piece("Part 1 - I. Allegro", "Part 2 - I. Allegro")
        # One track holding two movements is not the one movement (live replay).
        assert _is_different_piece(
            "Water Music Suite No. 1 In F Major, HWV 348 - VIII. Hornpipe",
            "Handel: Water Music Suite No. 1 In F Major, HWV 348: VII. Bourrée - VIII. Hornpipe",
        )

    def test_the_same_movement_remastered_is_still_a_duplicate(self):
        """The guard must not block genuine version-variant pairs."""
        assert not _is_different_piece(
            "Water Music Suite No. 1 - Air", "Water Music Suite No. 1 - Air (Remastered)"
        )

    def test_ordinary_pop_titles_are_unaffected(self):
        assert not _is_different_piece("Yesterday", "Yesterday (Remaster)")
        assert not _is_different_piece("Feel Like Making Love", "Feel like Makin' Love")

    def test_one_sided_markers_do_not_assert_a_difference(self):
        """Conservative by design.

        A marker on one side only is the ordinary "Live"/"Remaster" case
        the existing stripping already handles; asserting a difference
        there would block real duplicates.
        """
        assert not _is_different_piece("Symphony No. 5", "Symphony")
        assert not _is_different_piece("Sonata - I. Allegro", "Sonata")


def test_an_original_mix_label_is_a_version_label():
    """Sam & Dave "Hold On I'm Comin' (Original Mix)" was paired with "Hold On, I'm
    Comin'" only by fingerprint, never by name (Odds & Ends, 2026-10-23)."""
    from musaeus.stages.neardupe import _normalise

    a = _normalise("Hold On I'm Comin' (Original Mix)", strip_qualifiers=True)
    assert a == _normalise("Hold On, I'm Comin'", strip_qualifiers=True)


@pytest.mark.parametrize(
    ("a", "b"),
    [
        ("Dancing In The Street (Edit)", "Dancing In The Street (2002 Remaster)"),
        ("Mean Woman Blues - Original", "Mean Woman Blues"),
    ],
)
def test_edit_and_original_are_version_labels(a, b):
    """Same recordings by fingerprint, apart by name (first real batch, 2026-10-23)."""
    from musaeus.stages.acousticid import _same_song
    from musaeus.stages.neardupe import _normalise

    assert _normalise(a, strip_qualifiers=True) == _normalise(b, strip_qualifiers=True)
    assert _same_song(a, b)


def test_edit_and_original_are_labels_never_title_words():
    """ "Original Sin" is not "Sin": the new labels count only as a label."""
    from musaeus.stages.acousticid import _same_song
    from musaeus.stages.neardupe import _normalise

    assert _normalise("Original Sin", strip_qualifiers=True) == "original sin"
    assert _normalise("Edit the Sad Parts", strip_qualifiers=True) == "edit the sad parts"
    assert not _same_song("Original Sin", "Sin")


def test_a_suffix_and_a_bracket_label_both_go():
    """ "Song - Original (Live)": brackets first, then the suffix (Codex review of #232, r3)."""
    from musaeus.stages.neardupe import _normalise

    assert _normalise("Song - Original (Live)", strip_qualifiers=True) == "song"


@pytest.mark.parametrize(
    ("artist", "leader"),
    [
        ("The Jimi Hendrix Experience", "jimi hendrix"),
        ("Bob Seger & The Silver Bullet Band", "bob seger"),
        ("The Band", ""),
        ("Jimi Hendrix", ""),
    ],
)
def test_a_band_named_after_its_leader_is_compared_with_the_leader(artist, leader):
    """Grey, 2026-10-23: "Jimi Hendrix" and "The Jimi Hendrix Experience" are one act."""
    from musaeus.stages.neardupe import _primary_artist_key

    assert _primary_artist_key(artist) == leader


class TestOneCopyPerPiece:
    """Grey, 2026-10-24: one copy per classical piece; two performances of one
    movement are duplicates. Their wordings differ ("Air On The G String" and
    "Air"), so the title score never paired them: 20-odd movements sat in the
    library two to five times (2026-10-26). The catalogue number and the
    movement marker say which piece."""

    #: Rows filed under their composer, as ClassicalComposer files them.
    COMPOSERS = (
        "Johann Sebastian Bach",
        "Antonio Vivaldi",
        "Wolfgang Amadeus Mozart",
        "George Frideric Handel",
    )

    @classmethod
    def _found(cls, ctx_dry, tmp_path, artist, title_a, title_b, durations=(None, None)) -> bool:
        canon = "".join(f"{c.lower()}\t{c}\n" for c in cls.COMPOSERS)
        (ctx_dry.config.meta_dir / "Composer_Canon.tsv").write_text(canon, encoding="utf-8")
        for name, title, seconds in zip(("a", "b"), (title_a, title_b), durations, strict=True):
            path = str(tmp_path / f"{name}.flac")
            _insert_catalogued(ctx_dry, path, artist, title)
            if seconds is not None:
                ctx_dry.conn.execute(
                    "UPDATE archive SET duration = ? WHERE file_path = ?", (seconds, path)
                )
        ctx_dry.conn.commit()
        return NearDupeStage().execute(ctx_dry).files_changed > 0

    def test_same_catalogue_number_and_movement_in_two_wordings(self, ctx_dry, tmp_path):
        assert self._found(
            ctx_dry,
            tmp_path,
            "Johann Sebastian Bach",
            "J.S. Bach - Orchestral Suite No. 3 In D Major, BWV 1068 - II. Air On The G String",
            "Orchestral Suite No. 3 In D Major, BWV 1068 - II. Air",
        )

    def test_the_catalogue_number_outranks_a_concerto_number(self, ctx_dry, tmp_path):
        # The first identifier read "3" on one side and "293" on the other.
        assert self._found(
            ctx_dry,
            tmp_path,
            "Antonio Vivaldi",
            "Concerto No. 3 In F Major, RV 293 L'autunno - II. Adagio Molto",
            "Vivaldi: The Four Seasons: Autumn, Violin Concerto In F Major, RV 293, Op. 8 No. 3: "
            "II. Adagio Molto",
        )

    def test_k_and_kv_are_one_catalogue(self, ctx_dry, tmp_path):
        assert self._found(
            ctx_dry,
            tmp_path,
            "Wolfgang Amadeus Mozart",
            "Symphony No. 40 In G Minor, K. 550 - I. Molto Allegro",
            "Sinfonie g-Moll KV 550: I. Allegro molto",
        )

    def test_another_movement_is_another_piece(self, ctx_dry, tmp_path):
        assert not self._found(
            ctx_dry,
            tmp_path,
            "Antonio Vivaldi",
            "Concerto No. 3 In F Major, RV 293 L'autunno - II. Adagio Molto",
            "Vivaldi: The Four Seasons: Autumn, Violin Concerto In F Major, RV 293, Op. 8 No. 3: "
            "I. Allegro",
        )

    def test_another_catalogue_number_is_another_piece(self, ctx_dry, tmp_path):
        assert not self._found(
            ctx_dry,
            tmp_path,
            "Johann Sebastian Bach",
            "Orchestral Suite No. 3 In D Major, BWV 1068 - II. Air",
            "Orchestral Suite No. 2 In B Minor, BWV 1067 - II. Rondeau",
        )

    def test_without_a_movement_on_both_sides_nothing_is_asserted(self, ctx_dry, tmp_path):
        assert not self._found(
            ctx_dry,
            tmp_path,
            "Johann Sebastian Bach",
            "J.S. Bach - Cantata BWV 208 - Sheep May Safely Graze",
            "Cantata BWV 208 - 9. Schafe Können Sicher Weiden",
        )

    def test_a_shared_part_label_does_not_hide_another_movement(self, ctx_dry, tmp_path):
        # Codex review of #255: only the first marker ("Part 1") was compared.
        assert not self._found(
            ctx_dry,
            tmp_path,
            "Antonio Vivaldi",
            "Part 1, RV 269 - I. Allegro",
            "The Four Seasons, Part 1, RV 269 - II. Largo",
        )

    def test_a_shared_part_label_does_not_merge_movements_at_a_high_score(self, ctx_dry, tmp_path):
        # Codex review of #255, round 2: the title score is 98.6, and the
        # different-piece guard compared only the first marker ("Part 1").
        assert not self._found(
            ctx_dry,
            tmp_path,
            "Antonio Vivaldi",
            "Part 1, RV 269 - I. Allegro",
            "Part 1, RV 269 - II. Allegro",
        )

    def test_a_performer_row_never_takes_the_shortcut(self, ctx_dry, tmp_path):
        # Codex review of #255, round 3: K. is Mozart's catalogue and
        # Scarlatti's (Kirkpatrick). Only a composer's own rows are one work.
        assert not self._found(
            ctx_dry,
            tmp_path,
            "Christian Zacharias",
            "Mozart: Piano Sonata, K. 545 - I. Allegro",
            "Scarlatti: Sonata In G Major, K. 545 - I. Allegro",
        )

    def test_studio_and_live_of_one_movement_are_one_piece(self, ctx_dry, tmp_path):
        # Codex review of #255, round 3: the live-vs-studio length check, made
        # for title-score pairs, dropped this one.
        assert self._found(
            ctx_dry,
            tmp_path,
            "Johann Sebastian Bach",
            "J.S. Bach - Orchestral Suite No. 3 In D Major, BWV 1068 - II. Air On The G String",
            "Orchestral Suite No. 3, BWV 1068 - II. Air (Live)",
            durations=(220.0, 240.0),
        )

    def test_a_title_written_out_twice_names_one_work(self, ctx_dry, tmp_path):
        # Live replay: the library has titles that repeat themselves.
        assert self._found(
            ctx_dry,
            tmp_path,
            "Antonio Vivaldi",
            "The Four Seasons - Autumn, Violin Concerto In F Major, RV 293, Op. 8 No. 3 - Vivaldi"
            " - The Four Seasons - Autumn, Violin Concerto In F Major, RV 293, Op. 8 No. 3 - I. Allegro",
            'The Four Seasons - Violin Concerto No. 3 In F Major, Op. 8, RV 293 "Autumn": I. Allegro',
        )

    def test_one_track_holding_two_works_is_not_one_work(self, ctx_dry, tmp_path):
        # Codex review of #255, round 4: only the first catalogue number was read.
        assert not self._found(
            ctx_dry,
            tmp_path,
            "Johann Sebastian Bach",
            "Orchestral Suite No. 3, BWV 1068 - II. Air",
            "BWV 1068 - II. Air / BWV 1067 - Badinerie",
        )

    def test_two_live_performances_of_one_movement_are_one_piece(self, ctx_dry, tmp_path):
        # Codex review of #255, round 5: the live-vs-live guard ran first.
        assert self._found(
            ctx_dry,
            tmp_path,
            "Johann Sebastian Bach",
            "Orchestral Suite No. 3 In D Major, BWV 1068 - II. Air (Live In Berlin)",
            "J.S. Bach - BWV 1068 - II. Air On The G String (Live At The Musikverein)",
            durations=(300.0, 320.0),
        )

    def test_a_kochel_alias_by_dash_or_slash_is_one_work(self, ctx_dry, tmp_path):
        # Codex review of #255, round 5: "K.280-189e" read as 280 only.
        assert self._found(
            ctx_dry,
            tmp_path,
            "Wolfgang Amadeus Mozart",
            "Piano Sonata No. 2 In F Major, K.280-189e - II. Adagio",
            "Mozart: Sonata In F, K. 280/189e: II. Adagio",
        )

    def test_two_arias_of_one_act_are_two_pieces(self, ctx_dry, tmp_path):
        # Codex review of #255, round 6: an act holds many pieces.
        assert not self._found(
            ctx_dry,
            tmp_path,
            "George Frideric Handel",
            "Serse, HWV 40 - Act 1: Ombra mai fu",
            "Handel: Serse, HWV 40, Act 1: Crude furie degl'orridi abissi",
        )

    def test_roman_and_arabic_movement_numbers_are_one_movement(self, ctx_dry, tmp_path):
        # Codex review of #255, round 6.
        assert self._found(
            ctx_dry,
            tmp_path,
            "Johann Sebastian Bach",
            "J.S. Bach - Orchestral Suite No. 3 In D Major, BWV 1068 - II. Air On The G String",
            "Suite For Orchestra, BWV 1068 - Movement 2: Air",
        )

    def test_mass_sections_restarting_at_one_are_two_pieces(self, ctx_dry, tmp_path):
        # Codex review of #255, round 7: each section restarts at "I.".
        assert not self._found(
            ctx_dry,
            tmp_path,
            "Johann Sebastian Bach",
            "Mass In B Minor, BWV 232 - Kyrie: I. Kyrie eleison",
            "Bach: Mass In B Minor, BWV 232, Gloria: I. Gloria in excelsis Deo",
        )

    def test_numbered_items_of_one_work_are_two_pieces(self, ctx_dry, tmp_path):
        # Codex review of #255, round 7: matching catalogue numbers had stopped
        # the "No." comparison; the title score is 97.6.
        assert not self._found(
            ctx_dry,
            tmp_path,
            "Johann Sebastian Bach",
            "Mass In B Minor, BWV 232 - No. 1 Kyrie eleison",
            "Mass In B Minor, BWV 232 - No. 3 Kyrie eleison",
        )

    @pytest.mark.parametrize(
        "label",
        [
            "(Arr. For Piano)",
            "(Piano Arrangement)",
            "(Adaptation For Solo Piano)",
            "(Trans. For Solo Piano)",
            "(Bearbeitung Für Klavier)",
            "(Piano Reduction)",
        ],
    )
    def test_an_arrangement_is_the_same_piece(self, ctx_dry, tmp_path, label):
        # Grey, 2026-10-26: "does an arrangement count as the same piece? = yes".
        assert self._found(
            ctx_dry,
            tmp_path,
            "Johann Sebastian Bach",
            "J.S. Bach - Orchestral Suite No. 3 In D Major, BWV 1068 - II. Air On The G String",
            f"Orchestral Suite No. 3, BWV 1068 - II. Air {label}",
        )

    @pytest.mark.parametrize("label", ["(Bearbeitung Für Klavier)", "(Arr. Myra Hess For Piano)"])
    def test_a_shared_arrangement_label_names_no_movement(self, ctx_dry, tmp_path, label):
        # Codex review of #255, round 15: the label's words were the only shared name.
        assert not self._found(
            ctx_dry,
            tmp_path,
            "Johann Sebastian Bach",
            f"BWV 1068 - II. Air {label}",
            f"BWV 1068 - Movement 2: Gavotte {label}",
        )

    @pytest.mark.parametrize(
        ("title_a", "title_b"),
        [
            ("BWV 846 - I. Prelude (Live In Berlin)", "BWV 846 - I. Fugue (Live In Berlin)"),
            (
                "BWV 846 - I. Prelude In C Major (Recorded At Carnegie Hall)",
                "BWV 846 - I. Fugue (Recorded At Carnegie Hall)",
            ),
            ("BWV 846 - I. (Prelude) (Live In Berlin)", "BWV 846 - I. (Fugue) (Live In Berlin)"),
            # Round 17: the concert bracket came first, then stood in as the name
            (
                "BWV 846 - I. (Live In Berlin) (Prelude)",
                "BWV 846 - Movement 1 (Live In Berlin) (Fugue)",
            ),
            ("BWV 1068 - II. (Live In Berlin)", "BWV 1068 - Movement 2 (Live In Berlin) (Air)"),
            # Round 19: the same note without brackets
            (
                "BWV 846 - I. Prelude In C Major - Live In Berlin",
                "BWV 846 - I. Fugue - Live In Berlin",
            ),
            (
                "BWV 846 - I. Prelude: Recorded At Carnegie Hall",
                "BWV 846 - Movement 1: Fugue: Recorded At Carnegie Hall",
            ),
        ],
    )
    def test_a_shared_concert_label_names_no_movement(self, ctx_dry, tmp_path, title_a, title_b):
        # Codex review of #255, round 16: "berlin" was the only shared name.
        assert not self._found(ctx_dry, tmp_path, "Johann Sebastian Bach", title_a, title_b)

    @pytest.mark.parametrize(
        ("title_a", "title_b"),
        [
            ("BWV 846 - I. Prelude In C Major", "BWV 846 - I. Fugue In C Major"),
            ("BWV 847 - I. Prelude In C Sharp Minor", "BWV 847 - I. Fugue In C Sharp Minor"),
            ("BWV 846 - I. Präludium C-Dur", "BWV 846 - I. Fuge C-Dur"),
            ("BWV 846 - I. Prélude En Ut Majeur", "BWV 846 - I. Fugue En Ut Majeur"),
        ],
    )
    def test_a_shared_key_names_no_movement(self, ctx_dry, tmp_path, title_a, title_b):
        # CodeRabbit review of #255: "major" was the only shared name.
        assert not self._found(ctx_dry, tmp_path, "Johann Sebastian Bach", title_a, title_b)

    @pytest.mark.parametrize("label", ["(Radio Edit)", "(Edit)", "(Abridged)", "(Short Version)"])
    def test_a_labelled_cut_takes_no_shortcut_at_any_length(self, ctx_dry, tmp_path, label):
        # Codex review of #255, round 24: a 220 s "Radio Edit" is over half the Air.
        assert not self._found(
            ctx_dry,
            tmp_path,
            "Johann Sebastian Bach",
            f"Orchestral Suite No. 3, BWV 1068 - II. Air {label}",
            "J.S. Bach - Orchestral Suite No. 3 In D Major, BWV 1068 - II. Air On The G String",
            durations=(220.0, 320.0),
        )

    @pytest.mark.parametrize("label", ["(Excerpt)", "(Fragment)", "(Radio Edit)", ""])
    def test_a_part_of_a_movement_is_not_the_movement(self, ctx_dry, tmp_path, label):
        # Codex review of #255, round 18: a 75 s excerpt could replace the full Air;
        # round 23: so could a 75 s "Radio Edit", or a cut with no label at all.
        assert not self._found(
            ctx_dry,
            tmp_path,
            "Johann Sebastian Bach",
            f"Orchestral Suite No. 3, BWV 1068 - II. Air {label}",
            "J.S. Bach - Orchestral Suite No. 3 In D Major, BWV 1068 - II. Air On The G String",
            durations=(75.0, 320.0),
        )

    def test_a_dotted_catalogue_number_is_read(self, ctx_dry, tmp_path):
        # Codex review of #255, round 25: "BWV. 1068" was no catalogue number.
        assert not self._found(
            ctx_dry, tmp_path, "Johann Sebastian Bach", "BWV. 1068 - II. Air", "BWV 1067 - II. Air"
        )

    def test_a_catalogue_number_is_no_name(self, ctx_dry, tmp_path):
        # Codex review of #255, round 22: "BWV" was the only shared name word.
        assert not self._found(
            ctx_dry,
            tmp_path,
            "Johann Sebastian Bach",
            "II. Air on the G String, BWV 1068",
            "II. Gavotte for orchestra, BWV 1068",
        )

    def test_a_second_name_without_a_marker_is_a_second_movement(self, ctx_dry, tmp_path):
        # Codex review of #255, round 22: one marker, two movements named.
        assert not self._found(
            ctx_dry,
            tmp_path,
            "Johann Sebastian Bach",
            "BWV 1068 - II. Air on the G String / Gavotte for orchestra",
            "BWV 1068 - II. Air on the G String",
        )

    def test_a_tempo_change_is_one_movement(self, ctx_dry, tmp_path):
        # The live Vivaldi "Summer": " - " joins one movement's tempos.
        assert self._found(
            ctx_dry,
            tmp_path,
            "Antonio Vivaldi",
            "Vivaldi: The Four Seasons: Summer, Violin Concerto In G Minor, RV 315, Op. 8 No. 2: "
            "I. Allegro Non Molto - Allegro",
            "Concerto In Sol Minore Op.8 No.2 Rv315, F.I/23, L'Estate - Summer: I. Allegro Non Molto",
        )

    def test_a_track_of_two_movements_takes_no_shortcut(self, ctx_dry, tmp_path):
        # Codex review of #255, round 20: the name was read after the last marker
        # only, so two tracks ending in "II. Fugue" paired.
        assert not self._found(
            ctx_dry,
            tmp_path,
            "Johann Sebastian Bach",
            "BWV 846 - I. Prelude In C Major / II. Fugue",
            "BWV 846 - I. Fantasia For Organ / II. Fugue",
        )
        # Round 21: the same marker twice is still two movements
        assert not self._found(
            ctx_dry,
            tmp_path,
            "Johann Sebastian Bach",
            "BWV 846 - I. Prelude In C Major / I. Fugue",
            "BWV 846 - I. Fantasia For Organ / I. Fugue",
        )

    def test_a_medley_is_not_the_piece(self, ctx_dry, tmp_path):
        assert not self._found(
            ctx_dry,
            tmp_path,
            "Johann Sebastian Bach",
            "J.S. Bach - Orchestral Suite No. 3 In D Major, BWV 1068 - II. Air On The G String",
            "Orchestral Suite No. 3, BWV 1068 - II. Air (Medley)",
        )

    def test_a_medley_never_pairs_with_one_of_its_songs(self, ctx_dry, tmp_path):
        # Codex review of #255, round 8: the title score paired these (on main too).
        assert not self._found(
            ctx_dry,
            tmp_path,
            "Johann Sebastian Bach",
            "J.S. Bach - Orchestral Suite No. 3 In D Major, BWV 1068 - II. Air On The G String",
            "J.S. Bach - Orchestral Suite No. 3 In D Major, BWV 1068 - II. Air On The G String (Medley)",
        )

    @pytest.mark.parametrize(
        ("title_a", "title_b"),
        [
            (
                "Mass In B Minor, BWV 232 - Kyrie: I. Chorus",
                "Mass In B Minor, BWV 232 - Gloria: I. Chorus",
            ),
            (
                "Mass In B Minor, BWV 232 - I. (Kyrie Eleison)",
                "Mass In B Minor, BWV 232 - I. (Gloria In Excelsis)",
            ),
        ],
    )
    def test_sections_of_a_mass_are_never_one_movement(self, ctx_dry, tmp_path, title_a, title_b):
        # Codex review of #255, round 8: a Mass restarts its numbering in every
        # section, and the title cannot always say which section it is.
        assert not self._found(ctx_dry, tmp_path, "Johann Sebastian Bach", title_a, title_b)

    def test_an_improvisation_with_a_close_title_is_not_the_piece(self, ctx_dry, tmp_path):
        # Codex review of #255, round 9: the title score paired these (on main too).
        assert not self._found(
            ctx_dry,
            tmp_path,
            "Johann Sebastian Bach",
            "J.S. Bach - Orchestral Suite No. 3 In D Major, BWV 1068 - II. Air On The G String",
            "J.S. Bach - Orchestral Suite No. 3 In D Major, BWV 1068 - II. Air On The G String "
            "(Improvisation)",
        )

    def test_a_shared_live_suffix_is_no_shared_movement_name(self, ctx_dry, tmp_path):
        # Codex review of #255, round 10: "Live" was the only word in common.
        assert not self._found(
            ctx_dry,
            tmp_path,
            "Johann Sebastian Bach",
            "Mass In B Minor, BWV 232: I. Lord Have Mercy Upon Us - Live",
            "Bach: Mass In B Minor, BWV 232: I. Glory Be To God On High - Live",
        )

    def test_a_recording_kept_by_a_person_never_takes_the_shortcut(self, ctx_dry, tmp_path):
        # Codex review of #255, round 11: a third copy paired with each of two
        # recordings a person kept both of, and the resolver moved one of them.
        canon = "".join(f"{c.lower()}\t{c}\n" for c in self.COMPOSERS)
        (ctx_dry.config.meta_dir / "Composer_Canon.tsv").write_text(canon, encoding="utf-8")
        titles = {
            "a": "J.S. Bach - Orchestral Suite No. 3 In D Major, BWV 1068 - II. Air On The G String",
            "b": "Suite For Orchestra, BWV 1068 - Movement 2: Air",
            "c": "Bach: Ouvertüre Nr. 3, BWV 1068: II. Air (Arr. For Piano)",
        }
        for name, title in titles.items():
            path = str(tmp_path / f"{name}.flac")
            _insert_catalogued(ctx_dry, path, "Johann Sebastian Bach", title)
            ctx_dry.conn.execute(
                "UPDATE archive SET audio_hash = ? WHERE file_path = ?", (f"hash-{name}", path)
            )
        for name in ("a", "b"):
            ctx_dry.conn.execute(
                "INSERT INTO duplicates (group_id, file_path, duplicate_type, status, audio_hash) "
                "VALUES ('near_kept', ?, 'NEAR', 'keep', ?)",
                (str(tmp_path / f"{name}.flac"), f"hash-{name}"),
            )
        ctx_dry.conn.commit()
        assert NearDupeStage().execute(ctx_dry).files_changed == 0

    @pytest.mark.parametrize(
        ("title_a", "title_b"),
        [
            (  # Codex review of #255, round 12: sections restart at "I.".
                "Requiem In D Minor, K. 626 - Offertorium: I. (Domine Jesu Christe)",
                "Requiem In D Minor, K. 626 - Sequentia: I. (Dies Irae)",
            ),
            (
                "Cantata BWV 147 - I. Herz und Mund und Tat und Leben",
                "Herz Und Mund, BWV 147: I. Chorus",
            ),
        ],
    )
    def test_a_vocal_work_never_takes_the_shortcut(self, ctx_dry, tmp_path, title_a, title_b):
        # Masses, Requiems, cantatas and the like restart their numbering in
        # each section; their titles cannot always say which section it is.
        artist = "Wolfgang Amadeus Mozart" if "K. 626" in title_a else "Johann Sebastian Bach"
        assert not self._found(ctx_dry, tmp_path, artist, title_a, title_b)

    def test_a_bracketed_movement_name_still_names_it(self, ctx_dry, tmp_path):
        # Codex review of #255, round 13: bracketed names were dropped, so two
        # differently named movements had no name to compare.
        assert not self._found(
            ctx_dry,
            tmp_path,
            "Johann Sebastian Bach",
            "Orchestral Suite No. 3, BWV 1068 - II. (Air)",
            "J.S. Bach: Suite In D, BWV 1068 - II. (Bourrée)",
        )

    def test_an_unnamed_movement_never_takes_the_shortcut(self, ctx_dry, tmp_path):
        # Codex review of #255, round 14: "II. [Without Indication]" paired with
        # both "II. Air" and "II. Bourrée" and joined them in one group.
        assert not self._found(
            ctx_dry,
            tmp_path,
            "Johann Sebastian Bach",
            "Violin Concerto No. 1 In A Minor, BWV 1041: I. [Without Indication]",
            "J.S. Bach: Violin Concerto No. 1 In A Minor, BWV 1041: I. (Allegro Moderato)",
        )

    def test_an_improvisation_on_a_piece_is_not_the_piece(self, ctx_dry, tmp_path):
        assert not self._found(
            ctx_dry,
            tmp_path,
            "Bobby McFerrin",
            "Song For Amadeus (Improvisation Of Sonata No. 2 In F Major, K.280-189e - II. Adagio)",
            "Song For Amadeus (Improvisation On Mozart's Piano Sonata No. 2 In F Major, "
            "K. 280/189e: II. Adagio) (Vocal)",
        )
