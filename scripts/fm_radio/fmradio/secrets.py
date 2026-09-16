"""Load the ListenBrainz user token. Never from a command-line argument.

A token on a command line lands in shell history, in `ps` output, and in any
log that records the invocation. So: an environment variable, or a file with
mode 600 on the backup drive -- Grey's own convention, secrets never in a
project directory or on the Desktop.
"""

from __future__ import annotations

import os
from pathlib import Path

#: Grey's convention: /mnt/NUC8TB_BACKUP/SECRETS/, not the project tree.
DEFAULT_SECRETS = Path("/mnt/NUC8TB_BACKUP/SECRETS/listenbrainz.env")
VAR = "LISTENBRAINZ_USER_TOKEN"


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

    p = path or DEFAULT_SECRETS
    if p.exists():
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

    raise TokenMissing(
        f"no ListenBrainz user token.\n"
        f"  Get one:   https://listenbrainz.org/settings/  (signs in via MusicBrainz)\n"
        f"  Store it:  printf '{VAR}=%s\\n' 'the-token' > {DEFAULT_SECRETS}\n"
        f"             chmod 600 {DEFAULT_SECRETS}\n"
        f"  Or set:    export {VAR}=the-token\n"
        f"\n"
        f"  This is a USER TOKEN. It is not a MetaBrainz OAuth client id or\n"
        f"  secret, and not a MetaBrainz access token -- those are different\n"
        f"  credentials for different services and none of them work here."
    )
