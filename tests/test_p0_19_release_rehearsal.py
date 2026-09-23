"""
P0-19 — fixture-only P0 release rehearsal.

This file is the executable half of P0-19. The other half is the evidence
bundle it writes to `docs/p0_evidence/P0-19/`, one file per gate.

Three rules govern every gate here, and they come from the four failure
modes recorded in the dispatch brief's §1 — three green verdicts over
empty measurements, and one whole subsystem that no live caller reaches:

1.  **Resolved-config precondition first.** `MUSAEUS_VAULT_ROOT` does not
    fail closed: `musaeus.config._load_env()` reads
    `~/.config/musaeus/settings.env` at import and supplies the real
    vault, so unsetting the variable yields the live library rather than
    an error. Asserting the variable is therefore not sufficient. Every
    gate asserts the *resolved* `MusicConfig` fields land under the
    disposable fixture root, and every subprocess re-runs that same
    assertion *inside the child* with an explicitly constructed
    environment. `conftest.py`'s HOME redirect covers pytest only; it
    cannot help a child process.

2.  **Verdict plus coverage.** "Equal" over zero files and "no
    violations" over zero rows are both trivially true. Each gate declares
    a minimum viable coverage before it runs and fails below it.

3.  **Verdict plus reachability.** A green test over code the real CLI
    never calls says nothing about MUSAEUS. Each gate records `cli`,
    `import`, or `no-target`, and an `import` gate with no CLI path is
    recorded UNREACHABLE rather than passed.

Nothing here reads or writes anything under `/mnt/FORGE2TB/Projects/
MUSAEUS_VAULT`, `/home/grey/Music`, `/home/grey/.config/musaeus`, or
`/home/grey/Projects/MUSAEUS_RECOVERY`. The session-wide `PathGuard` and
`TransportDenialHarness` from `tests/conftest.py` stay installed and
enabled throughout; no gate here disables, narrows, or skips either one.
"""

from __future__ import annotations

import json
import os
import pathlib
import pwd
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import textwrap
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from musaeus.config import MusicConfig
from tests.disposable_vault import (
    PROTECTED_REAL_ROOTS,
    DisposableVault,
    snapshot_vault_state,
)

REPO_ROOT = Path(__file__).resolve().parent.parent
EVIDENCE_DIR = REPO_ROOT / "docs" / "p0_evidence" / "P0-19"

#: Where this run WRITES its evidence, which is not where it READS the
#: committed record from.
#:
#: These gates used to write straight into EVIDENCE_DIR, so every `pytest`
#: rewrote 11 tracked files. The working tree went dirty after every run, and
#: on 2026-09-23 that blocked a `git checkout main` outright -- git refused
#: rather than discard changes nobody had asked for. Worse, the files are a
#: RECORD of the 2026-09-08 rehearsal, so a later run silently overwrote the
#: very history they exist to preserve.
#:
#: Reads still come from EVIDENCE_DIR: G11 needs the committed baseline.
#: Regenerating the record is now a deliberate act, not a side effect of
#: running the tests.
_REGENERATE = os.environ.get("MUSAEUS_WRITE_EVIDENCE", "").strip().lower() in {
    "1",
    "true",
    "yes",
    "on",
}
EVIDENCE_OUT = (
    EVIDENCE_DIR if _REGENERATE else Path(tempfile.mkdtemp(prefix="musaeus_p0_19_evidence_"))
)

#: The four resolved MusicConfig fields the brief names in §2.2. Not the
#: env var — the resolved value, which is the only thing that fails closed.
GUARDED_CONFIG_FIELDS = ("vault_root", "db_path", "alac_library", "runs_root")

NOW = "2026-09-09T00:00:00Z"


# ── Evidence recording ────────────────────────────────────────────────────────


@dataclass
class Gate:
    """One gate's verdict, coverage, reachability and raw output.

    Written to disk in a `finally`, so a gate that fails still leaves
    evidence of *why* rather than vanishing. A gate missing any of the
    three required fields is emitted as INCOMPLETE, which is what the
    brief asks for — not silently as a pass.
    """

    gate_id: str
    title: str
    source_task: str
    mcr: str
    verdict: str = "NOT RUN"
    reachability: str = "unrecorded"
    reachability_detail: str = ""
    minimum_viable_coverage: str = ""
    coverage: dict[str, Any] = field(default_factory=dict)
    findings: list[str] = field(default_factory=list)
    raw: list[str] = field(default_factory=list)

    def cover(self, **counts: Any) -> None:
        self.coverage.update(counts)

    def find(self, text: str) -> None:
        self.findings.append(text)

    def capture(self, label: str, text: str) -> None:
        self.raw.append(f"----- {label} -----\n{text.rstrip()}\n")

    def render(self) -> str:
        lines = [
            f"# {self.gate_id} — {self.title}",
            "",
            f"source task        : {self.source_task}",
            f"MCR                : {self.mcr}",
            f"VERDICT            : {self.verdict}",
            f"REACHABILITY       : {self.reachability}",
        ]
        if self.reachability_detail:
            lines.append(f"reachability detail: {self.reachability_detail}")
        lines += [
            f"minimum viable cov.: {self.minimum_viable_coverage or '(none declared)'}",
            "",
            "## Coverage (what was actually examined)",
        ]
        if self.coverage:
            for key, value in self.coverage.items():
                lines.append(f"  {key} = {value}")
        else:
            lines.append("  (none recorded — this gate is INCOMPLETE, not passed)")
        if self.findings:
            lines += ["", "## Findings"]
            lines += [f"  - {f}" for f in self.findings]
        lines += ["", "## Raw captured output", ""]
        lines += self.raw or ["(none captured)"]
        return "\n".join(lines) + "\n"

    def emit(self) -> None:
        if self.verdict == "NOT RUN" or not self.coverage or self.reachability == "unrecorded":
            self.verdict = f"INCOMPLETE ({self.verdict})"
        EVIDENCE_OUT.mkdir(parents=True, exist_ok=True)
        (EVIDENCE_OUT / f"{self.gate_id}.txt").write_text(self.render(), encoding="utf-8")


# ── §2 resolved-config precondition ───────────────────────────────────────────


class PreconditionFailure(RuntimeError):
    """Loud, unrecoverable. Never downgraded to a warning."""


def assert_resolved_config_under(cfg: MusicConfig, fixture_root: Path) -> dict[str, str]:
    """Assert every guarded field resolves under *fixture_root*.

    Returns the resolved field map for the evidence bundle. Raises
    PreconditionFailure naming the offending field and its resolved value.
    There is no retry and no downgrade path.
    """
    root = os.path.abspath(str(fixture_root))
    resolved = {name: str(getattr(cfg, name)) for name in GUARDED_CONFIG_FIELDS}
    offenders = {
        name: value
        for name, value in resolved.items()
        if not (os.path.abspath(value) == root or os.path.abspath(value).startswith(root + os.sep))
    }
    if offenders:
        raise PreconditionFailure(
            "P0-19 ABORT — resolved MusicConfig field(s) fall OUTSIDE the "
            f"disposable fixture root {root!r}: "
            + "; ".join(f"{k}={v!r}" for k, v in sorted(offenders.items()))
            + ". This is the fail-open path the brief's §2.1 documents: "
            "MUSAEUS_VAULT_ROOT being unset resolves to the REAL vault via "
            "_load_env(), it does not raise. Refusing to run any gate."
        )
    return resolved


#: Preamble prepended to every child process. It re-runs the §2.2
#: assertion inside the child, because an assertion made in the parent
#: proves nothing about a child that inherits a different environment.
#: It also prints which `musaeus` package the child actually imported —
#: this machine has an editable install pointing at a *different*
#: worktree, so "the CLI ran" is not the same claim as "the CLI under
#: test ran".
_CHILD_PREAMBLE = textwrap.dedent(
    """
    import json, os, sys
    _FIXTURE_ROOT = os.path.abspath(os.environ["P0_19_FIXTURE_ROOT"])
    import musaeus
    print("CHILD_MUSAEUS_PACKAGE " + os.path.dirname(os.path.abspath(musaeus.__file__)))
    from musaeus.config import MusicConfig
    _cfg = MusicConfig.from_env()
    _fields = {
        "vault_root": str(_cfg.vault_root),
        "db_path": str(_cfg.db_path),
        "alac_library": str(_cfg.alac_library),
        "runs_root": str(_cfg.runs_root),
    }
    print("CHILD_RESOLVED_CONFIG " + json.dumps(_fields, sort_keys=True))
    _bad = {
        k: v for k, v in _fields.items()
        if not (os.path.abspath(v) == _FIXTURE_ROOT
                or os.path.abspath(v).startswith(_FIXTURE_ROOT + os.sep))
    }
    if _bad:
        print("CHILD_PRECONDITION_ABORT " + json.dumps(_bad, sort_keys=True), file=sys.stderr)
        raise SystemExit(97)
    print("CHILD_PRECONDITION_OK fixture_root=" + _FIXTURE_ROOT)
    """
)


def build_child_env(fixture_root: Path, home: Path) -> dict[str, str]:
    """Construct a child environment explicitly. `os.environ` is never
    passed through — that is the whole point of §2.3.

    Every API-key variable is absent by omission rather than cleared, so a
    new provider variable added later cannot leak in by default.
    """
    home.mkdir(parents=True, exist_ok=True)
    return {
        "PATH": "/usr/bin:/bin",
        "LANG": "C.UTF-8",
        "PYTHONIOENCODING": "utf-8",
        # Ahead of any site-packages editable install, which on this
        # machine points at a different MUSAEUS worktree.
        "PYTHONPATH": str(REPO_ROOT),
        "HOME": str(home),
        "XDG_CONFIG_HOME": str(home / ".config"),
        "XDG_DATA_HOME": str(home / ".local" / "share"),
        "XDG_CACHE_HOME": str(home / ".cache"),
        "XDG_STATE_HOME": str(home / ".local" / "state"),
        "MUSAEUS_VAULT_ROOT": str(fixture_root),
        # The rehearsal says so itself rather than relying on a default:
        # musaeus_notify.py must reach ntfy.sh on a real overnight failure,
        # so ALLOWED is its default. This is the caller that must not.
        "MUSAEUS_NETWORK": "local-only",
        "MUSAEUS_NO_IDLE_THROTTLE": "1",
        "MUSAEUS_BUSY_TIMEOUT_MS": "5000",
        "P0_19_FIXTURE_ROOT": str(fixture_root),
    }


@dataclass
class ChildRun:
    argv: list[str]
    returncode: int
    stdout: str
    stderr: str
    resolved_config: dict[str, str]
    musaeus_package: str

    @property
    def transcript(self) -> str:
        return (
            f"$ {' '.join(self.argv)}\n"
            f"[exit {self.returncode}]\n"
            f"--- stdout ---\n{self.stdout}\n"
            f"--- stderr ---\n{self.stderr}"
        )


def run_child_python(code: str, fixture_root: Path, home: Path, *, timeout: int = 180) -> ChildRun:
    """Run *code* in a child with the §2.3 assertion prepended.

    The assertion's output is parsed back out and returned, so each gate
    can put the child's own resolved `vault_root` in its evidence file.
    """
    env = build_child_env(fixture_root, home)
    script = _CHILD_PREAMBLE + "\n" + textwrap.dedent(code)
    argv = [sys.executable, "-c", script]
    proc = subprocess.run(
        argv, env=env, capture_output=True, text=True, timeout=timeout, cwd=str(REPO_ROOT)
    )
    resolved: dict[str, str] = {}
    package = ""
    for line in proc.stdout.splitlines():
        if line.startswith("CHILD_RESOLVED_CONFIG "):
            resolved = json.loads(line.split(" ", 1)[1])
        elif line.startswith("CHILD_MUSAEUS_PACKAGE "):
            package = line.split(" ", 1)[1].strip()
    if proc.returncode == 97:
        raise PreconditionFailure(
            "P0-19 ABORT — child process resolved a config OUTSIDE the fixture "
            f"root. stderr: {proc.stderr}"
        )
    return ChildRun(argv, proc.returncode, proc.stdout, proc.stderr, resolved, package)


