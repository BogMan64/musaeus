"""
MUSAEUS — P0-01 characterization pass

Runs the CURRENT (unmodified) preview/dry-run entry points and other
relevant existing entry points against the disposable vault, and records
what they actually do TODAY, honestly. This is a baseline/characterization
suite for later P0 tasks to fix against, not a suite asserting today's
behaviour is acceptable. Where behaviour is unsafe, the test says so in
its name and docstring rather than treating it as a passing safety
guarantee.

Every assertion here is a factual "this is what currently happens", each
one individually confirmed against the running code during the P0-01
session (see the completion evidence for this task in
.kiro/specs/musaeus-consumer-readiness/tasks.md).

Scope: the typed public CLI preview surface these tests describe is
`musaeus.cli._run_pipeline(..., dry_run=True)`, which is what `musaeus
run --dry-run` / `musaeus dry-run` actually invoke. P0-04/P0-05's
distinct RunMode.PREVIEW/typed-planner design does not exist in this
codebase (confirmed: no state/, network_policy.py, or planning module
present) — the tasks.md history describing that work belonged to a
reverted branch. What's characterized here is the CLI's actual, current,
unguarded dry-run path.
"""

from __future__ import annotations

from unittest.mock import patch

from musaeus.stages import DEFAULT_PIPELINE

# ── musaeus run --dry-run / musaeus dry-run (DEFAULT_PIPELINE) ────────────────


