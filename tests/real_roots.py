"""The real folders on this machine, worked out before conftest redirects HOME.

The PathGuard (tests/disposable_vault.py) refuses any test that touches these.
They used to be written out as Grey's own paths; they are derived instead, so
the public repository names nobody's folders and the guard still protects the
real ones on whatever machine runs the tests.

tests/conftest.py imports this module BEFORE it changes HOME. Nothing here
imports musaeus: its config would load the real settings into os.environ.
"""

from __future__ import annotations

import os
from pathlib import Path

#: The real home folder, read before conftest redirects HOME.
REAL_HOME = Path.home()


def _setting(key: str) -> Path | None:
    """*key* from the environment, else from the real settings.env (that one
    key: the file can sit beside secrets); None when neither has it."""
    named = os.environ.get(key, "")
    if not named:
        try:
            settings = REAL_HOME / ".config" / "musaeus" / "settings.env"
            text = settings.read_text(encoding="utf-8-sig")
        except OSError:
            text = ""
        for line in text.splitlines():
            name, sep, value = line.strip().partition("=")
            if sep and name.strip() == key:
                named = value.strip().strip("\"'")  # the last one wins, as in config.py
    return Path(os.path.expanduser(named)) if named else None


#: The live vault this machine is set up for; None where there is none (CI).
REAL_VAULT = _setting("MUSAEUS_VAULT_ROOT")

#: Other folders of this machine the settings name (None where unset).
REAL_BACKUP_ROOT = _setting("MUSAEUS_MASTERS_BACKUP_ROOT")
REAL_SECRETS_DIR = _setting("MUSAEUS_SECRETS_DIR")

#: Nothing under these may be created, read, renamed or deleted by a test.
PROTECTED_REAL_ROOTS: tuple[str, ...] = tuple(
    str(p)
    for p in (
        REAL_HOME / "Music",  # the personal music library
        REAL_HOME / ".config" / "musaeus",  # settings.env / credentials.env / resume_state.json
        # the live vault, and the manual test vault kept beside it
        *((REAL_VAULT, REAL_VAULT.parent / "MUSAEUS_TEST_VAULT") if REAL_VAULT else ()),
        REAL_HOME
        / "Projects"
        / "MUSAEUS_RECOVERY",  # policy.FUTURE_RECOVERY_ROOT: never create/probe
    )
)
