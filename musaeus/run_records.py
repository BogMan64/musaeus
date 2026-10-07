"""A run's own log, a copy of its records beside the library, and a cap on how many are kept.

Grey, 2026-09-24, planning a fresh end-to-end run he will start himself:

  * "whenever a run adds tracks to ALAC_Library, keep a copy of that run's
    log and report with the library" -- beside it, never inside it:
    ``Libraries/ALAC_Library_Run_Logs/<run_id>/``. Inside ALAC_Library the
    copies would be files the catalogue does not know, which doctor and every
    library scan would have to learn to ignore.
  * keep 10 of each kind, "and disregard the rest".

Why the log needed inventing: ``musaeus run`` logged to the terminal only.
The logs under RUNS/LOGS were written by wrapper scripts that captured
stdout; a run started by hand left no log at all, so the one record a person
running it alone would need was the one thing not kept.
"""

from __future__ import annotations

import contextlib
import logging
import re
import shutil
import sys
from collections import defaultdict
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .context import StageResult
from .handoff import _crash_reports, act_of

if TYPE_CHECKING:
    from .context import RunContext

logger = logging.getLogger(__name__)

KEEP = 10
RUN_LOGS_DIRNAME = "ALAC_Library_Run_Logs"

_FORMAT = "%(asctime)s  %(levelname)-8s  %(name)s  %(message)s"


class _Tee:
    """Write to the terminal and the run log. print() output is half of what a
    run says, and a log that captured only `logging` would miss the stage
    summaries entirely."""

    def __init__(self, stream, log):
        self._stream, self._log = stream, log

    def write(self, text):
        self._stream.write(text)
        # A log closed underneath us still leaves the terminal its output.
        with contextlib.suppress(ValueError):
            self._log.write(text)
        return len(text)

    def flush(self):
        self._stream.flush()
        with contextlib.suppress(ValueError):
            self._log.flush()

    def __getattr__(self, name):
        return getattr(self._stream, name)


class RunLog:
    """Everything the run prints or logs, also written to RUNS/LOGS/run_<run_id>.log."""

    def __init__(self, runs_root: Path, run_id: str) -> None:
        logs = Path(runs_root) / "LOGS"
        logs.mkdir(parents=True, exist_ok=True)
        self.path = logs / f"run_{run_id}.log"
        # Held open for the whole run and closed by close(); a with-block
        # cannot span the run. Line-buffered so an interrupted run's log
        # ends at its last complete line.
        self._fh = open(self.path, "a", encoding="utf-8", buffering=1)  # noqa: SIM115
        self._handler = logging.StreamHandler(self._fh)
        self._handler.setFormatter(logging.Formatter(_FORMAT, datefmt="%H:%M:%S"))
        self._saved = (sys.stdout, sys.stderr)
        # The handler writes to the file directly; stdout/stderr are teed for
        # print(). Root-logger records reach the terminal through their own
        # StreamHandler on the ORIGINAL stderr, so nothing is written twice.
        logging.getLogger().addHandler(self._handler)
        sys.stdout = _Tee(sys.stdout, self._fh)
        sys.stderr = _Tee(sys.stderr, self._fh)

    def close(self) -> None:
        """Idempotent: the run closes it on every return, atexit again."""
        if self._fh.closed:
            return
        # Restore only what we replaced; if something else has swapped the
        # streams since, leave theirs alone rather than undo it.
        if isinstance(sys.stdout, _Tee) and sys.stdout._log is self._fh:
            sys.stdout = self._saved[0]
        if isinstance(sys.stderr, _Tee) and sys.stderr._log is self._fh:
            sys.stderr = self._saved[1]
        logging.getLogger().removeHandler(self._handler)
        self._fh.close()


def added_to_library(stage_results) -> int:
    """How many files Finalize placed in ALAC_Library this run."""
    return sum(
        getattr(r, "files_changed", 0) or 0
        for r in stage_results
        if getattr(r, "stage_name", "") == "finalize" and not getattr(r, "dry_run", False)
    )


def filed_this_run(ctx: Any) -> int:
    """How many tracks this run filed, counting a Finalize that never finished.

    added_to_library reads Finalize's StageResult, which a Ctrl-C during
    Finalize, or a crash part-way, never records -- though files were moved.
    Finalize logs one FINALIZE_MOVE per file as it goes, so the event log
    knows what the results do not (cloud review of #37, 2026-09-25).
    """
    counted = added_to_library(ctx.stage_results)
    try:
        logged = ctx.conn.execute(
            "SELECT COUNT(*) FROM events WHERE run_id = ? AND event_type = 'FINALIZE_MOVE'",
            (ctx.run_id,),
        ).fetchone()[0]
    except Exception:  # noqa: BLE001 - bookkeeping must not sink a run
        logged = 0
    return max(int(counted), int(logged))


def publish(libraries: Path, runs_root: Path, run_id: str, extra: list[Path | None]) -> Path:
    """Copy this run's log, reports and failure files beside the library."""
    dest = Path(libraries) / RUN_LOGS_DIRNAME / run_id
    dest.mkdir(parents=True, exist_ok=True)
    runs_root = Path(runs_root)
    found = [p for p in extra if p is not None]
    for sub in ("HANDOFFS", "FAILURES"):
        found += sorted((runs_root / sub).glob(f"*{run_id}*"))
    for p in found:
        if Path(p).is_file():
            shutil.copy2(p, dest / Path(p).name)
    return dest


_RUN_KEY = re.compile(r"(\d{8}T\d{6}Z?(?:_[0-9a-f]{6})?)")