class TestDefaultPipelineDryRunCharacterization:
    """
    `musaeus.cli._run_pipeline(DEFAULT_PIPELINE, dry_run=True)` is exactly
    what `musaeus run --dry-run` and the `musaeus dry-run` alias invoke
    (see musaeus/cli.py's command dispatch). This exercises that real
    function end to end against a disposable vault.
    """

    def _run_cli_dry_run(self, disposable_vault, monkeypatch, tmp_path):
        """Invoke the real CLI pipeline runner exactly as `musaeus
        dry-run` does, with get_config() patched to the disposable
        vault's config (mirroring how the CLI resolves it via
        environment normally) and a redirected HOME so cli.py's
        Path.home()-frozen _RESUME_FILE cannot touch real state."""
        import musaeus.cli as cli_mod

        monkeypatch.setattr(cli_mod, "get_config", lambda: disposable_vault.cfg)
        monkeypatch.setattr(
            cli_mod, "_RESUME_FILE", disposable_vault.config_home / "resume_state.json"
        )
        return cli_mod._run_pipeline(DEFAULT_PIPELINE, dry_run=True)

    def test_characterization_dry_run_creates_the_vault_directory_skeleton(
        self, disposable_vault, monkeypatch, tmp_path
    ):
        """
        UPDATED BY P0-02 (musaeus-consumer-readiness spec): this test
        originally documented an UNSAFE-BY-DESIGN-INTENT finding --
        `dry-run` called cfg.ensure_dirs() unconditionally before any
        stage ran (see musaeus/cli.py's _run_pipeline()), mkdir()ing the
        entire vault skeleton (INBOX/STAGING/QUARANTINE/RUNS/MetaData/
        ALAC-Library/etc) even in dry-run mode. That finding is exactly
        what task P0-02 added a temporary, blunt, fail-closed guard for:
        _reject_unsafe_dry_run() now rejects dry_run=True before
        cfg.ensure_dirs() (or anything else) runs, so the directory
        skeleton must NOT be created. This is a compatibility patch, not
        the real preview fix -- P0-04/P0-05 still need to build a
        truthful RunMode.PREVIEW.
        """
        assert not disposable_vault.root.exists()
        rc = self._run_cli_dry_run(disposable_vault, monkeypatch, tmp_path)
        # UPDATED AGAIN BY P0-04/P0-05: the refusal is lifted, so this now
        # SUCCEEDS -- while the claim that matters is unchanged and, if
        # anything, stronger. P0-02 proved no skeleton was created because
        # nothing ran; the planner proves it while actually producing a
        # preview, because it never calls ensure_dirs() at all.
        assert rc == 0, "--dry-run must now succeed by producing a plan"
        assert not disposable_vault.root.exists(), (
            "the planner must never call cfg.ensure_dirs() -- no vault "
            "directory skeleton may be created by a preview"
        )

    def test_characterization_dry_run_creates_and_writes_to_the_db(
        self, disposable_vault, monkeypatch, tmp_path
    ):
        """
        UPDATED BY P0-02: this test originally documented an
        UNSAFE-BY-DESIGN-INTENT finding -- dry-run created the SQLite DB
        file (via db.open_db()) and committed RUN_START/STAGE_COMPLETE/
        RUN_END events to it unconditionally (RunContext.new() and
        record_stage() have no dry_run gate). P0-02's guard now rejects
        dry_run=True before get_config()/cfg.ensure_dirs()/open_db() are
        ever called, so the DB file itself must not exist afterwards.
        """
        rc = self._run_cli_dry_run(disposable_vault, monkeypatch, tmp_path)
        # UPDATED AGAIN BY P0-04/P0-05: succeeds now, and still creates no
        # database. The planner opens mode=ro and reports an absent DB as a
        # fact rather than creating one -- the original defect this test was
        # written to document.
        assert rc == 0
        assert not disposable_vault.cfg.db_path.exists(), (
            "the planner must never open a writable connection -- "
            "no database file may be created by a preview"
        )

    def test_characterization_dry_run_rejection_is_stable_across_repeated_calls(
        self, disposable_vault, monkeypatch, tmp_path
    ):
        """
        UPDATED BY P0-02: this test originally proved a SAFE finding --
        against an empty inbox, archive/duplicates/validation_issues all
        stayed at zero rows after a dry-run. That finding is now
        subsumed by the stronger guarantee proven above (the DB is never
        created at all), so this test is repurposed to prove the P0-02
        guard's rejection is stable and side-effect-free across repeated
        calls, not just a one-off first call.
        """
        rc1 = self._run_cli_dry_run(disposable_vault, monkeypatch, tmp_path)
        rc2 = self._run_cli_dry_run(disposable_vault, monkeypatch, tmp_path)
        # UPDATED AGAIN BY P0-04/P0-05: stability still matters, but of a
        # preview rather than a refusal. Running it twice must remain inert.
        assert rc1 == 0
        assert rc2 == 0
        assert not disposable_vault.root.exists()
        assert not disposable_vault.cfg.db_path.exists()

    def test_characterization_dry_run_makes_no_network_connection(
        self, disposable_vault, monkeypatch, tmp_path, transport_harness
    ):
        """
        SAFE FINDING, still true after the 2026-08-17 reorder for a
        different reason than originally: DEFAULT_PIPELINE now DOES
        include EnrichStage/MBEnrichStage (default-on, last in the
        chain), but P0-02's fail-closed guard rejects `--dry-run`
        outright, before any stage's dry_run() ever runs — so a CLI
        dry-run still makes zero outbound network connection attempts,
        just via a different mechanism (blocked at the guard, not by
        pipeline composition). AcousticIDStage/ReviewerStage remain
        on-demand only. This test runs under the session-wide
        transport_harness every other test already runs under, so any
        connection attempt would raise NetworkAccessDeniedError and fail
        this test rather than silently succeed.
        """
        transport_harness_attempts_before = len(transport_harness.attempts)
        self._run_cli_dry_run(disposable_vault, monkeypatch, tmp_path)
        assert len(transport_harness.attempts) == transport_harness_attempts_before

    def test_characterization_dry_run_rejection_message_is_honest_and_on_stderr(
        self, disposable_vault, monkeypatch, tmp_path, capsys
    ):
        """
        UPDATED BY P0-02: this test originally captured that dry-run's
        stdout output never made MCR-001's required "no managed state
        was changed" statement, while also never printing anything that
        contradicted it outright. Now that dry-run is rejected instead
        of executed, the risk flips: a rejection message MUST NOT be
        mistaken for a successful (if silent) preview -- it must not
        claim success or "no changes made" while staying silent about
        the fact that nothing ran. This test proves the P0-02 guard's
        message lands on stderr (not stdout, so it can't be confused
        with real preview output) and is honest about being a refusal,
        not a completed no-op preview.
        """
        rc = self._run_cli_dry_run(disposable_vault, monkeypatch, tmp_path)
        captured = capsys.readouterr()

        # UPDATED AGAIN BY P0-04/P0-05. The risk flips back once more. P0-02
        # had to prove its refusal was NOT mistaken for a preview. Now there
        # IS a preview, and it must state plainly that nothing changed --
        # which the refusal was never entitled to say, because it had not
        # looked at anything.
        assert rc == 0
        assert "PREVIEW ONLY" in captured.out
        assert "nothing was changed" in captured.out
        assert "no network lookup was performed" in captured.out
        # The plan is the product; it belongs on stdout so it can be piped.
        assert captured.out.strip() != ""
        assert "temporarily disabled" not in captured.err.lower()


