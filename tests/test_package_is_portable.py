"""The Debian package refuses to build when the code names this workstation
(packaging/build-deb.sh greps for it). Checked here, so a change that breaks the
package fails the suite instead of the next release build (2026-11-02: a comment
naming /home/grey/... in musaeus/state/policy.py stopped the build)."""

import re
from pathlib import Path

# The same pattern packaging/build-deb.sh refuses.
WORKSTATION = re.compile(r"/(home|media)/grey|/mnt/(FORGE2TB|NUC8TB_BACKUP)")
CODE = Path(__file__).resolve().parents[1] / "musaeus"


def test_the_packaged_code_names_no_workstation_path():
    hits = [
        f"{path.relative_to(CODE.parent)}:{n}"
        for path in sorted(CODE.rglob("*"))
        if path.is_file() and "tests" not in path.parts and "__pycache__" not in path.parts
        for n, line in enumerate(path.read_text(encoding="utf-8", errors="replace").splitlines(), 1)
        if WORKSTATION.search(line)
    ]
    assert hits == []
