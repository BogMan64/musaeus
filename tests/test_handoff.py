"""One self-contained document per run, for a session with no tools.

Built for 2026-09-08: after Opus 5 access ends, whoever looks at a
failed run may be on claude.ai's free tier with no file or CLI access.
This reads what already exists (StageResult.verified/errors,
FAILURES/*.json) rather than adding a second tracking mechanism -- see
handoff.py's own module docstring for why that distinction matters.
"""

from __future__ import annotations

import inspect
import json
from pathlib import Path
from types import SimpleNamespace

from musaeus import cli as cli_mod
from musaeus.context import StageResult
from musaeus.handoff import write_handoff_doc


def _ctx(tmp_path: Path, stage_results: list[StageResult], run_id: str = "run_test"):
    return SimpleNamespace(
        stage_results=stage_results, runs_root=tmp_path / "RUNS", run_id=run_id
    )


def _ok(stage: str, **kw) -> StageResult:
    return StageResult(stage_name=stage, success=True, **kw)


def test_a_clean_run_writes_nothing(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path, [_ok("ingest"), _ok("sentinel"), _ok("scholar")])
    assert write_handoff_doc(ctx) is None
    assert not (tmp_path / "RUNS" / "HANDOFFS").exists()


def test_a_verification_failure_is_the_priority_section(tmp_path: Path) -> None:
    """verified=False is a stage that completed and reported success, but
    its OWN after-the-fact check found the claimed effect did not
    happen -- the class of bug this whole project keeps finding, and the
    reason the file should say to look here first."""
    bad = StageResult(
        stage_name="corrupt", success=True, verified=False,
        verify_notes=["Song.m4a: decode failed: Input buffer exhausted"],
    )
    ctx = _ctx(tmp_path, [_ok("ingest"), bad])
    path = write_handoff_doc(ctx)

    assert path is not None
    # The name is half the point: a fresh session is handed this doc, and
    # must not mistake it for one of Grey's hand-written MUSAEUS_HANDOFF_*
    # notes. Asserted positively -- naming the value that is wanted, not
    # the one to avoid.
    assert path.name == "ForClaudeHandoff_run_test.md"
    text = path.read_text()
    assert "Verification failures" in text
    assert "corrupt" in text
    assert "Song.m4a: decode failed" in text
    assert "run_test" in text


def test_a_stage_that_reported_failure_without_crashing_is_captured(tmp_path: Path) -> None:
    failed = StageResult(
        stage_name="mb_enrich", success=False, files_errored=3,
        errors=["network timeout for 'Some Artist'"],
    )
    ctx = _ctx(tmp_path, [failed])
    path = write_handoff_doc(ctx)

    assert path is not None
    text = path.read_text()
    assert "Stages that reported failure" in text
    assert "mb_enrich" in text
    assert "network timeout for 'Some Artist'" in text


def test_a_crash_report_is_read_and_inlined_not_just_pointed_at(tmp_path: Path) -> None:
    """The whole point for a tool-less reader: a path to a JSON file is
    useless to a session that cannot open it. The traceback must be
    inline in the markdown itself."""
    failures_dir = tmp_path / "RUNS" / "FAILURES"
    failures_dir.mkdir(parents=True)
    report = {
        "stage": "canonicalize",
        "phase": "run",
        "run_id": "run_test",
        "dry_run": False,
        "occurred_at": "2026-09-08T00:00:00+00:00",
        "exception_type": "CanonicalizeError",
        "exception_message": "ffmpeg exited 1",
        "traceback": "Traceback (most recent call last):\n  File ...\nCanonicalizeError: ffmpeg exited 1",
        "last_item": "/vault/Some Album/track.flac",
    }
    (failures_dir / "canonicalize_run_test_20260908T000000000000Z.json").write_text(
        json.dumps(report)
    )

    ctx = _ctx(tmp_path, [], run_id="run_test")
    path = write_handoff_doc(ctx)

    assert path is not None
    text = path.read_text()
    assert "Stage crashes" in text
    assert "canonicalize" in text
    assert "CanonicalizeError" in text
    assert "ffmpeg exited 1" in text
    assert "Traceback (most recent call last)" in text
    assert "/vault/Some Album/track.flac" in text


