"""The end of a run says what each problem means and how to fix it.

Grey, 2026-10-28, on Streamrip batch 1: six damaged downloads made a clean run
end with a bare "Pipeline finished with errors." Grey: "yes please add more
detail. Enough so that a FreeAI can interpret and help with any corrections".
A free AI has no access to the code, so the run must say it in words: what
went wrong, whether it is a fault in the file or in MUSAEUS, and what to do.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from musaeus.context import StageResult
from musaeus.handoff import UNKNOWN_KIND, explain, problem_summary, write_handoff_doc

#: The real line from run_20261029T032802Z_16b94d.
DAMAGED = (
    "audio does not decode cleanly: 05. THE 12 TENORS - Bohemian Rhapsody.flac: "
    "decodes with errors: [flac @ 0x55dbb0166480] invalid residual"
)


def _failed(*errors: str, stage: str = "sentinel") -> StageResult:
    return StageResult(
        stage_name=stage, success=False, files_errored=len(errors), errors=list(errors)
    )


def test_a_damaged_file_is_explained_as_a_fault_in_the_file() -> None:
    [(meaning, fix, n)] = explain([DAMAGED, DAMAGED.replace("05.", "12.")])
    assert n == 2
    assert "damaged" in meaning and "not in MUSAEUS" in meaning
    assert "QUARANTINE/corrupted" in meaning
    assert "Get the song again" in fix and "No code change" in fix


def test_a_line_of_no_known_kind_says_it_may_need_a_code_fix() -> None:
    assert explain(["something nobody planned for"]) == [(*UNKNOWN_KIND, 1)]
    assert "code fix" in UNKNOWN_KIND[0]


def test_each_kind_is_explained_once_with_its_count() -> None:
    got = explain([DAMAGED, "x.m4a: file missing on disk", DAMAGED])
    assert [n for _, _, n in got] == [2, 1]


def test_the_report_explains_each_problem_under_its_stage(tmp_path: Path) -> None:
    ctx = SimpleNamespace(stage_results=[_failed(DAMAGED)], runs_root=tmp_path, run_id="run_t")
    text = write_handoff_doc(ctx).read_text()
    failure = text.split("## Stages that reported failure", 1)[1]
    assert "What this means" in failure and "Get the song again" in failure


def test_a_stages_notes_are_not_mixed_in_with_its_errors(tmp_path: Path) -> None:
    """The batch 1 report listed "DUPLICATE: a == b" notes straight after the
    ERROR lines, so a reader could take 135 normal duplicates for errors."""
    r = _failed(DAMAGED)
    r.notes.append("DUPLICATE: a.flac == b.m4a")
    ctx = SimpleNamespace(stage_results=[r], runs_root=tmp_path, run_id="run_t")
    failure = write_handoff_doc(ctx).read_text().split("## Stages that reported failure", 1)[1]
    assert failure.index("not errors") < failure.index("DUPLICATE: a.flac")


def test_the_screen_summary_names_the_stage_an_example_and_the_fix() -> None:
    lines = "\n".join(
        problem_summary([StageResult(stage_name="ingest", success=True), _failed(DAMAGED)])
    )
    assert "sentinel" in lines and "ingest" not in lines
    assert "invalid residual" in lines
    assert "Get the song again" in lines
    assert "any AI" in lines


def test_a_clean_run_has_no_screen_summary() -> None:
    assert problem_summary([StageResult(stage_name="ingest", success=True)]) == []


def test_a_failed_self_check_is_summarised_too() -> None:
    bad = StageResult(
        stage_name="organize", success=True, verified=False, verify_notes=["3 files not moved"]
    )
    lines = "\n".join(problem_summary([bad]))
    assert "organize" in lines and "3 files not moved" in lines and "own check" in lines


def test_the_end_of_a_real_run_prints_the_summary(tmp_path, monkeypatch, capsys) -> None:
    from musaeus import cli
    from musaeus.config import MusicConfig
    from musaeus.context import RunContext
    from musaeus.stages.base import BaseStage

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
    monkeypatch.setattr(cli, "get_config", lambda: cfg)
    monkeypatch.setattr(cli, "_RESUME_FILE", tmp_path / "resume_state.json")
    monkeypatch.setattr(cli.sys.stdin, "isatty", lambda: False, raising=False)

    class _Sentinel(BaseStage):
        NAME = "sentinel"
        CLAIMS_EFFECT = False

        def validate(self, ctx: RunContext) -> None:
            return None

        def run(self, ctx: RunContext) -> StageResult:
            result = self._make_result(dry_run=False)
            result.success = False
            result.files_errored = 1
            result.errors.append(DAMAGED)
            ctx.record_stage(result)
            return result

        def dry_run(self, ctx: RunContext) -> StageResult:
            raise NotImplementedError

    assert cli._run_pipeline([_Sentinel], dry_run=False) == 1
    err = capsys.readouterr().err
    assert "Pipeline finished with errors" in err
    assert "What needs attention" in err
    assert "Get the song again" in err


# Codex review of #261 (2026-10-28), three Mediums.


def test_a_failed_self_check_is_explained_in_the_report_too(tmp_path: Path) -> None:
    bad = StageResult(
        stage_name="organize", success=True, verified=False, verify_notes=["3 files not moved"]
    )
    ctx = SimpleNamespace(stage_results=[bad], runs_root=tmp_path, run_id="run_t")
    section = write_handoff_doc(ctx).read_text().split("## Verification failures", 1)[1]
    assert "What this means" in section and "How to fix it" in section


def test_a_failed_stage_with_only_notes_does_not_call_them_not_errors(tmp_path: Path) -> None:
    """BitrotStage fails with notes only ("MASTERS MISSING"); they are the failure."""
    r = StageResult(stage_name="bitrot", success=False)
    r.notes.append("MASTERS MISSING: 2 recording(s) the catalogue still holds are not on disk")
    ctx = SimpleNamespace(stage_results=[r], runs_root=tmp_path, run_id="run_t")
    section = write_handoff_doc(ctx).read_text().split("## Stages that reported failure", 1)[1]
    assert "not errors" not in section
    assert "MASTERS MISSING" in section and "no longer on the disk" in section
    assert "MASTERS MISSING" in "\n".join(problem_summary([r]))


def test_a_db_collision_does_not_promise_the_line_says_if_the_file_was_put_back() -> None:
    """Finalize's line is "x: DB collision on <target>: <exc>"; COULD NOT REVERT is only in the log."""
    [(meaning, fix, _)] = explain(["a.flac: DB collision on /lib/a.m4a: UNIQUE constraint failed"])
    assert "says whether" not in meaning
    assert "both" in fix and "run log" in fix