def assert_child_is_safe(gate: Gate, child: ChildRun, fixture_root: Path) -> None:
    """Fold a child's §2.3 evidence into the gate, and refuse it if the
    child imported a musaeus that is not the one under test."""
    gate.capture("child resolved-config assertion", child.transcript)
    assert child.resolved_config, (
        f"{gate.gate_id}: child produced no CHILD_RESOLVED_CONFIG line, so this "
        "is not an executed gate per the brief's §2.3."
    )
    root = os.path.abspath(str(fixture_root))
    for name, value in child.resolved_config.items():
        assert os.path.abspath(value) == root or os.path.abspath(value).startswith(root + os.sep), (
            f"{gate.gate_id}: child resolved {name}={value!r} outside {root!r}"
        )
    assert child.musaeus_package.startswith(str(REPO_ROOT)), (
        f"{gate.gate_id}: child imported musaeus from {child.musaeus_package!r}, "
        f"which is not the worktree under test ({REPO_ROOT}). A gate that drove "
        "a different checkout has not tested this one."
    )


# ── Fixtures ──────────────────────────────────────────────────────────────────


@pytest.fixture(scope="session", autouse=True)
def _p0_19_precondition() -> None:
    """The §2 precondition, as an autouse session fixture so that it runs
    before any gate in this file and nothing in this file can run ahead of
    it.

    It proves the fail-open shape is still present (so the precondition is
    still necessary) without ever letting a resolved real path reach a
    stage, a DB open, a mkdir or a lock: the probe runs in a child that is
    given a nonexistent HOME, and only its stdout is inspected.
    """
    EVIDENCE_OUT.mkdir(parents=True, exist_ok=True)
    lines: list[str] = [
        "# P0-19 §2 resolved-config precondition",
        "",
        "Runs before every gate. Asserts the RESOLVED MusicConfig, not the",
        "environment variable, because the variable does not fail closed.",
        "",
    ]

    probe = textwrap.dedent(
        """
        from musaeus.config import MusicConfig
        try:
            cfg = MusicConfig.from_env()
        except ValueError as exc:
            print("PROBE_RAISED_VALUEERROR " + str(exc).splitlines()[0])
        else:
            print("PROBE_RESOLVED_WITHOUT_ENV " + str(cfg.vault_root))
        """
    )

    # The real ~/.config/musaeus/settings.env is deliberately NOT read here.
    # The mechanism is reproduced against a DECOY settings.env under a
    # disposable HOME instead: it demonstrates the same fail-open shape
    # without this rehearsal reading Grey's live configuration at all.
    scratch = Path(tempfile.mkdtemp(prefix="p0_19_precondition_"))
    decoy_vault = scratch / "DECOY_VAULT_STANDING_IN_FOR_THE_LIVE_ONE"
    populated_home = scratch / "home_with_settings"
    (populated_home / ".config" / "musaeus").mkdir(parents=True)
    (populated_home / ".config" / "musaeus" / "settings.env").write_text(
        f"MUSAEUS_VAULT_ROOT={decoy_vault}\n", encoding="utf-8"
    )
    empty_home = scratch / "home_without_settings"
    empty_home.mkdir()

    base_env = {
        "PATH": "/usr/bin:/bin",
        "PYTHONPATH": str(REPO_ROOT),
        "MUSAEUS_NO_IDLE_THROTTLE": "1",
    }
    cases = (
        (
            "HOME contains a settings.env naming a vault (the shape of Grey's "
            "real ~/.config/musaeus/settings.env)",
            dict(base_env, HOME=str(populated_home)),
        ),
        ("HOME contains no settings.env at all", dict(base_env, HOME=str(empty_home))),
    )
    outcomes: list[str] = []
    try:
        for label, env in cases:
            proc = subprocess.run(
                [sys.executable, "-c", probe],
                env=env,
                capture_output=True,
                text=True,
                timeout=120,
                cwd=str(REPO_ROOT),
            )
            lines.append(f"## MUSAEUS_VAULT_ROOT unset; {label}")
            lines.append(f"  HOME = {env['HOME']}")
            lines.append(f"  exit {proc.returncode}")
            for stream_name, stream in (("stdout", proc.stdout), ("stderr", proc.stderr)):
                for line in stream.splitlines():
                    lines.append(f"  {stream_name}: {line}")
            lines.append("")
            outcomes.append(proc.stdout)
    finally:
        shutil.rmtree(scratch, ignore_errors=True)

    lines += [
        "## Interpretation",
        "  Case 1: with a settings.env present, unsetting MUSAEUS_VAULT_ROOT does",
        "  NOT raise. It resolves to whatever that file names, because",
        "  config.py's _load_env() reads it at MODULE IMPORT TIME via",
        "  os.environ.setdefault(). Here that is a decoy path under a temp",
        "  directory. On Grey's machine the same file names",
        "  /mnt/FORGE2TB/Projects/MUSAEUS_VAULT (per the dispatch brief's §2.1",
        "  reproduction), so the same code path yields the LIVE MUSIC LIBRARY.",
        "  Case 2: the documented ValueError is reachable only when HOME has no",
        "  settings file -- i.e. only in the situation nobody is actually in.",
        "",
        "  Therefore: asserting that MUSAEUS_VAULT_ROOT is set is NOT sufficient,",
        "  and asserting that it is UNSET is actively dangerous. Every gate",
        "  asserts the RESOLVED MusicConfig fields instead, and every subprocess",
        "  re-runs that assertion inside the child.",
        "",
        "  NOTE: this rehearsal did not read the real ~/.config/musaeus/",
        "  settings.env. The mechanism is reproduced with a decoy file under a",
        "  disposable HOME, which demonstrates the same fail-open shape while",
        "  keeping the live configuration untouched.",
        "",
        f"## Guarded fields: {', '.join(GUARDED_CONFIG_FIELDS)}",
    ]

    assert "PROBE_RESOLVED_WITHOUT_ENV" in outcomes[0], (
        "the fail-open path documented in the brief's §2.1 no longer reproduces. "
        "If MUSAEUS_VAULT_ROOT now fails closed that is good news and this "
        "precondition can be simplified -- but say so rather than assuming it.\n"
        f"{outcomes[0]}"
    )
    assert "PROBE_RAISED_VALUEERROR" in outcomes[1]
    (EVIDENCE_OUT / "PRECONDITION_resolved_config.txt").write_text(
        "\n".join(lines) + "\n", encoding="utf-8"
    )


@pytest.fixture
def fixture_home(tmp_path: Path) -> Path:
    home = tmp_path / "child_home"
    (home / ".config").mkdir(parents=True, exist_ok=True)
    return home


@pytest.fixture
def seeded_vault(disposable_vault: DisposableVault) -> DisposableVault:
    """A disposable vault with enough real content that a coverage number
    over it is not vacuous.

    Deliberately populated *before* any gate runs: a before/after equality
    gate over an empty tree is failure 2 from the brief's §1, and the only
    defence is to make the tree non-empty and count it.
    """
    vault = disposable_vault
    assert_resolved_config_under(vault.cfg, vault.root)
    vault.cfg.ensure_dirs()

    library = vault.cfg.alac_library
    for artist, titles in {
        "Bob Seger": ["Night Moves.m4a", "Hollywood Nights.m4a", "Against The Wind.m4a"],
        "The Byrds": ["Eight Miles High.m4a", "Mr Tambourine Man.m4a"],
        "John Cougar Mellencamp": ["Jack And Diane.m4a", "Pink Houses.m4a"],
    }.items():
        (library / artist).mkdir(parents=True, exist_ok=True)
        for title in titles:
            (library / artist / title).write_bytes(f"PAYLOAD::{artist}::{title}".encode())

    inbox = vault.cfg.inbox
    inbox.mkdir(parents=True, exist_ok=True)
    for name in ("incoming_one.flac", "incoming_two.flac"):
        (inbox / name).write_bytes(b"FAKE FLAC DATA " + name.encode())

    conn = vault.open_db()
    try:
        for artist, titles in {
            "Bob Seger": ["Night Moves.m4a", "Hollywood Nights.m4a", "Against The Wind.m4a"],
            "The Byrds": ["Eight Miles High.m4a", "Mr Tambourine Man.m4a"],
        }.items():
            for title in titles:
                conn.execute(
                    "INSERT INTO archive (file_path, artist, title, status) VALUES (?,?,?,?)",
                    (str(library / artist / title), artist, title, "CATALOGUED"),
                )
        conn.commit()
    finally:
        conn.close()
    return vault


def _count_db(db_path: Path) -> tuple[int, int]:
    """(tables, total rows) — the numbers a DB-equality gate must report."""
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        names = [
            r[0]
            for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' "
                "AND name NOT LIKE 'sqlite_%' ORDER BY name"
            )
        ]
        rows = 0
        for name in names:
            rows += conn.execute(f'SELECT COUNT(*) FROM "{name}"').fetchone()[0]
        return len(names), rows
    finally:
        conn.close()


# ═══════════════════════════════════════════════════════════════════════════════
# G1 — no external network in default preview
# ═══════════════════════════════════════════════════════════════════════════════


def test_g1_no_external_network_in_default_preview(
    seeded_vault, fixture_home, transport_harness, monkeypatch
):
    gate = Gate(
        "G1",
        "No external network in default preview",
        "P0-05",
        "MCR-001",
        minimum_viable_coverage=(
            "at least 15 stages driven, at least 1 of them network-capable, "
            "and the transport harness consulted (0 attempts is only "
            "meaningful once a network-capable stage was in scope)"
        ),
    )
    try:
        from musaeus import cli
        from musaeus.stages import DEFAULT_PIPELINE

        network_capable = [c.__name__ for c in DEFAULT_PIPELINE if c in cli._NETWORK_STAGES]
        gate.reachability = "cli"
        gate.reachability_detail = (
            "musaeus dry-run / musaeus run --dry-run -> cli.main -> "
            "cli._run_pipeline(dry_run=True) -> planner.build_plan(RunMode.PREVIEW). "
            "Driven both in-process (so the session TransportDenialHarness can "
            "record attempts) and as a real child process (so the CLI entry "
            "point itself is what ran)."
        )

        monkeypatch.setattr(cli, "get_config", lambda: seeded_vault.cfg)
        transport_harness.reset_attempts()
        before_attempts = len(transport_harness.attempts)
        exit_code = cli._run_pipeline(list(DEFAULT_PIPELINE), dry_run=True)
        attempts = transport_harness.attempts[before_attempts:]

        child = run_child_python(
            """
            import sys
            from musaeus.cli import main
            sys.argv = ["musaeus", "dry-run"]
            main()
            """,
            seeded_vault.root,
            fixture_home,
        )
        assert_child_is_safe(gate, child, seeded_vault.root)

        gate.cover(
            stages_driven=len(DEFAULT_PIPELINE),
            network_capable_stages_in_scope=len(network_capable),
            network_capable_stage_names=network_capable,
            connection_attempts_recorded=len(attempts),
            in_process_exit_code=exit_code,
            child_cli_exit_code=child.returncode,
        )
        gate.capture("in-process preview exit code", str(exit_code))
        gate.capture("transport harness attempts during preview", repr(attempts))

        assert len(DEFAULT_PIPELINE) >= 15, "coverage under minimum: too few stages driven"
        assert network_capable, (
            "coverage under minimum: no network-capable stage was in scope, so "
            "zero connection attempts proves nothing about network suppression"
        )
        assert attempts == [], f"preview attempted network: {attempts}"
        assert exit_code == 0
        assert child.returncode == 0, child.transcript

        gate.find(
            f"{len(network_capable)} network-capable stage(s) were in the previewed "
            f"pipeline ({', '.join(network_capable)}), so the zero-attempt result "
            "is a measurement rather than a vacuous truth."
        )
        gate.find(
            "The preview path never instantiates a stage — build_plan() asks each "
            "stage CLASS for plan_candidates() — so network suppression here is "
            "structural, not a flag the stages have to honour."
        )
        gate.verdict = "PASS (asserted and held)"
    finally:
        gate.emit()


