"""The running-check must not match the shell that is asking the question.

`pgrep -f "python3 -m musaeus"` matches its own wrapper process and has twice
reported a running pipeline when nothing was running.
"""
import subprocess
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "musaeus_running.sh"


def test_script_exists_and_is_executable():
    assert SCRIPT.is_file()
    assert SCRIPT.stat().st_mode & 0o111


def test_does_not_match_the_shell_that_is_asking(tmp_path):
    """The wrapper's own command line contains the pattern verbatim.

    Asserts the script never reports the ASKING shell's pid -- not that the
    output is empty. An earlier version asserted emptiness and went red
    whenever a real pipeline happened to be running alongside the suite,
    which is a true answer, not a failure.
    """
    pidfile = tmp_path / "asker.pid"
    r = subprocess.run(
        ["bash", "-c",
         f'echo $$ > {pidfile}; echo "python3 -m musaeus run" >/dev/null; {SCRIPT}'],
        capture_output=True, text=True,
    )
    asker = pidfile.read_text().strip()
    reported = set(r.stdout.split())
    assert asker not in reported, (
        f"reported the asking shell itself (pid {asker}); "
        f"this is the pgrep -f self-match trap"
    )


def test_naive_pgrep_would_have_false_positived():
    # Guard the guard: prove the trap is real, so this test keeps its meaning.
    r = subprocess.run(
        ["bash", "-c", 'pgrep -f "python3 -m musaeus" >/dev/null && echo MATCHED'],
        capture_output=True, text=True,
    )
    assert "MATCHED" in r.stdout, "trap no longer reproduces; revisit this guard"