def _group_key(name: str) -> str:
    """Files of one run share a key: its run id, or a tool's timestamp.
    "ForClaudeHandoff_run_..._512d34_act2.md" belongs with its run's main doc."""
    m = _RUN_KEY.search(name)
    return m.group(1) if m else name


def prune(
    folder: Path, keep: int = KEEP, *, group_by_run: bool = False, prefix_kinds: bool = False
) -> list[Path]:
    """Keep the newest `keep` entries of `folder`; remove the rest. Returns what went.

    group_by_run: count a run's several files as one entry (reports).
    prefix_kinds: keep `keep` of EACH kind, the kind being the name before
    "_run_" (recovery holds finalize_run_*, canonicalize_run_*, ...).
    """
    folder = Path(folder)
    if not folder.is_dir():
        return []
    entries = [p for p in folder.iterdir() if not p.name.startswith(".")]
    kinds: dict[str, dict[str, list[Path]]] = defaultdict(lambda: defaultdict(list))
    for p in entries:
        kind = p.name.split("_run_", 1)[0] if prefix_kinds else ""
        key = _group_key(p.name) if group_by_run else p.name
        kinds[kind][key].append(p)
    gone: list[Path] = []
    for groups in kinds.values():
        ordered = sorted(
            groups.values(), key=lambda ps: max(q.stat().st_mtime for q in ps), reverse=True
        )
        for ps in ordered[keep:]:
            for p in ps:
                if p.is_dir() and not p.is_symlink():
                    shutil.rmtree(p)
                else:
                    p.unlink()
                gone.append(p)
    return gone


def prune_backups(meta_dir: Path, keep: int = KEEP) -> list[Path]:
    """Keep the newest `keep` backups of each MetaData file ("MasterLaw.csv.bak...").

    Only names containing ".bak" are candidates, grouped by what comes before
    it -- a live rulings file never contains ".bak", so it can never be one.
    """
    meta_dir = Path(meta_dir)
    by_file: dict[str, list[Path]] = defaultdict(list)
    for p in meta_dir.iterdir():
        if p.is_file() and ".bak" in p.name:
            by_file[p.name.split(".bak", 1)[0]].append(p)
    gone = []
    for files in by_file.values():
        for p in sorted(files, key=lambda q: q.stat().st_mtime, reverse=True)[keep:]:
            p.unlink()
            gone.append(p)
    return gone


def prune_all(runs_root: Path, libraries: Path, meta_dir: Path, keep: int = KEEP) -> int:
    runs_root = Path(runs_root)
    n = 0
    n += len(prune(runs_root / "LOGS", keep, group_by_run=True))
    n += len(prune(runs_root / "HANDOFFS", keep, group_by_run=True))
    n += len(prune(runs_root / "FAILURES", keep, group_by_run=True))
    n += len(prune(runs_root / "recovery", keep, prefix_kinds=True))
    n += len(prune(Path(libraries) / RUN_LOGS_DIRNAME, keep))
    n += len(prune_backups(meta_dir, keep))
    return n


def count_problems(stage_results: list[StageResult]) -> int:
    """How many things in these results want a look: one per error line, plus
    one per failed stage or failed self-check that left no error line."""
    n = 0
    for r in stage_results:
        # A stage can report success and still have files that errored with no
        # error line -- the handoff doc counts that as a problem, so this must
        # too, or the console says "no problems" beside a doc that lists one.
        n += len(r.errors) or (0 if r.success and not r.files_errored else 1)
        if r.verified is False:
            n += max(1, len(r.verify_notes))
    return n


def write_problems_tsv(ctx: RunContext) -> Path | None:
    """Every problem the run has hit so far, one row each, uncapped.

    The reports cap each list so they can be pasted into a chat; a 2,000-file
    run can have hundreds of lines, and reviewing "there was this, and this,
    and this" afterwards needs all of them (Grey, 2026-09-24). Rewritten after
    every act, so an interrupted run still leaves the list up to that point.
    Opens in a spreadsheet.
    """
    try:
        out = ctx.runs_root / "LOGS" / f"run_{ctx.run_id}_problems.tsv"
        out.parent.mkdir(parents=True, exist_ok=True)
        rows = []
        for r in ctx.stage_results:
            act = act_of(r.stage_name) or ""
            if not r.success and not r.errors:
                rows.append((act, r.stage_name, "stage failed", "; ".join(r.notes)[:500]))
            for e in r.errors:
                rows.append((act, r.stage_name, "error" if not r.success else "problem", e))
            if r.verified is False:
                for v in r.verify_notes or ["self-check failed"]:
                    rows.append((act, r.stage_name, "self-check failed", v))
        for c in _crash_reports(ctx.runs_root, ctx.run_id):
            stage = c.get("stage", "")
            where = f" (while on: {c['last_item']})" if c.get("last_item") else ""
            rows.append(
                (
                    act_of(stage) or "",
                    stage,
                    "crash",
                    f"{c.get('exception_type', '?')}: {c.get('exception_message', '')}{where}",
                )
            )
        clean = [tuple(str(x).replace("\t", " ").replace("\n", " ") for x in row) for row in rows]
        out.write_text(
            "act\tstage\tkind\tmessage\n" + "".join("\t".join(row) + "\n" for row in clean),
            encoding="utf-8",
        )
        return out
    except Exception as exc:  # noqa: BLE001 -- a report may never cost a run
        logger.warning("[handoff] could not write the problems list: %s", exc)
        return None