# CodeRabbit on #261 (2026-10-28).

import pytest  # noqa: E402

from musaeus.handoff import PROBLEM_KINDS  # noqa: E402

_MEANING = dict(
    zip(
        ("damaged", "held", "decoder", "missing", "collision", "read", "tag", "crash"),
        (meaning for _, meaning, _ in PROBLEM_KINDS),
        strict=True,
    )
)


def test_a_file_name_holding_the_words_does_not_pick_the_kind() -> None:
    [(meaning, _, _)] = explain(["Missing: /INBOX/damaged audio.flac"])
    assert meaning == _MEANING["missing"]


@pytest.mark.parametrize(
    ("line", "kind"),
    [
        # The lines the stages write, as they write them.
        (DAMAGED, "damaged"),
        ("a.m4a: damaged audio, not measured", "damaged"),
        (
            "  ⚠ b.m4a: does not decode, moved to /Q/corrupted/b.m4a",
            "damaged",
        ),
        (
            "3 arrival(s) held back: not decoded clean (Act 1's Corrupt decodes them; then Act 3 files them)",
            "held",
        ),
        ("a.m4a: not decoded: ffmpeg missing", "decoder"),
        ("a.m4a: arrival not decoded: ffmpeg missing", "decoder"),
        ("/x/a.m4a: file missing on disk", "missing"),
        ("a.m4a: missing on disk", "missing"),
        ("Missing file, marked GHOST: /x/a.m4a", "missing"),
        ("Missing: /x/a.m4a", "missing"),
        (
            "MASTERS MISSING: 2 recording(s) the catalogue still holds are not on disk",
            "missing",
        ),
        ("a.flac: DB collision on /lib/a.m4a: UNIQUE constraint failed", "collision"),
        ("a.m4a: DB collision, move reverted", "collision"),
        ("a.m4a: could not read file: Permission denied", "read"),
        ("full_hash OS error: a.m4a: [Errno 5]", "read"),
        ("a.m4a: tag write failed", "tag"),
        ("Failure report: /RUNS/FAILURES/x.json", "crash"),
    ],
)
def test_each_stage_line_gets_its_kind(line: str, kind: str) -> None:
    [(meaning, _, _)] = explain([line])
    assert meaning == _MEANING[kind]


def test_damaged_arrivals_noted_without_an_error_line_still_get_the_fix(tmp_path: Path) -> None:
    """CorruptStage notes its quarantined arrivals and writes no error line."""
    r = StageResult(stage_name="corrupt", success=True, files_errored=1)
    r.notes += [
        "1 new arrival(s) do not decode; not filed:",
        "  ⚠ b.m4a: does not decode, moved to /Q/b.m4a",
    ]
    ctx = SimpleNamespace(stage_results=[r], runs_root=tmp_path, run_id="run_t")
    section = (
        write_handoff_doc(ctx).read_text().split("## Problems in stages that still finished", 1)[1]
    )
    assert "Get the song again" in section
    assert "Get the song again" in "\n".join(problem_summary([r]))


def test_the_damaged_text_does_not_say_a_library_file_stays_put() -> None:
    assert "stays where it is" not in _MEANING["damaged"]


# Codex review of #261, round 2.


def test_a_file_name_leading_the_line_does_not_pick_the_kind() -> None:
    assert explain(["damaged audio.m4a: Permission denied"]) == [(*UNKNOWN_KIND, 1)]


def test_the_screen_shows_the_damaged_note_not_the_clean_scan_note() -> None:
    r = StageResult(stage_name="corrupt", success=True, files_errored=1)
    r.notes += [
        "✓ No corrupt files detected",
        "1 new arrival(s) do not decode; not filed:",
        "  ⚠ b.m4a: does not decode, moved to /Q/b.m4a",
    ]
    text = "\n".join(problem_summary([r]))
    assert "No corrupt files detected" not in text
    assert "First note: ⚠ b.m4a: does not decode" in text


# Codex review of #261, round 3.


def test_the_damaged_text_does_not_say_the_file_was_always_moved() -> None:
    """Sentinel alone records a failed decode and moves nothing."""
    assert "MUSAEUS moves the file" not in _MEANING["damaged"]
    assert "when it confirms the damage" in _MEANING["damaged"]