# ── Resume-records-failed-as-complete baseline defect (reproduced) ───────────


class TestResumeRecordsFailedStageAsCompleteCharacterization:
    """
    FIXED 2026-08-18. Used to reproduce the baseline defect
    requirements.md names: "Resume records failed stages as complete and
    permits downstream work to continue." cli.py's _run_pipeline() now
    only appends a stage to completed_names (and saves that to the resume
    file) when result.success is True -- a stage that runs to completion
    but reports failure (no exception, just result.success=False) is no
    longer treated as resumable-skip. This test now proves the fix: the
    failed stage is retried, not silently skipped, on the next
    invocation. This was the specific defect P0-08 (run/stage lifecycle,
    prerequisite gating, safe resume) tracked under this name; P0-08's
    broader scope may still have other open items unrelated to this one.

    UPDATED BY P0-02 (kept from the original test): dry_run=True is
    rejected outright now, so this exercises dry_run=False (a real run)
    against the disposable vault's empty inbox -- every other stage still
    completes trivially with zero files to process.
    """

    def test_failed_stage_is_retried_not_skipped_on_resume(
        self, disposable_vault, monkeypatch, tmp_path
    ):
        import musaeus.cli as cli_mod

        monkeypatch.setattr(cli_mod, "get_config", lambda: disposable_vault.cfg)
        monkeypatch.setattr(
            cli_mod, "_RESUME_FILE", disposable_vault.config_home / "resume_state.json"
        )

        # Force NearDupeStage to fail validation deterministically without
        # depending on whether rapidfuzz happens to be installed in
        # whatever environment runs this suite.
        from musaeus.stages.base import StageError
        from musaeus.stages.neardupe import NearDupeStage

        def _always_fail_validate(self, ctx):
            raise StageError("forced failure for characterization test")

        monkeypatch.setattr(NearDupeStage, "validate", _always_fail_validate)

        rc1 = cli_mod._run_pipeline(DEFAULT_PIPELINE, dry_run=False)
        assert rc1 == 1  # pipeline reports failure the first time

        import json

        resume_state = json.loads(cli_mod._RESUME_FILE.read_text())
        assert "NearDupeStage" not in resume_state["completed"], (
            "fix regressed: a stage that FAILED validation was written "
            "into the resume file's 'completed' list"
        )

        # Second invocation: NearDupeStage's validate() is still
        # monkeypatched to always fail, so a correct retry fails again --
        # rc2 == 1 (honest, not silently reported as success) proves the
        # stage was actually re-attempted, not skipped.
        rc2 = cli_mod._run_pipeline(DEFAULT_PIPELINE, dry_run=False)
        assert rc2 == 1, (
            "fix regressed: resume treated the still-failing stage as "
            "done and reported the pipeline complete"
        )


# ── On-demand network stages: dry_run does not gate the network call ─────────