def test_only_this_runs_failure_reports_are_included(tmp_path: Path) -> None:
    """A stale FAILURES/ report from a DIFFERENT, earlier run must not
    bleed into today's handoff doc -- that would make every run's doc
    grow forever instead of describing THIS run."""
    failures_dir = tmp_path / "RUNS" / "FAILURES"
    failures_dir.mkdir(parents=True)
    (failures_dir / "ingest_run_OLD_20260101T000000000000Z.json").write_text(
        json.dumps({"stage": "ingest", "exception_type": "OldError",
                    "exception_message": "from a previous run", "traceback": "...",
                    "phase": "run", "run_id": "run_OLD", "occurred_at": "x"})
    )

    bad = StageResult(stage_name="corrupt", success=True, verified=False,
                       verify_notes=["today's problem"])
    ctx = _ctx(tmp_path, [bad], run_id="run_TODAY")
    path = write_handoff_doc(ctx)

    text = path.read_text()
    assert "today's problem" in text
    assert "OldError" not in text
    assert "from a previous run" not in text


def test_a_run_with_both_kinds_of_issue_gets_both_sections(tmp_path: Path) -> None:
    bad = StageResult(stage_name="corrupt", success=True, verified=False,
                       verify_notes=["decode mismatch"])
    failed = StageResult(stage_name="mb_enrich", success=False,
                          errors=["timeout"])
    ctx = _ctx(tmp_path, [bad, failed])
    text = write_handoff_doc(ctx).read_text()
    assert "Verification failures" in text
    assert "Stages that reported failure" in text


def test_an_unreadable_failure_report_is_skipped_not_fatal(tmp_path: Path) -> None:
    failures_dir = tmp_path / "RUNS" / "FAILURES"
    failures_dir.mkdir(parents=True)
    (failures_dir / "x_run_test_20260101T000000000000Z.json").write_text("{not valid json")

    bad = StageResult(stage_name="corrupt", success=True, verified=False,
                       verify_notes=["real problem"])
    ctx = _ctx(tmp_path, [bad], run_id="run_test")
    # must not raise despite the corrupt report sitting alongside a real issue
    path = write_handoff_doc(ctx)
    assert path is not None
    assert "real problem" in path.read_text()


def test_the_doc_orients_a_tool_less_reader(tmp_path: Path) -> None:
    """The file has to work for someone who cannot verify anything --
    it should say so, not just dump data."""
    bad = StageResult(stage_name="corrupt", success=True, verified=False,
                       verify_notes=["x"])
    ctx = _ctx(tmp_path, [bad])
    text = write_handoff_doc(ctx).read_text()
    assert "no file or tool access" in text.lower()
    assert "cannot verify" in text.lower()


def test_run_pipeline_actually_calls_write_handoff_doc() -> None:
    """A unit test on write_handoff_doc alone cannot catch cli.py's
    caller being removed or never wired in the first place -- the same
    gap a wiring-level test closed for db.ensure_columns earlier this
    project. Source-level rather than a full pipeline run: _run_pipeline
    needs a real vault, DB, and stage list to execute end to end, which
    is what test_p0_01_characterization.py etc. already cover; this only
    needs to prove the call site exists."""
    import inspect

    import musaeus.cli as cli_mod

    source = inspect.getsource(cli_mod._run_pipeline)
    assert "write_handoff_doc(ctx)" in source


def test_a_stage_with_thousands_of_errors_stays_pasteable(tmp_path: Path) -> None:
    """The document exists to be PASTED into a session with no file access,
    so its size is a correctness property, not formatting. Scholar appends
    one "Missing: <path>" error per row whose file has gone -- clearing a
    3,000-file tree out of INBOX mid-run produces exactly that -- and
    rendering all of them made a ~400KB file that could not be pasted
    anywhere. Asserts the positive: twenty entries, then an honest count."""
    flood = StageResult(
        stage_name="scholar", success=False, files_errored=3094,
        errors=[f"Missing: /vault/INBOX/track_{n}.m4a" for n in range(3094)],
    )
    ctx = _ctx(tmp_path, [flood])
    text = write_handoff_doc(ctx).read_text()

    assert text.count("ERROR: Missing:") == 20
    assert "- ... and 3074 more" in text
    # the scale must survive the truncation, not be hidden by it
    assert "3094 file(s) errored" in text
    assert len(text) < 20_000


