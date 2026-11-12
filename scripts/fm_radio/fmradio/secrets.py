"""Load the ListenBrainz user token. Never from a command-line argument.

A token on a command line lands in shell history, in `ps` output, and in any
log that records the invocation. So: an environment variable, or a file with
mode 600 in the folder MUSAEUS_SECRETS_DIR names (in ~/.config/musaeus/settings.env)
-- secrets never in a project directory or on the Desktop.
"""

from __future__ import annotations

import os
from pathlib import Path

VAR = "LISTENBRAINZ_USER_TOKEN"
FILE_NAME = "listenbrainz.env"


def secrets_file() -> Path | None:
    """listenbrainz.env in MUSAEUS_SECRETS_DIR, or None when that is not set."""
    import musaeus.config  # noqa: F401  -- puts settings.env's values in os.environ

    folder = (os.environ.get("MUSAEUS_SECRETS_DIR") or "").strip()
    return Path(folder).expanduser() / FILE_NAME if folder else None


class TokenMissing(RuntimeError):
    """No token anywhere. Raised early, with instructions rather than a code."""


def load_token(path: Path | None = None) -> str:
    """The environment first, then the secrets file.

    Environment wins so a one-off run can override without editing the file --
    and so a test can inject a fake without going near the real path.
    """
    from_env = (os.environ.get(VAR) or "").strip()
    if from_env:
        return from_env

    p = path or secrets_file()
    if p is not None and p.exists():
        try:
            for raw in p.read_text(encoding="utf-8").splitlines():
                line = raw.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, value = line.partition("=")
                if key.strip() == VAR:
                    # Strip quotes a well-meaning editor may have added. A
                    # quoted token fails auth with a 401 that looks exactly
                    # like a wrong token, which is a miserable thing to debug.
                    return value.strip().strip("'\"")
        except OSError as exc:
            raise TokenMissing(f"cannot read {p}: {exc}") from exc

    where = p or f"<MUSAEUS_SECRETS_DIR>/{FILE_NAME}"
    raise TokenMissing(
        f"no ListenBrainz user token.\n"
        f"  Get one:   https://listenbrainz.org/settings/  (signs in via MusicBrainz)\n"
        f"  Store it:  printf '{VAR}=%s\\n' 'the-token' > {where}\n"
        f"             chmod 600 {where}\n"
        f"             (MUSAEUS_SECRETS_DIR is set in ~/.config/musaeus/settings.env)\n"
        f"  Or set:    export {VAR}=the-token\n"
        f"\n"
        f"  This is a USER TOKEN. It is not a MetaBrainz OAuth client id or\n"
        f"  secret, and not a MetaBrainz access token -- those are different\n"
        f"  credentials for different services and none of them work here."
    )
