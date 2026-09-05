"""
Tests for ArtistConsolidateStage / _preferred_name()'s "the"-article format.

Regression context (do not remove without understanding why this file
exists): the canonical "the"-article output format was fixed at least
twice and kept coming back wrong.

  - 2026-08-08 (47c3a36): normalize.py's _move_article_to_suffix() fixed
    to emit "Beatles, The" (suffix form), for correct alphabetical
    sorting. Tested with 7 cases, committed.
  - ~2026-08-12: a wrong "(The)" value was seen live, manually reversed.
    Never root-caused at the time.
  - 2026-08-14 (c559fa6): artist_consolidate.py's _preferred_name()
    touched for an unrelated comma-tail bug, but its own "the"-article
    branch still emitted the OLD "Name (The)" parenthetical format.
  - 2026-08-15: root cause found. ACT1_INTAKE_CORRECTION
    (musaeus/stages/__init__.py) runs NormalizeStage BEFORE
    ArtistConsolidateStage. Normalize correctly writes "Beatles, The" --
    then ArtistConsolidateStage runs immediately after, sees that value,
    and (via _preferred_name()'s with_the/without_the branch)
    regenerates the canonical form as "Beatles (The)", silently
    reintroducing the exact bug Normalize had just fixed. Not a second
    hidden code path or a stale cache -- two stages, same pipeline run,
    disagreeing conventions, second one wins.

A pure unit test of _preferred_name() alone is not sufficient to catch
this class of regression -- it would keep passing even if some other
function elsewhere in the same pipeline re-emitted the wrong format
downstream. TestArtistConsolidateStageLive below exercises the actual
live run() path (real DB, real INSERT, real UPDATE) specifically so a
future stage-ordering or formatting regression here fails a test instead
of being rediscovered live for a third time.
"""

from pathlib import Path

import pytest

from musaeus.config import MusicConfig
from musaeus.context import RunContext
from musaeus.db import open_db, upsert_archive
from musaeus.stages.artist_consolidate import ArtistConsolidateStage, _preferred_name


class TestPreferredNameArticleFormat:
    """Unit-level: _preferred_name() must emit ', The' (suffix), never
    '(The)' (parenthetical), when with/without-article variants exist."""

    def test_generic_with_and_without_the(self):
        assert _preferred_name([("Beatles", 500), ("Beatles, The", 10)]) == "Beatles, The"

    def test_leading_the_variant(self):
        assert _preferred_name([("Kinks", 50), ("The Kinks", 5)]) == "Kinks, The"

    def test_never_emits_parenthetical_form(self):
        result = _preferred_name([("Band", 10), ("Band, The", 90)])
        assert "(The)" not in result
        assert "(the)" not in result.lower()
        assert result == "Band, The"

    def test_chieftains_real_data_split(self):
        """Real counts from the c559fa6 commit: 'Chieftains' (76 tracks)
        and 'the Chieftains' (78 tracks) previously produced different
        normalize keys and were never even recognized as the same
        artist. Confirms both the grouping fix and the format fix."""
        assert _preferred_name([("Chieftains", 76), ("the Chieftains", 78)]) == "Chieftains, The"

    def test_hardcoded_canon_display_uses_suffix_form(self):
        assert (
            _preferred_name([("Andrews Sisters", 1), ("Andrews Sisters (The)", 1)])
            == "Andrews Sisters, The"
        )

    def test_single_variant_suffix_the_stays_capitalized(self):
        """2026-08-15 real-data find: a THIRD independent spot touching
        this convention. Single-variant path (no with/without-the pair
        to group) runs the name through _smart_title(), whose
        and/of/the connector-word lowercasing rule (for cases like
        "Lord of the Rings") was also lowercasing a genuine trailing
        ", The" article suffix. Found via real INBOX data: "Chieftains
        and Belfast Harp Orchestra, The" and "Bob Seger System, The"
        both came out with a lowercase trailing "the" before this was
        fixed in _smart_title()."""
        assert (
            _preferred_name([("Chieftains and Belfast Harp Orchestra, The", 1)])
            == "Chieftains & Belfast Harp Orchestra, The"
        )
        assert _preferred_name([("Bob Seger System, The", 1)]) == "Bob Seger System, The"


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


def _insert_catalogued(conn, path: str, artist: str) -> None:
    upsert_archive(
        conn,
        {
            "file_path": path,
            "filename": Path(path).name,
            "ext": Path(path).suffix,
            "status": "CATALOGUED",
            "artist": artist,
        },
    )


class TestArtistConsolidateStageLive:
    """Stage-level: run ArtistConsolidateStage for real against a real DB
    and assert the value actually written back to archive.artist -- not
    just what the helper function returns in isolation. This is the test
    that would have caught the NormalizeStage-then-ArtistConsolidateStage
    ordering regression, since it exercises the same live UPDATE path the
    real pipeline uses."""

    def test_run_writes_suffix_form_not_parenthetical(self, ctx: RunContext):
        conn = ctx.conn
        # Simulates the real-world sequence: NormalizeStage already moved
        # one variant to suffix form; a second, uncorrected variant
        # still has the bare (no-article) form.
        _insert_catalogued(conn, "/inbox/a.flac", "Beatles, The")
        _insert_catalogued(conn, "/inbox/b.flac", "Beatles")
        conn.commit()

        stage = ArtistConsolidateStage()
        result = stage.run(ctx)

        assert result.success
        rows = conn.execute(
            "SELECT DISTINCT artist FROM archive WHERE status = 'CATALOGUED'"
        ).fetchall()
        artists = {row["artist"] for row in rows}

        assert artists == {"Beatles, The"}, (
            f"expected consolidation to suffix form only, got {artists!r} -- "
            "if this is failing with '(The)' anywhere in it, the "
            "NormalizeStage/ArtistConsolidateStage format regression is back"
        )

    def test_run_chieftains_real_world_shape(self, ctx: RunContext):
        conn = ctx.conn
        for i in range(76):
            _insert_catalogued(conn, f"/inbox/chieftains_{i}.flac", "Chieftains")
        for i in range(78):
            _insert_catalogued(conn, f"/inbox/the_chieftains_{i}.flac", "the Chieftains")
        conn.commit()

        stage = ArtistConsolidateStage()
        stage.run(ctx)

        rows = conn.execute(
            "SELECT DISTINCT artist FROM archive WHERE status = 'CATALOGUED'"
        ).fetchall()
        artists = {row["artist"] for row in rows}
        assert artists == {"Chieftains, The"}


