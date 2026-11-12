"""The Debian package refuses to build when the code names this workstation
(packaging/build-deb.sh greps for it). Checked here, so a change that breaks the
package fails the suite instead of the next release build (2026-11-02: a comment
naming /home/grey/... in musaeus/state/policy.py stopped the build)."""

import re
from pathlib import Path

# The same pattern packaging/build-deb.sh refuses.
WORKSTATION = re.compile(r"/(home|media)/grey|/mnt/(FORGE2TB|NUC8TB_BACKUP)")
ROOT = Path(__file__).resolve().parents[1]
CODE = ROOT / "musaeus"
# The helper scripts the build puts beside the code, read from its own list
# (CodeRabbit, review of #281).
HELPERS = (
    re.search(
        r"for helper in ([^;]+); do",
        (ROOT / "packaging" / "build-deb.sh").read_text(encoding="utf-8"),
    )
    .group(1)
    .split()
)


def _packaged() -> list[Path]:
    # Excluded by where they sit in the package, not in this checkout: a
    # checkout inside a folder named "tests" scanned nothing (review of #281).
    code = [
        p
        for p in sorted(CODE.rglob("*"))
        if p.is_file() and not {"tests", "__pycache__"} & set(p.relative_to(CODE).parts)
    ]
    return code + [ROOT / "scripts" / f"{h}.py" for h in HELPERS]


def test_the_packaged_code_names_no_workstation_path():
    hits = [
        f"{path.relative_to(ROOT)}:{n}"
        for path in _packaged()
        for n, line in enumerate(path.read_text(encoding="utf-8", errors="replace").splitlines(), 1)
        if WORKSTATION.search(line)
    ]
    assert hits == []


def test_the_scan_covers_the_code_and_the_helper_scripts():
    names = {p.relative_to(ROOT).as_posix() for p in _packaged()}
    assert "musaeus/state/policy.py" in names and "scripts/musaeus_notify.py" in names


def test_the_version_has_one_value():
    """pyproject.toml names the Debian package; musaeus/__init__.py answers
    `--version`. Two homes for one number: they must agree."""
    from musaeus import __version__

    declared = re.search(
        r'^version = "([^"]+)"', (ROOT / "pyproject.toml").read_text(encoding="utf-8"), re.M
    )
    assert declared and declared.group(1) == __version__