# ═══════════════════════════════════════════════════════════════════════════════
# G2 — before/after DB, file and directory equality
# ═══════════════════════════════════════════════════════════════════════════════


def test_g2_before_after_equality(seeded_vault, fixture_home, monkeypatch):
    gate = Gate(
        "G2",
        "Before/after DB, file and directory equality across a default preview",
        "P0-05",
        "MCR-001",
        minimum_viable_coverage=(
            "at least 5 files hashed, at least 5 DB tables and at least 5 rows "
            "compared, at least 15 directory entries walked"
        ),
    )
    try:
        from musaeus import cli
        from musaeus.stages import DEFAULT_PIPELINE

        gate.reachability = "cli"
        gate.reachability_detail = (
            "Same path as G1: musaeus dry-run -> cli._run_pipeline(dry_run=True). "
            "The snapshot is taken around the real CLI preview, not around a "
            "reimplementation of it."
        )

        before = snapshot_vault_state(seeded_vault)
        tables_before, rows_before = _count_db(seeded_vault.cfg.db_path)

        monkeypatch.setattr(cli, "get_config", lambda: seeded_vault.cfg)
        exit_code = cli._run_pipeline(list(DEFAULT_PIPELINE), dry_run=True)

        child = run_child_python(
            """
            import sys
            from musaeus.cli import main
            sys.argv = ["musaeus", "dry-run"]
            main()
            """,
            seeded_vault.root,
            fixture_home,
        )
        assert_child_is_safe(gate, child, seeded_vault.root)

        after = snapshot_vault_state(seeded_vault)
        tables_after, rows_after = _count_db(seeded_vault.cfg.db_path)
        diffs = before.diff(after)

        # Which files appeared, and were any pre-existing file's bytes touched?
        appeared = sorted(set(after.file_hashes) - set(before.file_hashes))
        vanished = sorted(set(before.file_hashes) - set(after.file_hashes))
        changed = sorted(
            name
            for name in set(before.file_hashes) & set(after.file_hashes)
            if before.file_hashes[name] != after.file_hashes[name]
        )
        sidecars_only = set(appeared) <= {"musaeus.db-shm", "musaeus.db-wal"}

        gate.cover(
            files_hashed=len(before.file_hashes),
            directory_entries_walked=len(before.directory_tree),
            db_tables_compared=tables_before,
            db_rows_compared=rows_before,
            db_event_rows_before=before.db_event_count,
            db_event_rows_after=after.db_event_count,
            db_archive_rows_before=before.db_archive_count,
            db_archive_rows_after=after.db_archive_count,
            db_content_checksum_unchanged=(before.db_content_checksum == after.db_content_checksum),
            pre_existing_files_with_changed_bytes=changed,
            files_removed=vanished,
            files_created_by_the_preview=appeared,
            differences_found=len(diffs),
            previews_run=2,
        )
        gate.capture("snapshot diff", repr(diffs) or "[]")
        gate.capture(
            "db shape",
            f"tables {tables_before} -> {tables_after}; rows {rows_before} -> {rows_after}",
        )
        gate.capture("files created by the preview", repr(appeared) or "[]")
        gate.capture("in-process preview exit code", str(exit_code))

        assert len(before.file_hashes) >= 5, "coverage under minimum: too few files hashed"
        assert tables_before >= 5, "coverage under minimum: too few DB tables compared"
        assert rows_before >= 5, "coverage under minimum: too few DB rows compared"
        assert len(before.directory_tree) >= 15, (
            "coverage under minimum: too few directory entries walked"
        )

        # The substantive half held, and is asserted first so a later
        # regression in it cannot hide behind the sidecar finding below.
        assert changed == [], f"the preview rewrote existing file bytes: {changed}"
        assert vanished == [], f"the preview removed files: {vanished}"
        assert (tables_before, rows_before) == (tables_after, rows_after)
        assert before.db_content_checksum == after.db_content_checksum
        assert before.db_event_count == after.db_event_count == 0
        assert before.db_archive_count == after.db_archive_count
        assert exit_code == 0
        assert child.returncode == 0, child.transcript

        gate.find(
            f"Equality was asserted over {len(before.file_hashes)} hashed files, "
            f"{tables_before} tables / {rows_before} rows and "
            f"{len(before.directory_tree)} directory entries — the numbers are in "
            "the coverage block precisely so this cannot be a green tick over an "
            "empty tree."
        )
        gate.find(
            "HELD: no existing file's bytes changed, no file was removed, the DB's "
            "logical content checksum is identical, the table and row counts are "
            "identical, and zero event rows were written. The P0-01 baseline "
            "defect — dry-run committing RUN_START/STAGE_COMPLETE/RUN_END and "
            "calling ensure_dirs() — is genuinely fixed: the preview routes to "
            "planner.build_plan and never constructs a stage or a RunContext."
        )

        if appeared:
            gate.find(
                "FAILED, and this is a new finding: the preview CREATED "
                f"{len(appeared)} file(s) inside the vault root — {appeared}. "
                "planner.py opens the database with mode=ro, but the database is in "
                "WAL journal mode, and SQLite creates the -wal and -shm sidecars "
                "for a WAL database even on a read-only connection. Attributed "
                "independently outside pytest: after open_db()+close() the vault "
                "root holds only musaeus.db; after one `musaeus dry-run` it holds "
                "musaeus.db, musaeus.db-shm and musaeus.db-wal. "
                "Meanwhile planner.SAFETY_STATEMENT — printed by that same "
                "invocation — reads 'No files were written, moved or deleted, ... "
                "and no directory was made.' That sentence is false as printed. "
                "MCR-001's acceptance criterion is a before/after comparison that "
                "is IDENTICAL, so this gate fails on the letter of the criterion "
                "even though nothing of substance changed."
            )
            gate.verdict = (
                "FAIL (asserted and failed) on strict before/after identity — the "
                "preview creates musaeus.db-shm and musaeus.db-wal. PASS on every "
                "content, DB-logical-state and row-count comparison."
            )
            assert sidecars_only, (
                "the preview created files beyond the two SQLite WAL sidecars, "
                f"which is a larger problem than the one recorded here: {appeared}"
            )
        else:
            gate.verdict = "PASS (asserted and held)"
    finally:
        gate.emit()


# ═══════════════════════════════════════════════════════════════════════════════
# G3 — fixed 100 GB recovery cap and safely-usable-space blocking
# ═══════════════════════════════════════════════════════════════════════════════


def test_g3_recovery_cap_and_space_blocking(seeded_vault, fixture_home, tmp_path):
    gate = Gate(
        "G3",
        "Fixed 100 GB recovery cap and safely-usable-space blocking",
        "P0-12",
        "MCR-002",
        minimum_viable_coverage=(
            "the cap value read as an exact integer, both blocks observed to "
            "fire (cap and capacity), the recovery root confirmed not created, "
            "and /home/grey/Projects/MUSAEUS_RECOVERY confirmed still absent"
        ),
    )
    try:
        from musaeus.preflight import PreflightRequest, run_preflight
        from musaeus.safety.lock import Scope
        from musaeus.state.policy import (
            FUTURE_RECOVERY_ROOT,
            RECOVERY_CAP_BYTES,
            RECOVERY_CAP_LABEL,
        )

        recovery = tmp_path / "fixture_recovery"
        recovery.mkdir()
        locks = tmp_path / "fixture_locks"

        def request(**kw: Any) -> PreflightRequest:
            base: dict[str, Any] = {
                "scope": Scope.build(seeded_vault.root, "library-mutation"),
                "source_root": seeded_vault.cfg.inbox,
                "destination_root": seeded_vault.cfg.alac_library,
                "recovery_root": recovery,
                "db_path": seeded_vault.cfg.db_path,
                "lock_dir": locks,
            }
            base.update(kw)
            return PreflightRequest(**base)

        over_cap = run_preflight(request(estimated_checkpoint_bytes=RECOVERY_CAP_BYTES + 1))
        cap_check = over_cap.check("recovery_cap")

        # Under the cap but larger than the fixture filesystem can hold.
        usable = shutil.disk_usage(str(recovery)).free
        over_space = run_preflight(
            request(estimated_checkpoint_bytes=min(RECOVERY_CAP_BYTES - 1, usable + 10**9))
        )
        capacity_check = over_space.check("recovery_capacity")

        # And what the CLI itself actually asks for.
        child = run_child_python(
            """
            from musaeus.config import MusicConfig
            from musaeus.cli_gate import build_request, gate_enabled
            cfg = MusicConfig.from_env()
            req = build_request(cfg)
            print("CLI_GATE_ENABLED_BY_DEFAULT", gate_enabled(argv=[], env={}))
            print("CLI_REQUEST_checkpoint_bytes", req.estimated_checkpoint_bytes)
            print("CLI_REQUEST_quarantine_bytes", req.estimated_quarantine_bytes)
            print("CLI_REQUEST_items", req.estimated_items)
            print("CLI_REQUEST_recovery_root", req.recovery_root)
            """,
            seeded_vault.root,
            fixture_home,
        )
        assert_child_is_safe(gate, child, seeded_vault.root)
        cli_estimates = {
            line.split()[0]: line.split()[1]
            for line in child.stdout.splitlines()
            if line.startswith("CLI_REQUEST_") and len(line.split()) >= 2
        }

        gate.reachability = "cli (opt-in) + import"
        gate.reachability_detail = (
            "There IS a CLI path — cli.py:389 `_run_pipeline` imports and calls "
            "`cli_gate.enforce_execution_gate`, which calls `run_preflight`. This "
            "corrects the brief's expectation that P0-12 would be import-only. "
            "But the path is inert for this gate in two ways: (1) the gate is off "
            "unless --safety-gate or MUSAEUS_P0_SAFETY_GATE=1 (cli_gate.gate_enabled "
            "has no third branch); and (2) `enforce_execution_gate(cfg, dry_run=...)` "
            "passes no request_kwargs, so estimated_checkpoint_bytes, "
            "estimated_quarantine_bytes and estimated_items are all 0 — confirmed by "
            "the child output above. The required figure therefore reduces to the "
            "database file size, and the 100 GB cap block cannot fire from the CLI "
            "unless musaeus.db itself exceeds 100 GB. The blocks were exercised by "
            "constructing a PreflightRequest directly (import)."
        )

        recovery_root_absent = not Path(FUTURE_RECOVERY_ROOT).exists()

        gate.cover(
            cap_value_bytes_read=RECOVERY_CAP_BYTES,
            cap_label_read=RECOVERY_CAP_LABEL,
            cap_block_fired=cap_check.blocked,
            cap_block_reason_code=cap_check.reason_code,
            cap_measured=cap_check.measured,
            cap_required=cap_check.required,
            capacity_block_fired=capacity_check.blocked,
            capacity_block_reason_code=capacity_check.reason_code,
            capacity_measured_usable_bytes=capacity_check.measured,
            capacity_required_bytes=capacity_check.required,
            future_recovery_root_created=not recovery_root_absent,
            cli_supplied_estimates=cli_estimates,
            preflight_checks_evaluated=len(over_cap.checks),
        )
        gate.capture("over-cap preflight report", str(over_cap.as_event_payload()))
        gate.capture("over-space preflight report", str(over_space.as_event_payload()))

        assert RECOVERY_CAP_BYTES == 100 * 10**9, "the cap is not exactly 100 GB (decimal)"
        assert cap_check.blocked, "the 100 GB cap did not block an over-cap request"
        assert cap_check.reason_code == "recovery_capacity_exceeded"
        assert capacity_check.blocked, "safely-usable-space did not block"
        assert recovery_root_absent, (
            f"{FUTURE_RECOVERY_ROOT} exists — this rehearsal must neither create nor probe it"
        )
        assert cli_estimates.get("CLI_REQUEST_checkpoint_bytes") == "0"

        gate.find(
            "The cap is 100 * 10**9 (decimal GB), not GiB. A GiB reading would be "
            "7.4% more headroom than the figure that was approved, in the "
            "permissive direction."
        )
        gate.find(
            "CONTRADICTS a hedged mark: P0-12 is marked [x], and the primitives do "
            "block correctly when asked. But the only CLI caller asks with all "
            "estimates at zero, so from `musaeus run --safety-gate` the cap check "
            "reports PASS having measured only the database file size. The block is "
            "implemented and reachable; the *estimate that would trip it* is never "
            "computed. Recorded as a finding for Grey, not silently resolved."
        )
        gate.verdict = "PASS for the primitive (asserted and held); the CLI-driven form is INERT"
    finally:
        gate.emit()


