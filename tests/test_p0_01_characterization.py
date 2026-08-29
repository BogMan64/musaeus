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

    def test_dry_run_does_not_create_the_vault_directory_skeleton(
        self, disposable_vault, monkeypatch, tmp_path
    ):
        """
        Originally an UNSAFE-BY-DESIGN finding: dry-run called
        cfg.ensure_dirs() unconditionally before any stage ran, mkdir()ing
        the whole vault skeleton (INBOX/STAGING/QUARANTINE/RUNS/MetaData/
        ALAC-Library). P0-02 answered that by refusing every dry-run
        outright (exit 2). The refusal is now gone and the defect is fixed
        at its source instead: _run_pipeline() skips ensure_dirs() under
        dry_run, and a preview of a vault with no database says so and
        exits 0 rather than creating one.
        """
        assert not disposable_vault.root.exists()
        rc = self._run_cli_dry_run(disposable_vault, monkeypatch, tmp_path)
        assert rc == 0, "a preview with nothing to preview is not an error"
        assert not disposable_vault.root.exists(), (
            "dry-run must not run cfg.ensure_dirs() -- no vault directory "
            "skeleton may be created by a preview"
        )

    def test_dry_run_does_not_create_the_db(self, disposable_vault, monkeypatch, tmp_path):
        """
        Originally an UNSAFE-BY-DESIGN finding: dry-run created the SQLite
        file via open_db() and committed RUN_START/STAGE_COMPLETE/RUN_END
        to it, because RunContext had no dry_run gate. Now open_db() is
        called with read_only=True, which raises FileNotFoundError rather
        than creating a database that does not exist.
        """
        rc = self._run_cli_dry_run(disposable_vault, monkeypatch, tmp_path)
        assert rc == 0
        assert not disposable_vault.cfg.db_path.exists(), (
            "dry-run must never create the database file"
        )

    def test_dry_run_writes_no_events_to_an_existing_db(
        self, disposable_vault, monkeypatch, tmp_path
    ):
        """The real guarantee, now that previews actually execute.

        Against a database that DOES exist, a full DEFAULT_PIPELINE preview
        must leave it byte-identical: no RUN_START/STAGE_COMPLETE/RUN_END,
        no archive or duplicates rows. RunContext buffers events under
        dry_run and never commits, and the connection itself is read-only,
        so a stage attempting a write fails loudly instead of succeeding.
        """
        import hashlib
        import sqlite3

        from musaeus.db import open_db

        disposable_vault.cfg.ensure_dirs()
        open_db(disposable_vault.cfg.db_path).close()

        def fingerprint() -> str:
            conn = sqlite3.connect(str(disposable_vault.cfg.db_path))
            try:
                return hashlib.sha256("".join(conn.iterdump()).encode()).hexdigest()
            finally:
                conn.close()

        before = fingerprint()
        rc = self._run_cli_dry_run(disposable_vault, monkeypatch, tmp_path)
        after = fingerprint()

        assert rc in (0, 1), f"preview should run, not be refused (rc={rc})"
        assert before == after, "a dry run must leave the database byte-identical"

    def test_dry_run_is_stable_and_side_effect_free_across_repeated_calls(
        self, disposable_vault, monkeypatch, tmp_path
    ):
        """Repeating a preview must stay side-effect-free, not just the first."""
        rc1 = self._run_cli_dry_run(disposable_vault, monkeypatch, tmp_path)
        rc2 = self._run_cli_dry_run(disposable_vault, monkeypatch, tmp_path)
        assert rc1 == 0
        assert rc2 == 0
        assert not disposable_vault.root.exists()
        assert not disposable_vault.cfg.db_path.exists()

    def test_dry_run_does_not_disturb_a_real_runs_resume_state(
        self, disposable_vault, monkeypatch, tmp_path
    ):
        """Resume state is a live run's bookmark; a preview must not touch it.

        Without this, previewing after an interrupted run would rewrite (or
        clear) the bookmark and make the real run forget where it stopped.
        """
        import json

        import musaeus.cli as cli_mod

        resume_file = disposable_vault.config_home / "resume_state.json"
        resume_file.parent.mkdir(parents=True, exist_ok=True)
        original = {"completed": ["SentinelStage"], "all_stages": ["SentinelStage"]}
        resume_file.write_text(json.dumps(original))

        monkeypatch.setattr(cli_mod, "get_config", lambda: disposable_vault.cfg)
        monkeypatch.setattr(cli_mod, "_RESUME_FILE", resume_file)
        cli_mod._run_pipeline(DEFAULT_PIPELINE, dry_run=True)

        assert json.loads(resume_file.read_text()) == original

    def test_characterization_dry_run_makes_no_network_connection(
        self, disposable_vault, monkeypatch, tmp_path, transport_harness
    ):
        """
        SAFE FINDING, and now true for the right reason. DEFAULT_PIPELINE
        includes EnrichStage/MBEnrichStage (default-on, last in the chain)
        and both gate their own network calls behind dry_run (fixed
        2026-08-18). AcousticIDStage does too now -- its per-file fpcalc
        subprocess and rate-limited HTTP lookup used to run under dry_run
        with only the DB write afterwards gated, which was the last
        remaining reason the blanket refusal existed. So a CLI dry-run
        makes zero outbound connections because no stage attempts one,
        not because the command is refused before it starts. This test
        runs under the session-wide transport_harness, so any connection
        attempt raises NetworkAccessDeniedError and fails the test rather
        than silently succeeding.
        """
        transport_harness_attempts_before = len(transport_harness.attempts)
        self._run_cli_dry_run(disposable_vault, monkeypatch, tmp_path)
        assert len(transport_harness.attempts) == transport_harness_attempts_before

    def test_nothing_to_preview_message_is_honest_and_on_stderr(
        self, disposable_vault, monkeypatch, tmp_path, capsys
    ):
        """A preview that could not run must not read as a completed preview.

        When there is no database yet there is genuinely nothing to preview.
        That message goes to stderr, so it cannot be mistaken for real
        preview output on stdout, and it must not claim success or that
        changes were checked -- silence plus a zero exit code would let an
        operator believe a preview had run clean.
        """
        rc = self._run_cli_dry_run(disposable_vault, monkeypatch, tmp_path)
        captured = capsys.readouterr()

        assert rc == 0
        assert captured.out == "", "the nothing-to-preview path must print nothing to stdout"
        stderr_lower = captured.err.lower()
        assert "nothing to preview" in stderr_lower
        assert "without --dry-run" in stderr_lower
        # Must NOT claim a completed preview.
        assert "pipeline complete" not in stderr_lower
        assert "no changes made" not in stderr_lower


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

    This exercises dry_run=False (a real run) against the disposable
    vault's empty inbox -- every other stage completes trivially with zero
    files to process. Resume state is a live-run concern: a preview neither
    consumes nor rewrites it (see test_dry_run_safety.py), so a real run is
    the only thing that can exercise this path.
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