# ── ArtistCanon is actually applied now ──────────────────────────────────────
#
# Until 2026-08-21 the canon was dead data for correction purposes:
# organize.py's docstring claimed it used ArtistCanon but never imported it;
# normalize.py said canon lookup was "Scholar/Enrich"'s job and neither
# imported it either. Only neardupe.py touched it, and only to group
# candidates -- nothing ever wrote a canonical name back to archive.artist.
# That is why hand-curated mappings (including truncation repairs) never took
# effect, and why a lone wrong name survived every run: the grouping pass
# skips any key with a single variant.


class TestArtistCanonApplied:
    def _canon(self, tmp_path, rows):
        p = tmp_path / "artist_canon.tsv"
        p.write_text("# test canon\n" + "".join(f"{r}\t{c}\n" for r, c in rows), encoding="utf-8")
        from musaeus.canon import ArtistCanon

        return ArtistCanon(p)

    def test_exact_mapping_resolves(self, tmp_path):
        c = self._canon(tmp_path, [("nce boylan", "Terence Boylan")])
        assert c.resolve_exact("nce Boylan") == "Terence Boylan"

    def test_lookup_is_case_and_space_insensitive(self, tmp_path):
        c = self._canon(tmp_path, [("nce boylan", "Terence Boylan")])
        assert c.resolve_exact("  NCE   Boylan ") == "Terence Boylan"

    def test_unmapped_name_returns_none_not_a_guess(self, tmp_path):
        c = self._canon(tmp_path, [("nce boylan", "Terence Boylan")])
        assert c.resolve_exact("Someone Else Entirely") is None

    def test_similar_but_unmapped_names_are_left_alone(self, tmp_path):
        """The protected-pair guarantee.

        "Paul Young" (UK) and "John Paul Young" (Australian) are different
        people. resolve()'s fuzzy fallback scores them 80 -- under the 88
        threshold today, but only just. resolve_exact() removes the question
        entirely: no canon entry, no rename, no matter the score.
        """
        c = self._canon(tmp_path, [("nce boylan", "Terence Boylan")])
        assert c.resolve_exact("Paul Young") is None
        assert c.resolve_exact("John Paul Young") is None
        assert c.resolve_exact("Bon Jovi") is None

    def test_resolve_exact_never_falls_back_to_fuzzy(self, tmp_path):
        # resolve() may guess; resolve_exact() must not.
        c = self._canon(tmp_path, [("terence boylan", "Terence Boylan")])
        assert c.resolve_exact("Terrence Boylan") is None

    def test_empty_input_is_safe(self, tmp_path):
        c = self._canon(tmp_path, [("a", "B")])
        assert c.resolve_exact("") is None


class TestVerifyEffectChecksTheCanonWasApplied:
    """The canon is the deliberate half of this stage: every entry is a
    mapping somebody wrote down. A row still carrying a mapped raw name
    means the promise silently did not apply -- what a normalisation-key
    change, a status filter or a missing commit all look like from outside.

    2026-09-05: this stage rewrote SwitchOTR to Switchotr and would have
    flattened 202 artists across 477 tracks, and nothing checked its work.
    """

    def _canon(self, ctx, pairs):
        f = ctx.config.meta_dir / "artist_canon.tsv"
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text("".join(f"{a}\t{b}\n" for a, b in pairs), encoding="utf-8")

    def _row(self, ctx, artist):
        from musaeus.db import upsert_archive
        p = ctx.config.alac_library / f"{artist}.m4a"
        upsert_archive(ctx.conn, {"file_path": str(p), "status": "CATALOGUED",
                                  "artist": artist, "title": "t"})
        ctx.conn.commit()

    def test_an_applied_mapping_passes(self, ctx):
        self._canon(ctx, [("Dire Strats", "Dire Straits")])
        self._row(ctx, "Dire Straits")
        from unittest.mock import MagicMock
        assert ArtistConsolidateStage().verify_effect(ctx, MagicMock(files_changed=1)) == []

    def test_a_mapping_that_never_applied_is_caught(self, ctx):
        self._canon(ctx, [("Dire Strats", "Dire Straits")])
        self._row(ctx, "Dire Strats")          # still the raw name
        from unittest.mock import MagicMock
        problems = ArtistConsolidateStage().verify_effect(ctx, MagicMock(files_changed=1))
        assert problems, "a row still carrying a mapped name must not pass"
        assert "Dire Straits" in problems[0]

    def test_an_identity_mapping_is_not_a_complaint(self, ctx):
        """Registering a name as-is ('accept this spelling') maps it to
        itself and must never be reported as unapplied."""
        self._canon(ctx, [("Tone-Loc", "Tone-Loc")])
        self._row(ctx, "Tone-Loc")
        from unittest.mock import MagicMock
        assert ArtistConsolidateStage().verify_effect(ctx, MagicMock(files_changed=1)) == []