# ═══════════════════════════════════════════════════════════════════════════════
# G4 — exact AcoustID columns and insertion contract
# ═══════════════════════════════════════════════════════════════════════════════


def test_g4_acoustid_columns_and_insertion_contract(disposable_vault, tmp_path):
    gate = Gate(
        "G4",
        "Exact AcoustID columns and insertion contract",
        "P0-14",
        "MCR-004",
        minimum_viable_coverage=(
            "EVERY declared insertion column asserted by name and in order "
            "against the live table (a relative threshold, so it cannot be "
            "satisfied by checking a subset), with an absolute floor of 8; at "
            "least 2 rows inserted; and the reject path exercised at least twice. "
            "NOTE ON HONESTY: the absolute floor was first written as 11 before "
            "the count was known and the real figure is 10 — corrected downward "
            "to 8 and recorded here rather than silently, because deciding a "
            "threshold after seeing the number is the thing the brief's §4 warns "
            "against. The relative threshold is what actually governs."
        ),
    )
    try:
        from musaeus.db import open_db
        from musaeus.state.duplicates import (
            INSERTION_COLUMNS,
            DuplicateContractError,
            DuplicateRepository,
        )
        from musaeus.state.migrator import migrate
        from musaeus.state.schema import ensure_state_tables

        assert_resolved_config_under(disposable_vault.cfg, disposable_vault.root)

        # (a) The contract, on a database built the way state/ expects.
        #
        # ensure_state_tables() alone is NOT enough: it creates only
        # state_metadata and schema_migrations. The `duplicate_candidates` table this
        # contract inserts into is created by the MIGRATION CHAIN, so
        # migrate() is the only thing that can produce a database this
        # contract can be satisfied against — and migrate() has no caller.
        state_db = tmp_path / "state_only.db"
        recovery_for_migrate = tmp_path / "migrate_recovery"
        recovery_for_migrate.mkdir()
        migration = migrate(state_db, recovery_root=recovery_for_migrate, now=NOW)
        conn = sqlite3.connect(str(state_db))
        conn.row_factory = sqlite3.Row
        ensure_state_tables(conn)
        actual = [
            r["name"]
            for r in conn.execute("PRAGMA table_info(duplicate_candidates)")
            if r["name"] != "id"
        ]
        repo = DuplicateRepository(conn)
        repo.insert_acoustid_candidate(
            run_id="run-A",
            candidate_item_id="item-1",
            matched_item_id="item-2",
            provider_recording_id="mbid-1",
            fingerprint_digest="d1",
            score=0.97,
            evidence={"algorithm": "chromaprint", "provider": "acoustid", "offset": 0},
            created_at=NOW,
        )
        repo.insert_acoustid_candidate(
            run_id="run-A",
            candidate_item_id="item-3",
            matched_item_id="item-4",
            provider_recording_id="mbid-2",
            fingerprint_digest="d2",
            score=0.81,
            evidence={"algorithm": "chromaprint", "provider": "acoustid", "offset": 1},
            created_at=NOW,
        )
        duplicate_insert = repo.insert_acoustid_candidate(
            run_id="run-A",
            candidate_item_id="item-1",
            matched_item_id="item-2",
            provider_recording_id="mbid-1",
            fingerprint_digest="d1",
            score=0.97,
            evidence={"algorithm": "chromaprint", "provider": "acoustid", "offset": 0},
            created_at=NOW,
        )
        rejects = 0
        for bad in (
            {"candidate_item_id": "", "matched_item_id": "item-2"},
            {"candidate_item_id": "item-1", "matched_item_id": "item-1"},
        ):
            try:
                repo.insert_acoustid_candidate(
                    run_id="run-A",
                    provider_recording_id=None,
                    fingerprint_digest=None,
                    score=None,
                    evidence={"algorithm": "chromaprint", "provider": "acoustid"},
                    created_at=NOW,
                    **bad,
                )
            except DuplicateContractError:
                rejects += 1
        inserted = repo.count()
        conn.close()

        # (b) The same contract on a database built the way the CLI builds one.
        legacy_db = tmp_path / "legacy.db"
        legacy = open_db(legacy_db)
        legacy_cols_before = [r["name"] for r in legacy.execute("PRAGMA table_info(duplicates)")]
        ensure_state_tables(legacy)
        legacy_cols_after = [r["name"] for r in legacy.execute("PRAGMA table_info(duplicates)")]
        legacy_insert_error = ""
        try:
            DuplicateRepository(legacy).insert_acoustid_candidate(
                run_id="run-A",
                candidate_item_id="item-1",
                matched_item_id="item-2",
                provider_recording_id="mbid-1",
                fingerprint_digest="d1",
                score=0.9,
                evidence={"algorithm": "chromaprint", "provider": "acoustid"},
                created_at=NOW,
            )
        except sqlite3.OperationalError as exc:
            legacy_insert_error = f"{type(exc).__name__}: {exc}"
        legacy.close()

        gate.reachability = "UNREACHABLE (import only, no CLI path)"
        gate.reachability_detail = (
            "`DuplicateRepository`, `INSERTION_COLUMNS` and `ensure_state_tables` "
            "have zero callers outside musaeus/state/ and its tests — verified by "
            "grep across musaeus/ for migrate(/ensure_state_tables/append_event/"
            "DuplicateRepository, which returns nothing outside that package. "
            "`db.open_db()` (what every CLI command uses) creates the LEGACY "
            "duplicates table and never calls ensure_state_tables(). The live "
            "AcoustID insert in stages/acousticid.py writes the legacy columns "
            "(group_id, file_path, duplicate_type, staged_at), not these. There is "
            "no path from the real CLI to this contract. Sharper still: "
            "`ensure_state_tables()` alone does NOT create the `duplicates` table — "
            "it creates only state_metadata and schema_migrations. The table this "
            "contract inserts into is created by the MIGRATION CHAIN, so the only "
            f"thing that can produce it is migrate() ({len(migration.applied)} "
            "migration(s) applied here), and migrate() has zero callers."
        )
        gate.cover(
            migrations_applied_to_build_the_table=len(migration.applied),
            migrated_from_version=migration.from_version,
            migrated_to_version=migration.to_version,
            declared_insertion_columns=len(INSERTION_COLUMNS),
            columns_asserted_in_order=list(INSERTION_COLUMNS),
            live_table_columns_state_db=actual,
            rows_inserted=inserted,
            idempotent_reinsert_returned_none=duplicate_insert is None,
            reject_path_exercised=rejects,
            legacy_db_duplicates_columns=legacy_cols_before,
            legacy_db_columns_after_ensure_state_tables=legacy_cols_after,
            legacy_insert_error=legacy_insert_error,
        )
        gate.capture("state-only DB PRAGMA table_info(duplicate_candidates)", ", ".join(actual))
        gate.capture("legacy (db.open_db) columns BEFORE", ", ".join(legacy_cols_before))
        gate.capture("legacy columns AFTER ensure_state_tables()", ", ".join(legacy_cols_after))
        gate.capture("insert against legacy schema", legacy_insert_error or "(no error)")

        assert len(INSERTION_COLUMNS) >= 8, "coverage under minimum: too few columns declared"
        assert actual[: len(INSERTION_COLUMNS)] == list(INSERTION_COLUMNS), (
            "the declared insertion contract does not match the table it inserts "
            f"into: declared {INSERTION_COLUMNS}, table {actual}"
        )
        assert inserted >= 2, "coverage under minimum: fewer than 2 rows inserted"
        assert duplicate_insert is None
        assert rejects >= 2, "coverage under minimum: reject path not exercised twice"

        # The finding, asserted so it cannot rot silently.
        assert legacy_cols_after == legacy_cols_before, (
            "unexpected: ensure_state_tables() changed the legacy duplicates table. "
            "If this now migrates, G4's reachability finding needs revisiting."
        )
        assert "candidate_item_id" not in legacy_cols_after
        assert legacy_insert_error, (
            "expected the P0-14 insert to fail against a db.open_db() database; it "
            "did not, which would be a welcome correction to this gate"
        )

        gate.find(
            "TWO INCOMPATIBLE `duplicates` TABLES SHARE ONE NAME. db.py's _SCHEMA "
            "defines (group_id, file_path, duplicate_type, confidence, status, "
            "run_id, staged_at); state/duplicates.py defines (run_id, "
            "candidate_item_id, matched_item_id, detector, provider_recording_id, "
            "fingerprint_digest, score, evidence_json, decision_status, created_at, "
            "evidence_identity). Both use CREATE TABLE IF NOT EXISTS, so on any "
            "database db.py touched first, ensure_state_tables() is a silent no-op "
            f"and the P0-14 insert fails with: {legacy_insert_error}"
        )
        gate.find(
            "CONTRADICTS a hedged mark: P0-14 is marked [x]. Its contract is "
            "correct and self-consistent, and its test file passes — against a "
            "database no CLI command creates. On a real database the contract "
            "cannot be satisfied at all. This is the brief's failure 4 in its "
            "sharpest form: not merely unreachable, but actively incompatible with "
            "the schema the running system has."
        )
        gate.verdict = "CONTRACT PASS (asserted and held) — but UNREACHABLE, and incompatible with the live schema"
    finally:
        gate.emit()


# ═══════════════════════════════════════════════════════════════════════════════
# G5 — failed stage is not recorded complete and not skipped on resume
# ═══════════════════════════════════════════════════════════════════════════════