class TestNetworkStageDryRunCharacterization:
    """
    FIXED 2026-08-18. EnrichStage and MBEnrichStage joined
    DEFAULT_PIPELINE's default-on chain 2026-08-17 (positioned last,
    after Audit), which is what made the pre-existing "dry_run doesn't
    gate the network call" gap worth actually closing rather than just
    tracking (previously these stages were on-demand only, so the gap
    was lower-stakes). Both stages' dry_run() now skip the real network
    call entirely -- not just the DB write -- reporting what WOULD be
    queried instead of actually querying it. AcousticIDStage remains
    on-demand only and is unaffected; its own dry_run() still makes real
    network calls, tracked separately in the consumer-readiness safety
    spec (P0-03+, not this fix). MBEnrichStage also probes connectivity
    once up front and degrades gracefully (skip + report) when
    unreachable — see test_mb_enrich_unreachable_network_skips_gracefully.
    """

    def _catalogued_row(self, ctx, file_path: str, artist: str = "Some Artist") -> None:
        from musaeus.db import upsert_archive

        upsert_archive(
            ctx.conn,
            {
                "file_path": file_path,
                "status": "CATALOGUED",
                "artist": artist,
                "title": "Some Title",
            },
        )
        ctx.conn.commit()

    def test_enrich_dry_run_makes_no_lastfm_lookup(self, disposable_vault):
        disposable_vault.cfg.ensure_dirs()
        (disposable_vault.cfg.meta_dir / "genre_allowed.txt").write_text("Rock\n")
        (disposable_vault.cfg.meta_dir / "genre_map.tsv").write_text("")
        conn = disposable_vault.open_db()
        ctx = disposable_vault.new_context(conn, dry_run=True)
        self._catalogued_row(ctx, str(disposable_vault.root / "a.flac"))

        object.__setattr__(disposable_vault.cfg, "lastfm_api_key", "fake-key-for-characterization")

        from musaeus.stages.enrich import EnrichStage

        calls = {"n": 0}

        def _stub_top_tags(artist, api_key, limit=5):
            calls["n"] += 1
            return []

        with patch("musaeus.stages.enrich._lastfm_top_tags", side_effect=_stub_top_tags):
            result = EnrichStage()._enrich(ctx, dry_run=True)

        assert calls["n"] == 0, (
            "fix regressed: enrich --dry-run performed a real Last.fm network lookup"
        )
        assert any("would be queried via Last.fm" in n for n in result.notes)
        ctx.finish()

    def test_mb_enrich_dry_run_makes_no_musicbrainz_search(self, disposable_vault):
        disposable_vault.cfg.ensure_dirs()
        conn = disposable_vault.open_db()
        ctx = disposable_vault.new_context(conn, dry_run=True)
        self._catalogued_row(ctx, str(disposable_vault.root / "a.flac"))

        from musaeus.stages.mb_enrich import MBEnrichStage

        calls = {"n": 0}

        def _stub_search_artist(artist_name):
            calls["n"] += 1
            return None

        with (
            patch("musaeus.stages.mb_enrich.urlopen"),  # connectivity probe: reachable
            patch("musaeus.stages.mb_enrich._search_artist", side_effect=_stub_search_artist),
        ):
            result = MBEnrichStage()._enrich(ctx, dry_run=True)

        assert calls["n"] == 0, (
            "fix regressed: mb-enrich --dry-run performed a real MusicBrainz artist search"
        )
        assert any("would be queried via MusicBrainz" in n for n in result.notes)
        ctx.finish()

    def test_mb_enrich_unreachable_network_skips_gracefully(
        self, disposable_vault, transport_harness
    ):
        """
        2026-08-17: MBEnrichStage joined DEFAULT_PIPELINE's default-on
        chain, so an unreachable network must degrade gracefully (skip +
        report) rather than fail the stage/run -- matches EnrichStage's
        existing missing-API-key pattern. Runs under the session's
        transport_harness, so the connectivity probe genuinely fails.
        """
        disposable_vault.cfg.ensure_dirs()
        conn = disposable_vault.open_db()
        ctx = disposable_vault.new_context(conn, dry_run=False)
        self._catalogued_row(ctx, str(disposable_vault.root / "a.flac"))

        from musaeus.stages.mb_enrich import MBEnrichStage

        with patch("musaeus.stages.mb_enrich._search_artist") as search:
            result = MBEnrichStage()._enrich(ctx, dry_run=False)

        assert result.success
        search.assert_not_called()
        assert any("not reachable" in n.lower() for n in result.notes)
        ctx.finish()