def test_the_console_printer_is_bounded_too(tmp_path: Path) -> None:
    """handoff.py was capped first, but cli.py's stage-result printer walked
    the same unbounded list -- so clearing a directory out of INBOX still
    put 3,133 ERROR lines on the terminal. Both now go through
    head_with_remainder, which is why it lives beside StageResult rather
    than being inlined an eighth time. Asserts the split itself, the piece
    both callers depend on."""
    from musaeus.context import MAX_LISTED, TAIL_LISTED, head_with_remainder

    head, tail, hidden = head_with_remainder([f"Missing: /vault/{n}.m4a" for n in range(3094)])
    assert len(head) + len(tail) == MAX_LISTED == 20
    assert len(tail) == TAIL_LISTED == 5
    assert hidden == 3074
    # the tail really is the END of the list, not more of the head
    assert tail[-1] == "Missing: /vault/3093.m4a"

    # a short list is returned whole, with nothing to announce
    head, tail, hidden = head_with_remainder(["one", "two"])
    assert head == ["one", "two"]
    assert tail == [] and hidden == 0

    # and cli.py actually routes through it rather than looping the raw list
    source = inspect.getsource(cli_mod)
    assert "head_with_remainder(result.errors)" in source
    assert "for err in result.errors:" not in source


def test_the_undo_record_survives_truncation(tmp_path: Path) -> None:
    """Truncating from the head threw away the wrong end. Stages append their
    SUMMARY last -- dupe_resolver adds the manifest and restore-script paths
    after every per-file note -- so a run with 21+ notes dropped the only
    record of how to undo a batch of irreversible moves, from the console AND
    from this doc, while still looking like it reported successfully.

    Found in review 2026-09-04, hours after the cap shipped and with a dedupe
    re-run already queued that would have produced exactly this shape."""
    notes = [f"[EXACT] skipped stale row {n}" for n in range(22)] + [
        "moved 5884 file(s) to review",
        "manifest: /vault/DUPES_MOVED_FOR_REVIEW/2026-09-04/manifest.json",
        "restore script: /vault/DUPES_MOVED_FOR_REVIEW/2026-09-04/restore.sh",
    ]
    failed = StageResult(stage_name="dupe-resolver", success=False,
                         files_errored=1, errors=["one bad path"], notes=notes)
    text = write_handoff_doc(_ctx(tmp_path, [failed])).read_text()

    assert "restore script: /vault/DUPES_MOVED_FOR_REVIEW/2026-09-04/restore.sh" in text
    assert "manifest: /vault/DUPES_MOVED_FOR_REVIEW/2026-09-04/manifest.json" in text
    assert "moved 5884 file(s) to review" in text
    assert "... and 5 more" in text

    # the elision must sit BETWEEN head and tail, never after both -- otherwise
    # the tail reads as the entries that immediately followed the head
    assert text.index("... and 5 more") < text.index("restore script:")
    assert text.index("skipped stale row 0") < text.index("... and 5 more")


def test_a_handoff_failure_is_loud_not_silent() -> None:
    """On 2026-09-05 a 42-hour run lost its doc and said nothing: no file, no
    traceback, no log line. cli.py deferred `from .handoff import ...`, so it
    read a handoff.py that did not exist at startup against a musaeus.context
    that had been in sys.modules since startup -- a mixture neither version
    would produce alone.

    Two halves. The import is now eager, so a run holds one coherent
    snapshot; and the call is guarded, so if it fails anyway the run says so
    and exits non-zero instead of reporting success."""
    source = inspect.getsource(cli_mod)

    # eager: imported at module level, never inside the function
    assert "\nfrom .handoff import write_handoff_doc" in source
    assert "    from .handoff import write_handoff_doc" not in source

    # guarded: a raising write_handoff_doc must not escape, must warn, must
    # mark the run failed
    body = source[source.index("handoff_path = write_handoff_doc(ctx)") - 400:]
    assert "try:" in body
    assert "could not write the ForClaudeHandoff doc" in body
    assert "exit_code = 1" in body
