"""The iPhone transfer guide prints steps; it never touches the phone.

Grey, 2026-09-17: "can musaeus walk the enduser through it? even if that's
just a note saying cut'n'paste this into terminal."

Three steps genuinely cannot be automated: `idevicepair pair` shows a Trust
dialog that a person has to tap on an unlocked handset, `ifuse` needs the app
already installed, and clearing someone's phone is not a thing a script does
because it felt like it.

What CAN be done from here is checking every reason the paste would fail --
a missing tool, an unplugged phone, a lapsed pairing -- which is most of the
value. That is what this tests, plus the one property that matters more than
any of it: the tool is read-only.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "iphone_transfer.py"
SRC = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="module")
def vault(tmp_path_factory):
    """A throwaway vault with one track, so the tests never read the real one."""
    root = tmp_path_factory.mktemp("vault")
    for d in ("INBOX", "STAGING", "Q", "RUNS", "MetaData"):
        (root / d).mkdir()
    lib = root / "Libraries" / "iPHONE_Library" / "Artist" / "Album"
    lib.mkdir(parents=True)
    (lib / "t.m4a").write_bytes(b"\0" * 4096)
    (root / "Libraries" / "ALAC_Library").mkdir()
    return root


def run(*args, vault=None, env=None):
    import os
    e = dict(os.environ)
    e["MUSAEUS_NO_IDLE_THROTTLE"] = "1"
    if vault is not None:
        e["MUSAEUS_VAULT_ROOT"] = str(vault)
    if env:
        e.update(env)
    return subprocess.run([sys.executable, str(SCRIPT), *args],
                          capture_output=True, text=True, timeout=120, cwd=str(SRC), env=e)


class TestItIsReadOnly:
    def test_the_source_contains_no_destructive_call(self):
        """The whole contract. It prints an rm for the operator to run
        deliberately; it must never run one itself."""
        src = SCRIPT.read_text()
        for bad in ("shutil.rmtree", "os.remove", "Path.unlink", ".unlink()",
                    'subprocess.run(["rm'):
            assert bad not in src, f"the guide must not {bad}"

    def test_it_never_invokes_ifuse_or_rsync(self):
        """Mounting and copying are the operator's to run. The only commands
        this may execute are the read-only probes."""
        src = SCRIPT.read_text()
        body = src[src.index("def main("):]
        for cmd in ('"ifuse"', '"rsync"', '"fusermount"'):
            assert f'_run([{cmd}' not in body, f"must not execute {cmd}"

    def test_the_only_commands_it_runs_are_probes(self):
        """String scan, not a regex. A pattern containing an escaped bracket
        trips tests/test_semgrep_actually_scans_what_it_claims.py's
        bracket-regex guard, which cannot tell a bracket ALPHABET from a
        bracket that happens to appear in source code -- and the guard is
        right to be blunt about it."""
        src = SCRIPT.read_text()
        calls = set()
        needle = '_run(['
        i = src.find(needle)
        while i != -1:
            rest = src[i + len(needle):]
            if rest.startswith('"'):
                calls.add(rest[1:rest.index('"', 1)])
            i = src.find(needle, i + 1)
        assert calls <= {"idevice_id", "idevicepair"}, f"unexpected: {calls}"


class TestItPrintsUsableSteps:
    def test_every_step_appears(self, vault):
        out = run(vault=vault).stdout
        for step in ("idevicepair pair", "ifuse --documents", "rsync -a", "fusermount -u"):
            assert step in out, f"missing step: {step}"

    def test_the_paste_parses_as_bash(self, tmp_path, vault):
        """A guide whose commands do not parse is worse than no guide -- the
        console menu already shipped one hint that was a bash syntax error."""
        out = run(vault=vault).stdout
        block = [ln.strip() for ln in out.splitlines()
                 if ln.strip().startswith(("idevicepair", "mkdir", "ifuse", "rsync", "fusermount", "rm -rf"))]
        assert block, "no commands found to check"
        script = tmp_path / "paste.sh"
        script.write_text("\n".join(block) + "\n")
        r = subprocess.run(["bash", "-n", str(script)], capture_output=True, text=True)
        assert r.returncode == 0, f"the paste does not parse: {r.stderr}"

    def test_the_mount_point_is_in_home_not_mnt(self, vault):
        """/mnt needs root. The first version of these instructions used it
        and failed with 'Permission denied' on the operator's first try."""
        out = run(vault=vault).stdout
        assert "/mnt/iphone" not in out
        assert str(Path.home()) in out

    def test_wipe_is_off_by_default(self, vault):
        assert "rm -rf" not in run(vault=vault).stdout

    def test_wipe_adds_the_clear_line_when_asked(self, vault):
        out = run("--wipe", vault=vault).stdout
        assert "rm -rf" in out
        assert out.index("ifuse --documents") < out.index("rm -rf") < out.index("rsync -a"), \
            "the clear must happen after the mount and before the copy"


class TestItExplainsTheTwoSurprises:
    def test_it_warns_the_music_app_cannot_see_the_files(self, vault):
        """iOS sandboxing. This is the single most common surprise, and Grey
        hit it on his first attempt -- the files arrived and the Music app
        could not find them."""
        out = run(vault=vault).stdout.lower()
        assert "music app" in out and "sandbox" in out

    def test_it_warns_the_app_must_be_installed_first(self, vault):
        out = run(vault=vault).stdout
        assert "must already be installed" in out
        assert "No such file or directory" in out, "the confusing error should be named"


class TestTheChecks:
    def test_it_reports_on_each_required_tool(self, vault):
        out = run(vault=vault).stdout
        for tool in ("idevicepair", "ifuse", "rsync"):
            assert tool in out

    def test_a_missing_edition_is_explained_not_crashed(self, tmp_path, vault):
        r = run(vault=vault, env={"MUSAEUS_IPHONE_LIBRARY": str(tmp_path / "nope")})
        assert r.returncode == 1
        assert "does not exist yet" in r.stdout
        assert "build_car_library.py" in r.stdout, "it should say how to make one"