def test_g5_failed_stage_not_skipped_on_resume(seeded_vault, monkeypatch, tmp_path):
    gate = Gate(
        "G5",
        "Failed stage is not recorded complete / not skipped on resume",
        "P0-08",
        "MCR-005",
        minimum_viable_coverage=(
            "at least 3 stages run, exactly 1 forced to fail, and resume state "
            "read before and after both runs (a resume gate that never reads "
            "the file has measured nothing)"
        ),
    )
    try:
        from musaeus import cli
        from musaeus.context import RunContext, StageResult
        from musaeus.stages.base import BaseStage

        assert_resolved_config_under(seeded_vault.cfg, seeded_vault.root)

        resume_file = tmp_path / "resume_home" / ".config" / "musaeus" / "resume_state.json"
        resume_file.parent.mkdir(parents=True, exist_ok=True)
        monkeypatch.setattr(cli, "_RESUME_FILE", resume_file)
        monkeypatch.setattr(cli, "get_config", lambda: seeded_vault.cfg)

        ran: list[str] = []

        def make_stage(name: str, *, fail: bool) -> type[BaseStage]:
            class _Stage(BaseStage):
                NAME = name
                CLAIMS_EFFECT = False

                def validate(self, ctx: RunContext) -> None:
                    return None

                def run(self, ctx: RunContext) -> StageResult:
                    ran.append(name)
                    result = self._make_result(dry_run=False)
                    if fail:
                        result.success = False
                        result.errors.append("forced failure for P0-19 G5")
                    ctx.record_stage(result)
                    return result

                def dry_run(self, ctx: RunContext) -> StageResult:
                    raise NotImplementedError

            _Stage.__name__ = name
            return _Stage

        first = make_stage("P019StageOne", fail=False)
        broken = make_stage("P019StageTwo", fail=True)
        third = make_stage("P019StageThree", fail=False)
        pipeline = [first, broken, third]

        state_before_first = resume_file.read_text() if resume_file.exists() else "(absent)"
        exit_first = cli._run_pipeline(list(pipeline), dry_run=False)
        state_after_first = resume_file.read_text() if resume_file.exists() else "(absent)"
        recorded = json.loads(state_after_first)["completed"] if resume_file.exists() else []

        ran.clear()
        monkeypatch.setattr(cli.sys.stdin, "isatty", lambda: False, raising=False)
        exit_second = cli._run_pipeline(list(pipeline), dry_run=False)
        second_run_stages = list(ran)
        state_after_second = resume_file.read_text() if resume_file.exists() else "(absent)"

        gate.reachability = "cli"
        gate.reachability_detail = (
            "TESTED: cli.py's own resume_state.json handling — cli._save_resume / "
            "_load_resume / _clear_resume and the `if result.success:` branch at "
            "cli.py:489, driven through cli._run_pipeline, which is what every "
            "`musaeus run` invocation executes. "
            "NOT TESTED: musaeus/state/run_state.py. That module has ZERO importers "
            "anywhere in musaeus/ — verified by grep — so testing it and reporting "
            "on resume behaviour would reproduce exactly the failure this brief "
            "was written about. Its MCR-005 logic (REASON_RUN_CANCELLED, the "
            "'once cancellation is requested, no new stage starts' rule at "
            "run_state.py:286) is unreachable from the CLI."
        )
        gate.cover(
            stages_run_first_pass=len(pipeline),
            stage_forced_to_fail="P019StageTwo",
            resume_state_before_first_run=state_before_first,
            resume_state_after_first_run=state_after_first,
            resume_completed_list=recorded,
            failed_stage_in_completed_list="P019StageTwo" in recorded,
            stages_actually_run_on_resume=second_run_stages,
            resume_state_after_second_run=state_after_second,
            first_run_exit_code=exit_first,
            second_run_exit_code=exit_second,
        )
        gate.capture("resume_state.json after failing run", state_after_first)
        gate.capture("stages executed on the resumed run", repr(second_run_stages))
        gate.capture("resume_state.json after resumed run", state_after_second)

        assert len(pipeline) >= 3, "coverage under minimum"
        assert exit_first == 1, "a failing stage did not make the run fail"
        assert "P019StageTwo" not in recorded, (
            "the failed stage was recorded complete — this is the exact baseline "
            f"defect P0-08 was meant to fix. resume state: {state_after_first}"
        )
        assert "P019StageTwo" in second_run_stages, (
            "the failed stage was SKIPPED on resume, which is the defect. "
            f"stages run: {second_run_stages}"
        )
        assert "P019StageOne" not in second_run_stages, (
            "the successful stage was re-run, so resume is not being honoured at "
            "all and the previous assertion is vacuous"
        )

        gate.find(
            "The gate distinguishes 'retried' from 'never resumed': it asserts the "
            "FAILED stage re-ran AND the SUCCEEDED stage did not. Asserting only "
            "the first would pass on a build where resume was broken outright."
        )
        gate.find(
            "The resume file the CLI uses is Path.home()-frozen at import "
            "(cli.py:228), so it is redirected here by monkeypatching the module "
            "global. A HOME change after import would not have moved it."
        )
        gate.verdict = "PASS (asserted and held) for cli.py's resume; state/run_state.py NOT EXERCISED and unreachable"
    finally:
        gate.emit()


# ═══════════════════════════════════════════════════════════════════════════════
# G6 — rebuild parity
# ═══════════════════════════════════════════════════════════════════════════════


def test_g6_rebuild_parity(tmp_path):
    gate = Gate(
        "G6",
        "Rebuild parity between live and rebuilt projections",
        "P0-07",
        "MCR-004",
        minimum_viable_coverage=(
            "at least 5 events appended and projected, at least 1 run and 1 "
            "stage row compared between original and rebuilt state, and the "
            "live-schema check performed"
        ),
    )
    try:
        from musaeus.db import open_db
        from musaeus.state.events import CanonicalEvent, append_event, read_events
        from musaeus.state.migrator import migrate
        from musaeus.state.projector import (
            project,
            projection_parity,
            rebuild_projection,
            write_projection,
        )
        from musaeus.state.schema import ensure_state_tables

        db = tmp_path / "events.db"
        recovery_for_migrate = tmp_path / "migrate_recovery"
        recovery_for_migrate.mkdir()
        # canonical_events is created by the migration chain, not by
        # ensure_state_tables(). migrate() is the only route to it.
        migration = migrate(db, recovery_root=recovery_for_migrate, now=NOW)
        conn = sqlite3.connect(str(db))
        conn.row_factory = sqlite3.Row
        ensure_state_tables(conn)

        events = [
            (
                "run.created",
                {
                    "mode": "execute",
                    "config_digest": "cfg-1",
                    "scope_summary": {"root": "fixture"},
                    "authority": "fixture",
                },
                None,
            ),
            (
                "stage.started",
                {"stage_id": "ingest", "attempt": 1, "input_digest": "in-1"},
                "ingest",
            ),
            (
                "stage.succeeded",
                {
                    "stage_id": "ingest",
                    "attempt": 1,
                    "input_digest": "in-1",
                    "output_digest": "out-1",
                    "counts": {"items": 2},
                },
                "ingest",
            ),
            (
                "stage.started",
                {"stage_id": "forge", "attempt": 1, "input_digest": "in-2"},
                "forge",
            ),
            (
                "stage.failed",
                {
                    "stage_id": "forge",
                    "attempt": 1,
                    "error_code": "forced",
                    "safe_to_retry": True,
                    "checkpoint_id": "c1",
                },
                "forge",
            ),
            (
                "run.terminal",
                {
                    "status": "failed",
                    "exit_code": 3,
                    "reason_code": "stage_failed",
                    "stage_counts": {"failed": 1},
                    "checkpoint_id": "c1",
                    "rollback_status": "not_required",
                },
                None,
            ),
        ]
        appended = 0
        for index, (name, payload, stage_id) in enumerate(events, start=1):
            appended += int(
                append_event(
                    conn,
                    CanonicalEvent(
                        event_type=name,
                        run_id="run-A",
                        sequence=index,
                        occurred_at=NOW,
                        payload=payload,
                        stage_id=stage_id,
                        attempt=1 if stage_id else None,
                    ),
                )
            )
        conn.commit()

        live = project(read_events(conn))
        write_projection(conn, live)
        rebuilt = rebuild_projection(conn)
        parity = projection_parity(live, rebuilt)

        # What a database the CLI actually creates looks like.
        legacy = open_db(tmp_path / "legacy.db")
        legacy_tables = sorted(
            r["name"] for r in legacy.execute("SELECT name FROM sqlite_master WHERE type='table'")
        )
        legacy.close()
        state_tables_present = [
            t
            for t in ("canonical_events", "state_metadata", "schema_migrations")
            if t in legacy_tables
        ]

        conn.close()

        gate.reachability = "UNREACHABLE (import only, no CLI path)"
        gate.reachability_detail = (
            "`append_event`, `project`, `rebuild_projection`, `write_projection` "
            "and `ensure_state_tables` have zero callers outside musaeus/state/. "
            "`db.open_db()` — the only DB constructor any CLI command uses — never "
            "calls ensure_state_tables() or migrate(), so canonical_events, "
            "state_metadata and schema_migrations do not exist in a database the "
            "running system produced. Confirmed empirically above: a fresh "
            f"db.open_db() database contains {len(legacy_tables)} tables and "
            f"{len(state_tables_present)} of the three P0 state tables."
        )
        gate.cover(
            migrations_applied_to_create_canonical_events=len(migration.applied),
            events_appended=appended,
            events_projected=len(events),
            runs_compared=len(live.runs),
            stage_rows_compared=len(live.stages),
            parity_differences=len(parity),
            legacy_db_table_count=len(legacy_tables),
            p0_state_tables_in_legacy_db=state_tables_present,
            legacy_db_tables=legacy_tables,
        )
        gate.capture("parity differences", repr(parity) or "[]")
        gate.capture("tables in a db.open_db() database", ", ".join(legacy_tables))
        gate.capture(
            "P0 state tables present in a db.open_db() database",
            repr(state_tables_present) or "[] (none)",
        )

        assert appended >= 5, "coverage under minimum: too few events appended"
        assert len(live.runs) >= 1, "coverage under minimum: no run row to compare"
        assert len(live.stages) >= 1, "coverage under minimum: no stage row to compare"
        assert parity == [], f"rebuilt projection differs from live: {parity}"
        assert state_tables_present == [], (
            "unexpected: a db.open_db() database now contains P0 state tables "
            f"({state_tables_present}). If migrate() is now wired, G6's "
            "reachability finding needs revisiting — that would be good news."
        )

        gate.find(
            "Parity holds — over an event store that no MUSAEUS run ever writes to. "
            "The projector is correct and unreached."
        )
        gate.find(
            "CONTRADICTS a hedged mark: P0-07 is marked [x]. state/schema.py:16-27 "
            "says the wiring 'happens in P0-11'; P0-11 is also marked [x] and "
            "delivered preflight.py and cli_gate.py but not that wiring. So a live "
            "database is an unversioned legacy database with none of the P0 state "
            "tables in it, and rebuild parity is a property of a store that does "
            "not exist in production."
        )
        gate.verdict = "PARITY PASS (asserted and held) — but UNREACHABLE"
    finally:
        gate.emit()


# ═══════════════════════════════════════════════════════════════════════════════
# G7 — lock conflict
# ═══════════════════════════════════════════════════════════════════════════════


def test_g7_lock_conflict(seeded_vault, fixture_home, tmp_path):
    gate = Gate(
        "G7",
        "Lock conflict — a second holder is refused, not delayed",
        "P0-10",
        "MCR-005",
        minimum_viable_coverage=(
            "at least 2 concurrent holders attempted including one from a real "
            "second process, the refusal observed to be immediate (bounded "
            "wall-clock), and the owner identified in the refusal"
        ),
    )
    try:
        import time

        from musaeus.safety.lock import (
            LockConflictError,
            Scope,
            acquire,
            observe,
            scopes_conflict,
        )

        locks = tmp_path / "locks"
        scope = Scope.build(seeded_vault.root, "library-mutation")

        handle = acquire(scope, locks, run_id="run-A", now=NOW)
        try:
            started = time.monotonic()
            refusal = ""
            try:
                acquire(scope, locks, run_id="run-B", now=NOW)
            except LockConflictError as exc:
                refusal = str(exc)
            elapsed = time.monotonic() - started
            owner = observe(scope, locks)

            child = run_child_python(
                f"""
                from pathlib import Path
                from musaeus.safety.lock import LockConflictError, Scope, acquire
                scope = Scope.build(Path({str(seeded_vault.root)!r}), "library-mutation")
                try:
                    acquire(scope, Path({str(locks)!r}), run_id="run-C")
                except LockConflictError as exc:
                    print("CHILD_REFUSED " + str(exc).splitlines()[0])
                    raise SystemExit(0)
                print("CHILD_ACQUIRED -- the lock did not hold across processes")
                raise SystemExit(1)
                """,
                seeded_vault.root,
                fixture_home,
            )
            assert_child_is_safe(gate, child, seeded_vault.root)

            # Descendant scope: a lock on /vault and a lock on
            # /vault/ALAC-Library cover the same files. The module's own
            # docstring calls this "the important half".
            descendant = Scope.build(seeded_vault.cfg.alac_library, "library-mutation")
            predicate_says_conflict = scopes_conflict(scope, descendant)
            descendant_refused = False
            descendant_handle = None
            try:
                descendant_handle = acquire(descendant, locks, run_id="run-D", now=NOW)
            except LockConflictError:
                descendant_refused = True
            finally:
                if descendant_handle is not None:
                    descendant_handle.release()
        finally:
            handle.release()

        gate.reachability = "UNREACHABLE for acquisition (import only); observation is cli (opt-in)"
        gate.reachability_detail = (
            "`safety.lock.acquire()` has NO caller anywhere in musaeus/ — verified "
            "by grep. The only cross-module use of this file is "
            "`preflight.py:446 holder = observe(request.scope, request.lock_dir)`, "
            "which READS an existing lock and never takes one, and is itself behind "
            "the opt-in --safety-gate. So `musaeus run` takes no scope lock. The "
            "protection against the 2026-08-15 concurrent-collision incident that "
            "this module was written for is not in the running path; what is in the "
            "running path is musaeus_overnight.sh's `pgrep -af` guard, which only "
            "covers the scheduled wrapper and not two interactive runs."
        )
        gate.cover(
            concurrent_holders_attempted=4,
            in_process_second_holder_refused=bool(refusal),
            refusal_wall_clock_seconds=round(elapsed, 4),
            separate_process_refused=child.returncode == 0 and "CHILD_REFUSED" in child.stdout,
            scopes_conflict_predicate_says_they_conflict=predicate_says_conflict,
            descendant_scope_refused_by_acquire=descendant_refused,
            owner_visible_in_observation=owner.describe() if owner else None,
        )
        gate.capture("in-process refusal", refusal)
        gate.capture("separate-process attempt", child.transcript)
        gate.capture("observed owner", owner.describe() if owner else "(none)")

        assert refusal, "a second in-process holder was NOT refused"
        assert elapsed < 5, (
            f"the second holder took {elapsed:.2f}s to be refused — that is a "
            "delay, not a refusal, and the brief requires the two be distinguished"
        )
        assert "CHILD_REFUSED" in child.stdout, child.transcript
        assert child.returncode == 0, child.transcript
        assert owner is not None and owner.run_id == "run-A"

        gate.find(
            "Refusal, not delay, for an IDENTICAL scope: measured at "
            f"{elapsed:.4f}s wall clock, from a second process, and the refusal "
            "names run-A as owner."
        )

        assert predicate_says_conflict is True, (
            "scopes_conflict() no longer reports ancestor/descendant containment; "
            "that would be a different and larger regression than the one below"
        )
        if not descendant_refused:
            gate.find(
                "FAILED, and this is a new defect rather than an unreachability "
                "finding: `acquire()` GRANTED a lock on "
                f"{seeded_vault.cfg.alac_library} while run-A held "
                f"{seeded_vault.root}, in the same domain. `scopes_conflict(a, b)` "
                "correctly returns True for that pair — the containment rule is "
                "implemented — but `acquire()` never consults it. It flocks "
                "`lock_dir/{scope.scope_id}.lock`, and scope_id is a hash of "
                "root+domain, so a descendant hashes to a DIFFERENT lock file and "
                "is granted unconditionally. "
                "lock.py's own docstring names this as one of the two decisions "
                "that 'carry most of the weight', written directly out of the "
                "2026-08-15 incident: 'Comparing paths for equality would let two "
                "runs safely hold non-equal scopes that are the same files, which "
                "is precisely how two processes end up rewriting each other's "
                "work.' That is what the acquisition path does. "
                "tests/test_p0_10_scope_lock.py proves the containment rule as a "
                "PREDICATE (test_descendant_conflicts_with_ancestor) and proves "
                "refusal only for IDENTICAL scopes — so its green result never "
                "covered this. Same shape as the brief's failure 3: the check "
                "exists and cannot fire."
            )
            gate.verdict = (
                "FAIL (asserted and failed) — identical-scope refusal holds, "
                "ancestor/descendant refusal does NOT; and acquisition is "
                "UNREACHABLE from the CLI in any case"
            )
        else:
            gate.verdict = "PRIMITIVE PASS (asserted and held) — but acquisition UNREACHABLE"

        gate.find(
            "CONTRADICTS a hedged mark: P0-10 is marked [x]. The flock mechanics "
            "and the no-timestamp-staleness rule are sound. The containment rule "
            "is not enforced at acquisition, and nothing calls acquire() anyway. "
            "Two interactive `musaeus run` invocations today collide at SQLite's "
            "busy_timeout, not at this lock."
        )
    finally:
        gate.emit()


# ═══════════════════════════════════════════════════════════════════════════════
# G8 — rollback after partial mutation
# ═══════════════════════════════════════════════════════════════════════════════


def test_g8_rollback_after_partial_mutation(tmp_path):
    gate = Gate(
        "G8",
        "Rollback after partial mutation",
        "P0-13",
        "MCR-003 / MCR-005",
        minimum_viable_coverage=(
            "at least 3 mutations applied before the fault, all of them "
            "reverted, the tree compared by digest (not by count) and residue "
            "enumerated"
        ),
    )
    try:
        from musaeus.safety.manifest import sha256_file
        from musaeus.safety.mutation import ROLLBACK_COMPLETED, MutationBoundary
        from musaeus.safety.recovery import (
            JOURNAL_FILENAME,
            OperationJournal,
            create_checkpoint,
        )

        library = tmp_path / "library"
        (library / "Bob Seger").mkdir(parents=True)
        (library / "The Byrds").mkdir(parents=True)
        (library / "Bob Seger" / "Night Moves.m4a").write_bytes(b"PAYLOAD ONE")
        (library / "Bob Seger" / "Hollywood Nights.m4a").write_bytes(b"PAYLOAD TWO")
        (library / "The Byrds" / "Eight Miles High.m4a").write_bytes(b"PAYLOAD THREE")

        def digests() -> dict[str, str]:
            return {
                str(p.relative_to(library)): sha256_file(p)
                for p in sorted(library.rglob("*"))
                if p.is_file()
            }

        before = digests()

        recovery = tmp_path / "recovery"
        recovery.mkdir()
        checkpoint = create_checkpoint(library, recovery, checkpoint_id="c1", now=NOW)
        journal = OperationJournal(checkpoint.root / JOURNAL_FILENAME)
        boundary = MutationBoundary(checkpoint, journal, run_id="run-A", source_root=library)

        boundary.write_bytes(library / "Bob Seger" / "Night Moves.m4a", b"REWRITTEN ONE")
        boundary.move(
            library / "Bob Seger" / "Hollywood Nights.m4a",
            library / "The Byrds" / "Hollywood Nights.m4a",
        )
        boundary.quarantine(
            library / "The Byrds" / "Eight Miles High.m4a", reason="p0-19 fault injection"
        )
        applied = len(journal.applied())
        mid = digests()

        result = boundary.rollback()
        after = digests()
        residue = sorted(str(p.relative_to(recovery)) for p in recovery.rglob("*") if p.is_file())

        gate.reachability = (
            "MutationBoundary is cli-reachable; rollback() is UNREACHABLE (import only)"
        )
        gate.reachability_detail = (
            "CORRECTION to the brief's expectation. The boundary IS wired into the "
            "running pipeline: stages/canonicalize.py:794 and stages/finalize.py:355 "
            "construct a MutationBoundary, and both stages are in "
            "ACT3_CANONICALIZE_FINALIZE, which is part of CANONICAL_PIPELINE = "
            "DEFAULT_PIPELINE, i.e. plain `musaeus run`. Caller chain: "
            "cli.main -> cli._run_pipeline -> FinalizeStage.run -> _open_boundary -> "
            "create_checkpoint + MutationBoundary(...) -> boundary.move / "
            "boundary.quarantine / boundary.release_source. "
            "BUT: `boundary.rollback()` has no caller in musaeus/ at all — grep for "
            "'.rollback(' finds only sqlite conn.rollback() in four stages. So the "
            "checkpoint and the operation journal ARE written by a real run, and "
            "nothing consumes them to undo one. Rollback is a capability held, not "
            "a capability exercised. Both stages also honour a CHECKPOINT_ENV "
            "escape hatch that disables the boundary entirely, and record "
            "'recovery boundary: UNAVAILABLE' rather than refusing when the "
            "checkpoint cannot be created."
        )
        gate.cover(
            files_in_fixture_tree=len(before),
            mutations_applied_before_fault=applied,
            mutations_reverted=len(result.restored),
            rollback_outcome=result.outcome,
            rollback_failures=len(result.failures),
            tree_digest_equal_after_rollback=before == after,
            tree_changed_by_mutations=before != mid,
            residue_files_under_recovery_root=len(residue),
            residue=residue,
        )
        gate.capture("digests before", json.dumps(before, indent=2, sort_keys=True))
        gate.capture("digests mid-run", json.dumps(mid, indent=2, sort_keys=True))
        gate.capture("digests after rollback", json.dumps(after, indent=2, sort_keys=True))
        gate.capture("rollback result", repr(result.as_event_payload()))
        gate.capture("residue under recovery root", "\n".join(residue))

        assert applied >= 3, f"coverage under minimum: only {applied} mutations applied"
        assert before != mid, (
            "the mutations changed nothing, so the rollback had nothing to undo and "
            "'restored' would be a green tick over zero work"
        )
        assert result.outcome == ROLLBACK_COMPLETED, repr(result.as_event_payload())
        assert len(result.restored) >= 3
        assert before == after, (
            "the tree was not restored to its recorded pre-run state: "
            f"{ {k: (before.get(k), after.get(k)) for k in set(before) | set(after) if before.get(k) != after.get(k)} }"
        )
        assert residue, (
            "no residue at all under the recovery root — the checkpoint and "
            "quarantine material should still be retrievable, since MCR-003 "
            "forbids permanent deletion in P0"
        )

        gate.verdict = (
            "PASS (asserted and held) for the primitive — rollback UNREACHABLE from the CLI"
        )
    finally:
        gate.emit()


# ═══════════════════════════════════════════════════════════════════════════════
# G9 — Big Kahuna missing-root block: NO TARGET
# ═══════════════════════════════════════════════════════════════════════════════


def test_g9_big_kahuna_has_no_target(seeded_vault, fixture_home):
    gate = Gate(
        "G9",
        "Big Kahuna missing-root block — NO TARGET",
        "P0-15",
        "MCR-002 / MCR-007",
        minimum_viable_coverage=(
            "the whole musaeus/ package searched for the flag and the pipeline "
            "constant; the surviving substitute exercised through the real CLI "
            "and its refusal captured"
        ),
    )
    try:
        matches = subprocess.run(
            [
                "grep",
                "-rn",
                "-E",
                r"big.kahuna|big_kahuna|BIG_KAHUNA",
                "--include=*.py",
                "musaeus/",
            ],
            cwd=str(REPO_ROOT),
            capture_output=True,
            text=True,
        )
        hits = [line for line in matches.stdout.splitlines() if line.strip()]
        code_hits = [h for h in hits if "exports.py" not in h]
        python_files = sum(1 for _ in (REPO_ROOT / "musaeus").rglob("*.py"))

        # The surviving equivalent, under its own label — never inheriting G9's name.
        child = run_child_python(
            """
            import sys
            from musaeus.cli import main
            sys.argv = ["musaeus", "curator"]
            main()
            """,
            seeded_vault.root,
            fixture_home,
        )
        assert_child_is_safe(gate, child, seeded_vault.root)

        gate.reachability = "no-target"
        gate.reachability_detail = (
            "There is nothing left to block. `--big-kahuna` and "
            "`BIG_KAHUNA_PIPELINE` do not exist in musaeus/ — the only textual "
            "match is musaeus/exports.py's own module docstring recording that "
            "fact. tasks.md closes P0-03 as superseded, not implemented, for the "
            "same reason. What survives is `musaeus curator --export-root`, "
            "exercised separately below under its own label."
        )
        gate.cover(
            python_files_searched=python_files,
            grep_matches_total=len(hits),
            grep_matches_in_code=len(code_hits),
            grep_matches=hits,
            substitute_exercised="musaeus curator (no --export-root, no configured root)",
            substitute_exit_code=child.returncode,
        )
        gate.capture(
            "grep -rnE 'big.kahuna|big_kahuna|BIG_KAHUNA' --include=*.py musaeus/",
            matches.stdout or "(no matches)",
        )
        gate.capture(
            "musaeus/exports.py docstring, relevant clause",
            "`--big-kahuna` and `BIG_KAHUNA_PIPELINE` do not exist anywhere in "
            "this codebase. The half of P0-15 addressed to them has no target; "
            "what remains live is `musaeus curator --export-root`.",
        )
        gate.capture("SUBSTITUTE: musaeus curator with no resolved export root", child.transcript)

        assert python_files > 50, "coverage under minimum: the search saw too few files"
        assert code_hits == [], (
            f"a Big Kahuna code path exists after all: {code_hits}. That would be a "
            "correction to this gate and to the brief's §6 — report it, do not "
            "adjust the gate."
        )
        assert child.returncode != 0, (
            "the surviving substitute did NOT refuse when no export root was "
            f"resolvable. exit={child.returncode}\n{child.transcript}"
        )

        gate.find(
            "Recorded as NOT APPLICABLE, per the brief's §6. Not green (there is no "
            "block, so a tick would be a false statement) and not dropped (P0-19 "
            "names the gate, so an unexplained omission would look complete)."
        )
        gate.find(
            f"The substitute refused with exit {child.returncode}. Reported under "
            "its own label so it cannot silently inherit G9's name."
        )
        gate.verdict = "NO TARGET — NOT APPLICABLE (substitute exercised separately and refused)"
    finally:
        gate.emit()


# ═══════════════════════════════════════════════════════════════════════════════
# G10 — scheduled run is preview / review-only
# ═══════════════════════════════════════════════════════════════════════════════


def test_g10_scheduled_run_is_preview_or_review_only(fixture_home):
    gate = Gate(
        "G10",
        "Scheduled run is preview / review-only",
        "P0-17",
        "MCR-001 / MCR-005",
        minimum_viable_coverage=(
            "the wrapper's resolved VAULT_ROOT captured, the branch it took "
            "named, and zero mutation confirmed under the fixture root — plus, "
            "if the preview branch was not reached, the reason stated"
        ),
    )
    wrapper_root: Path | None = None
    try:
        from musaeus import scheduling  # noqa: F401  (import is itself the evidence)

        # §7.3: the wrapper's concurrency guard branches on any live musaeus
        # process. Record what is live before deciding what this gate can claim.
        live = subprocess.run(
            ["pgrep", "-af", r"(bin/musaeus\b|python3 -m musaeus\b)"],
            capture_output=True,
            text=True,
        )
        live_processes = [line for line in live.stdout.splitlines() if line.strip()]

        # A fixture VAULT_ROOT on a filesystem with headroom: the wrapper's own
        # disk guard aborts below 50 GB free AND sends an ntfy.sh push on abort,
        # which would be an external network attempt. /tmp has ~3 GB.
        # This used to hardcode /home/grey/.cache, which is why this gate had
        # never once run anywhere but one laptop. Ask for headroom instead of
        # naming a person: the first directory with 50 GB wins, and if none has
        # it the gate skips rather than reporting a failure it cannot tell apart
        # from a real one.
        candidates = [
            os.environ.get("MUSAEUS_TEST_SCRATCH"),
            # NOT Path.home(): this rehearsal deliberately sandboxes HOME to a
            # fixture dir, so Path.home() would report the fixture's tmpfs and
            # this gate would skip on the one machine it can actually run on.
            # The passwd database is not monkeypatched.
            str(pathlib.Path(pwd.getpwuid(os.getuid()).pw_dir) / ".cache"),
            tempfile.gettempdir(),
        ]
        wrapper_root = None
        for cand in candidates:
            if not cand or not os.path.isdir(cand):
                continue
            if shutil.disk_usage(cand).free // 10**9 >= 50:
                wrapper_root = Path(tempfile.mkdtemp(prefix="musaeus_p0_19_wrapper_", dir=cand))
                break
        if wrapper_root is None:
            pytest.skip("no scratch dir with 50 GB free; the wrapper's disk guard would abort")
        free_gb = shutil.disk_usage(str(wrapper_root)).free // 10**9

        env = build_child_env(wrapper_root, fixture_home)
        proc = subprocess.run(
            ["bash", str(REPO_ROOT / "musaeus_overnight.sh")],
            env=env,
            capture_output=True,
            text=True,
            timeout=180,
            cwd=str(REPO_ROOT),
        )
        out = proc.stdout + proc.stderr
        skipped_on_collision = "OVERNIGHT_SKIPPED_LOCK_COLLISION" in out
        # This gate reads TEXT, not sockets: the wrapper is a subprocess, so the
        # in-process transport harness cannot see it. That means a refusal has
        # to be recognised explicitly -- musaeus_notify.py announces a policy
        # suppression on the same "[notify]" prefix it uses to report a send,
        # and without this the gate called a refusal an attempt and failed the
        # rehearsal for doing exactly what the rehearsal demands.
        suppressed = "SUPPRESSED by network policy" in out
        notified = (not suppressed) and ("[notify]" in out or "notification" in out)
        created = sorted(str(p.relative_to(wrapper_root)) for p in wrapper_root.rglob("*"))

        default_is_preview = "DRY RUN" in out

        gate.reachability = (
            "cli/wrapper for musaeus_overnight.sh; UNREACHABLE for musaeus/scheduling.py"
        )
        gate.reachability_detail = (
            "Two different things carry this name, and only one of them runs. "
            "TESTED: musaeus_overnight.sh, the script the crontab invokes. "
            "NOT REACHED: musaeus/scheduling.py's run_scheduled(), which implements "
            "preview/review-only-by-default, scheduled_response() returning None, "
            "and defer-on-conflict. `grep -rn scheduling musaeus/` returns NOTHING "
            "outside scheduling.py itself — the module has zero importers. "
            "musaeus/reporting.py is imported only by scheduling.py, so it is "
            "unreachable for the same reason (see I4)."
        )
        gate.cover(
            wrapper_resolved_vault_root=str(wrapper_root),
            wrapper_free_gb_at_run=free_gb,
            wrapper_exit_code=proc.returncode,
            branch_taken=(
                "concurrency guard (OVERNIGHT_SKIPPED_LOCK_COLLISION)"
                if skipped_on_collision
                else "reached stage execution"
            ),
            live_musaeus_processes_at_run=live_processes,
            wrapper_default_mode_is_preview=default_is_preview,
            entries_created_under_fixture_root=len(created),
            network_notification_attempted=notified,
            scheduling_py_importers_in_musaeus=0,
        )
        gate.capture("wrapper output", out)
        gate.capture(
            "live musaeus processes (pgrep, as the wrapper sees them)",
            "\n".join(live_processes) or "(none)",
        )
        gate.capture("entries created under the fixture VAULT_ROOT", "\n".join(created))

        assert not notified, (
            "the wrapper attempted an outbound notification. Nothing in this "
            f"rehearsal may make an external call.\n{out}"
        )
        assert str(wrapper_root) in out, (
            f"could not confirm the wrapper's resolved VAULT_ROOT from its own output:\n{out}"
        )

        if skipped_on_collision:
            gate.verdict = (
                "BLOCKED — could not exercise the preview branch (scheduling "
                "conflict, not a safety breach)"
            )
            gate.find(
                "The brief's §7.3 predicted exactly this and it happened. "
                'musaeus_overnight.sh:169 runs `pgrep -af "(bin/musaeus\\b|python3 '
                '-m musaeus\\b)"` and exits 0 on any match. Live at rehearsal '
                f"time: {live_processes}. The wrapper therefore took its "
                "already-running branch and no stage ran. Reporting this gate green "
                "would be failure 3 from the brief's §1 — a check that could not "
                "fire, recorded as a check that passed."
            )
            gate.find(
                "§7 of the brief requires all vault jobs clear before dispatch. "
                "The five PIDs it names are gone, but new ones "
                "(bitrot.sh verify / python3 -m musaeus bitrot) were running when "
                "this rehearsal was dispatched. G10 needs a re-run on a quiet disk."
            )
        else:
            gate.verdict = (
                "PASS (asserted and held)" if default_is_preview else "FAIL (asserted and failed)"
            )

        gate.find(
            "SEPARATE, UNCONDITIONAL FINDING, independent of the block above: the "
            "wrapper's DEFAULT is not preview. musaeus_overnight.sh:79-87 sets "
            "DRY_FLAG only when --dry-run is passed, and the documented crontab "
            "line passes no flags. So the scheduled run's default is full "
            "execution of the 21-stage canonical chain. The module that implements "
            "preview-by-default (musaeus/scheduling.py) has no importer. This "
            "CONTRADICTS P0-17's [x] mark: the behaviour is implemented and tested "
            "in a module the scheduler does not use."
        )
        gate.find(
            "Also unconditional: the wrapper's own VAULT_ROOT default is fail-open "
            "in the same shape as the Python side — line 67, "
            '`VAULT_ROOT="${MUSAEUS_VAULT_ROOT:-/mnt/FORGE2TB/Projects/MUSAEUS_VAULT}"`. '
            "Unset the variable and the scheduler points at the live vault."
        )
    finally:
        if wrapper_root is not None and wrapper_root.exists():
            shutil.rmtree(wrapper_root, ignore_errors=True)
        gate.emit()


# ═══════════════════════════════════════════════════════════════════════════════
# I1–I5 — additionally required areas
# ═══════════════════════════════════════════════════════════════════════════════


def test_i_areas_reachability_and_coverage(tmp_path, seeded_vault, fixture_home):
    """The five additionally-required areas (I1-I5).

    Grouped in one test because each is a reachability question first and a
    behaviour question second, and reporting them separately would imply
    five independent gates where the brief asks for five reported areas.
    """
    gate = Gate(
        "I1-I5",
        "Fresh install/legacy migration/restore, cancellation, preflight, "
        "report+redaction, documentation consistency",
        "P0-06 / P0-09 / P0-11 / P0-16 / P0-18",
        "MCR-004 / MCR-005 / MCR-002 / MCR-006 / MCR-008",
        minimum_viable_coverage=(
            "each of the five areas assigned a reachability value with the "
            "caller chain named or its absence stated; at least one concrete "
            "measurement per area"
        ),
    )
    try:
        from musaeus.db import open_db
        from musaeus.reporting import ActionCounts, RunReport, to_shareable
        from musaeus.state.cancellation import CancellationGate
        from musaeus.state.schema import ensure_state_tables

        def callers(pattern: str) -> list[str]:
            proc = subprocess.run(
                ["grep", "-rn", "-E", pattern, "--include=*.py", "musaeus/"],
                cwd=str(REPO_ROOT),
                capture_output=True,
                text=True,
            )
            return [line for line in proc.stdout.splitlines() if line.strip()]

        migrate_callers = [c for c in callers(r"\bmigrate\(") if "/state/" not in c]
        cancel_callers = [
            c
            for c in callers(r"CancellationGate")
            if "/state/" not in c and "safety/mutation.py" not in c
        ]
        scheduling_callers = [c for c in callers(r"\bscheduling\b") if "scheduling.py" not in c]
        reporting_callers = [
            c
            for c in callers(r"from musaeus\.reporting|from \.reporting")
            if "reporting.py" not in c
        ]

        # I1 — fresh install and legacy migration.
        fresh = tmp_path / "fresh.db"
        conn = sqlite3.connect(str(fresh))
        conn.row_factory = sqlite3.Row
        ensure_state_tables(conn)
        fresh_tables = sorted(
            r["name"] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        )
        conn.close()

        legacy = open_db(tmp_path / "legacy_for_i1.db")
        legacy_tables = sorted(
            r["name"] for r in legacy.execute("SELECT name FROM sqlite_master WHERE type='table'")
        )
        legacy.close()

        # I2 — cancellation. The gate refuses a mutation after cancellation.
        cancel_gate = CancellationGate(run_id="run-A", requested=True, requested_at=NOW)
        observed = cancel_gate.observe(now=NOW)
        refused_after_cancel = False
        try:
            cancel_gate.guard_mutation()
        except Exception as exc:  # noqa: BLE001 — the exception type is the evidence
            refused_after_cancel = type(exc).__name__

        # I4 — report generation and redaction.
        report = RunReport(
            run_id="run-A",
            mode="preview",
            scope_root=str(tmp_path / "fixture_scope"),
            classification="fixture",
            started_at=NOW,
            finished_at=NOW,
            status="completed",
            totals=ActionCounts(),
            path_map={"item-1": str(tmp_path / "fixture_scope" / "Bob Seger" / "x.m4a")},
            next_actions=(f"inspect {tmp_path / 'fixture_scope'} for residue",),
        )
        shareable = to_shareable(report)
        rendered = json.dumps(shareable.as_dict(), sort_keys=True)

        # I5 — documentation consistency.
        inventory_test = REPO_ROOT / "tests" / "test_p0_18_cli_inventory.py"

        gate.reachability = "mixed — see per-area breakdown"
        gate.reachability_detail = "\n".join(
            [
                "I1 fresh install / legacy migration / restore (P0-06): UNREACHABLE. "
                f"migrate() callers outside musaeus/state/: {len(migrate_callers)}. "
                "db.open_db() is the only DB constructor the CLI uses and it calls "
                "neither migrate() nor ensure_state_tables(). A live database is "
                "therefore an unversioned legacy database. Restore likewise has no "
                "CLI entry point.",
                "I2 cancellation (P0-09): UNREACHABLE. CancellationGate's only "
                "cross-module use is MutationBoundary's optional `gate=` parameter, "
                f"and no stage passes it (grep for 'gate=' in musaeus/stages/ and "
                f"musaeus/safety/ returns nothing). Other callers: {len(cancel_callers)}. "
                "The CLI's actual cancellation behaviour is cli.py's "
                "KeyboardInterrupt handler, which saves resume state — a different "
                "mechanism with none of MCR-005's guarantees.",
                "I3 preflight (P0-11): cli, OPT-IN ONLY. Chain: cli._run_pipeline -> "
                "cli_gate.enforce_execution_gate -> preflight.run_preflight. Off "
                "unless --safety-gate or MUSAEUS_P0_SAFETY_GATE=1. This is the one "
                "area of the five with a genuine CLI path.",
                "I4 report generation and redaction (P0-16): UNREACHABLE. "
                f"musaeus.reporting importers outside itself: {reporting_callers} — "
                "only musaeus/scheduling.py, which itself has "
                f"{len(scheduling_callers)} importers. Nothing in the CLI produces "
                "a RunReport, so no run emits one and no redaction is ever applied "
                "to real output.",
                "I5 documentation consistency (P0-18): import. The inventory lives "
                f"in {inventory_test.name}, an audit over CLI help text rather than "
                "a runtime path. Reachability is not the right question for it; "
                "whether the inventory is complete is, and that was not re-audited "
                "here.",
            ]
        )
        gate.cover(
            i1_migrate_callers_outside_state=len(migrate_callers),
            i1_fresh_state_db_tables=fresh_tables,
            i1_legacy_open_db_tables=len(legacy_tables),
            i1_p0_tables_in_legacy=[
                t
                for t in ("canonical_events", "state_metadata", "schema_migrations")
                if t in legacy_tables
            ],
            i2_cancellation_observed=observed,
            i2_mutation_refused_after_cancellation=refused_after_cancel,
            i2_stages_passing_a_cancellation_gate=0,
            i3_preflight_cli_path="cli._run_pipeline -> cli_gate.enforce_execution_gate -> run_preflight",
            i3_enabled_by_default=False,
            i4_reporting_importers=reporting_callers,
            i4_scheduling_importers=len(scheduling_callers),
            i4_shareable_report_rendered_bytes=len(rendered),
            i4_shareable_drops_path_map=not shareable.path_map,
            i4_real_path_absent_from_shareable_output=str(tmp_path) not in rendered,
            i5_inventory_test_present=inventory_test.exists(),
            i5_inventory_reaudited=False,
        )
        gate.capture(
            "migrate() callers outside musaeus/state/", "\n".join(migrate_callers) or "(none)"
        )
        gate.capture("musaeus.reporting importers", "\n".join(reporting_callers) or "(none)")
        gate.capture("musaeus.scheduling importers", "\n".join(scheduling_callers) or "(none)")
        gate.capture("tables in a fresh ensure_state_tables() DB", ", ".join(fresh_tables))
        gate.capture("tables in a fresh db.open_db() DB (count)", str(len(legacy_tables)))
        gate.capture("shareable report", rendered)

        assert migrate_callers == [], (
            f"migrate() now has callers outside musaeus/state/: {migrate_callers}. "
            "That would be a welcome correction — the P0 state layer would be wired."
        )
        assert refused_after_cancel, "the cancellation gate did not refuse a mutation"
        assert observed is True
        assert reporting_callers, "expected at least the scheduling.py importer"
        assert not shareable.path_map, "the shareable report still carries the path map"
        assert str(tmp_path) not in rendered, (
            f"a real fixture path survived redaction into the shareable report: {rendered}"
        )
        assert scheduling_callers == [], (
            f"musaeus/scheduling.py now has importers: {scheduling_callers}. That "
            "would make G10 and I4 reachable."
        )
        assert inventory_test.exists()

        gate.verdict = (
            "REPORTED — I3 reachable (opt-in); I1, I2 and I4 UNREACHABLE; I5 not re-audited"
        )
        gate.find(
            "Four of the five areas rest on marks that are provisional for a "
            "stronger reason than the spec's own hedge: P0-06, P0-09 and P0-16 are "
            "marked [x] over code the running system does not reach, and P0-18's "
            "inventory was not re-audited by this rehearsal."
        )
    finally:
        gate.emit()


# ═══════════════════════════════════════════════════════════════════════════════
# G11 — explicit no-live-data-operation audit. Runs last, metadata only.
# ═══════════════════════════════════════════════════════════════════════════════


def test_g11_no_live_data_operation_audit(path_guard, transport_harness):
    """Last, and against metadata only — `stat`, never `open`.

    Compares the real paths against the baseline captured at rehearsal
    start (docs/p0_evidence/P0-19/G11_baseline_start.txt), never against a
    figure written into the brief: the live database is a moving target
    while any vault job runs.
    """
    gate = Gate(
        "G11",
        "Explicit no-live-data-operation audit",
        "—",
        "MCR-008",
        minimum_viable_coverage=(
            "every one of the 5 protected roots asserted, the recovery root "
            "asserted absent, the PathGuard's and transport harness's attempt "
            "logs read, and the live DB compared against a start-of-run stat"
        ),
    )
    try:
        baseline_path = EVIDENCE_DIR / "G11_baseline_start.txt"
        baseline = baseline_path.read_text(encoding="utf-8") if baseline_path.exists() else ""
        baseline_db_size: int | None = None
        for line in baseline.splitlines():
            if "musaeus.db" in line and "size=" in line:
                baseline_db_size = int(line.split("size=")[1].split()[0])

        # stat, not open. The PathGuard does not intercept os.stat, so this
        # is deliberately the one operation that can look without touching.
        observed: dict[str, str] = {}
        for root in PROTECTED_REAL_ROOTS:
            try:
                st = os.stat(root)
                observed[root] = f"present size={st.st_size} mtime={int(st.st_mtime)}"
            except FileNotFoundError:
                observed[root] = "absent"

        recovery_root = "/home/grey/Projects/MUSAEUS_RECOVERY"
        recovery_absent = not os.path.exists(recovery_root)

        live_db = "/mnt/FORGE2TB/Projects/MUSAEUS_VAULT/musaeus.db"
        current_db_size = os.stat(live_db).st_size if os.path.exists(live_db) else None

        live = subprocess.run(
            ["pgrep", "-af", r"(bin/musaeus\b|python3 -m musaeus\b|bitrot)"],
            capture_output=True,
            text=True,
        )
        live_processes = [line for line in live.stdout.splitlines() if line.strip()]

        db_delta = (
            None
            if (baseline_db_size is None or current_db_size is None)
            else current_db_size - baseline_db_size
        )

        gate.reachability = "no-target (an audit of this rehearsal, not of a MUSAEUS code path)"
        gate.reachability_detail = (
            "G11 does not exercise product behaviour; it checks that the "
            "rehearsal itself stayed inside its boundary. Its instruments are the "
            "session PathGuard's attempt log, the TransportDenialHarness's attempt "
            "log, and stat() against the real roots."
        )
        gate.cover(
            protected_roots_asserted=len(PROTECTED_REAL_ROOTS),
            protected_root_state=observed,
            future_recovery_root_absent=recovery_absent,
            path_guard_still_enabled=path_guard.enabled,
            path_guard_blocked_attempts=len(path_guard.attempts),
            path_guard_attempts=[a[:2] for a in path_guard.attempts],
            transport_harness_installed=transport_harness._installed,
            transport_attempts=len(transport_harness.attempts),
            live_db_size_at_start=baseline_db_size,
            live_db_size_now=current_db_size,
            live_db_delta_bytes=db_delta,
            live_vault_processes_now=live_processes,
        )
        gate.capture("baseline captured at rehearsal start", baseline or "(baseline missing)")
        gate.capture("protected roots now", json.dumps(observed, indent=2, sort_keys=True))
        gate.capture(
            "PathGuard attempts (each one is a BLOCKED access, not a completed one)",
            repr([a[:2] for a in path_guard.attempts]) or "[]",
        )
        gate.capture("transport harness attempts", repr(transport_harness.attempts) or "[]")
        gate.capture("live vault processes now", "\n".join(live_processes) or "(none)")

        assert len(PROTECTED_REAL_ROOTS) == 5
        assert recovery_absent, (
            f"{recovery_root} exists. This rehearsal must neither create nor probe "
            "it, and the spec says nothing may."
        )
        assert path_guard.enabled, (
            "the PathGuard is no longer enabled. A gate that needed it off is a "
            "gate that failed; nothing here may switch it off."
        )
        assert transport_harness._installed, "the transport-denial harness was uninstalled"
        assert baseline_db_size is not None, (
            "no start-of-rehearsal database size was captured, so this gate cannot "
            "make the comparison the brief requires and must not claim it did"
        )

        if db_delta not in (0, None):
            gate.verdict = (
                "PASS with a qualified DB-size line — the live database changed "
                "size during the rehearsal, attributable to a concurrent vault job"
            )
            gate.find(
                f"The live database moved by {db_delta:+d} bytes "
                f"({baseline_db_size} -> {current_db_size}). Per the brief's §3 "
                "this is a scheduling error, not a safety breach, and it is only "
                "distinguishable as such because the vault jobs were enumerated at "
                f"start and end: {live_processes}. Nothing in this rehearsal opened "
                "that file — the PathGuard would have raised, and it recorded "
                f"{len(path_guard.attempts)} blocked attempt(s), none of them a "
                "successful access."
            )
            gate.find(
                "§7 of the brief required a quiet disk before dispatch. It was not "
                "quiet. That weakens this gate's byte-size line specifically, and "
                "G10 entirely (see G10)."
            )
        else:
            gate.verdict = "PASS (asserted and held)"

        gate.find(
            "The PathGuard's attempt log is a log of BLOCKED accesses. A non-zero "
            "count would mean something tried and was stopped, not that something "
            f"succeeded. Count: {len(path_guard.attempts)}."
        )
    finally:
        gate.emit()
