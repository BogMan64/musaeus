"""
MUSAEUS — First-Run Setup Wizard

Triggered automatically on first run (no settings.env) or via `musaeus setup`.
Collects:
  1. Music directory (vault root) — where processed music lives
  2. Inbox path — where new files arrive for processing
  3. API keys — with links to registration pages

Saves to ~/.config/musaeus/settings.env and credentials.env.
"""

from __future__ import annotations

import getpass
import os
import sys
from pathlib import Path

_CONFIG_DIR = Path.home() / ".config" / "musaeus"
_SETTINGS_FILE = _CONFIG_DIR / "settings.env"
_CREDENTIALS_FILE = _CONFIG_DIR / "credentials.env"

# API keys the system can use, with registration URLs
API_KEYS = {
    # Core (used by default pipeline stages)
    "LASTFM_API_KEY": {
        "label": "Last.fm",
        "url": "https://www.last.fm/api/account/create",
        "required": False,
        "used_by": "Genre enrichment (enrich stage)",
    },
    "GROQ_API_KEY": {
        "label": "Groq",
        "url": "https://console.groq.com/keys",
        "required": False,
        "used_by": "AI metadata reviewer (reviewer stage)",
    },
    "ACOUSTICID_API_KEY": {
        "label": "AcousticID",
        "url": "https://acoustid.org/api-key",
        "required": False,
        "used_by": "Acoustic fingerprint dedup (acousticid stage)",
    },
    # Extended (used by optional stages)
    "MUSICBRAINZ_API_KEY": {
        "label": "MusicBrainz",
        "url": "https://musicbrainz.org/doc/MusicBrainz_API",
        "required": False,
        "used_by": "Artist/release enrichment (mb-enrich stage)",
    },
    "DISCOGS_CONSUMER_KEY": {
        "label": "Discogs Consumer Key",
        "url": "https://www.discogs.com/settings/developers",
        "required": False,
        "used_by": "Artist identity fallback (mb-enrich stage, when MusicBrainz has no match)",
    },
    "DISCOGS_CONSUMER_SECRET": {
        "label": "Discogs Consumer Secret",
        "url": "https://www.discogs.com/settings/developers",
        "required": False,
        "used_by": "Artist identity fallback (mb-enrich stage, paired with the consumer key)",
    },
    "SPOTIFY_CLIENT_ID": {
        "label": "Spotify Client ID",
        "url": "https://developer.spotify.com/dashboard/applications",
        "required": False,
        "used_by": "Genre classification (5-source voting)",
    },
    "SPOTIFY_CLIENT_SECRET": {
        "label": "Spotify Client Secret",
        "url": "https://developer.spotify.com/dashboard/applications",
        "required": False,
        "used_by": "Genre classification (5-source voting)",
    },
    "OPENROUTER_API_KEY": {
        "label": "OpenRouter",
        "url": "https://openrouter.ai/keys",
        "required": False,
        "used_by": "AI code review / overnight self-heal",
    },
}


def _load_env(path: Path) -> dict[str, str]:
    """Parse a KEY=VALUE env file."""
    env: dict[str, str] = {}
    if path.exists():
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, val = line.partition("=")
                env[key.strip()] = val.strip().strip("\"'")
    return env


def _save_env(path: Path, env: dict[str, str]) -> None:
    """Write a KEY=VALUE env file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        for key, val in sorted(env.items()):
            f.write(f"{key}={val}\n")


def needs_setup() -> bool:
    """Return True if the setup wizard should run (no vault configured)."""
    if not _SETTINGS_FILE.exists():
        return True
    env = _load_env(_SETTINGS_FILE)
    return not env.get("MUSAEUS_VAULT_ROOT")


def _ask(prompt: str, default: str = "") -> str:
    """Input with default shown."""
    suffix = f" [{default}]" if default else ""
    try:
        val = input(f"  {prompt}{suffix}: ").strip()
    except (EOFError, KeyboardInterrupt):
        print()
        return default
    return val or default


def run_wizard(force: bool = False) -> bool:
    """Run the interactive setup wizard.

    Returns True if setup completed, False if user cancelled.
    """
    print()
    print("=" * 60)
    print("  MUSAEUS — Setup Wizard")
    print("=" * 60)
    print()

    _CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    settings_env = _load_env(_SETTINGS_FILE)
    creds_env = _load_env(_CREDENTIALS_FILE)

    # ── Step 1: Music directory (vault root) ──────────────────────────────────
    print("  Step 1: Where should MUSAEUS store your processed music library?")
    print("          This is the 'vault' — the canonical home for your collection.")
    print()

    current_vault = settings_env.get("MUSAEUS_VAULT_ROOT", "")
    vault_root = _ask("Vault root path", default=current_vault)

    if not vault_root:
        print("  Vault root is required. Aborting setup.")
        return False

    vault_path = Path(vault_root).resolve()
    if not vault_path.is_absolute():
        print(f"  Error: must be an absolute path (got: {vault_root})")
        return False

    settings_env["MUSAEUS_VAULT_ROOT"] = str(vault_path)
    print(f"  ✓ Vault: {vault_path}")
    print()

    # ── Step 2: Inbox directory ───────────────────────────────────────────────
    print("  Step 2: Where do new music files arrive for processing?")
    print("          (This is the folder you drop new downloads into.)")
    print()

    default_inbox = settings_env.get("MUSAEUS_INBOX", str(vault_path / "INBOX"))
    inbox = _ask("Inbox path", default=default_inbox)
    inbox_path = Path(inbox).resolve()
    settings_env["MUSAEUS_INBOX"] = str(inbox_path)
    print(f"  ✓ Inbox: {inbox_path}")
    print()

    # ── Step 3: API Keys ──────────────────────────────────────────────────────
    print("  Step 3: API Keys (optional — press Enter to skip any)")
    print("          These enable enrichment, genre tagging, and AI review.")
    print()

    for key, info in API_KEYS.items():
        current = creds_env.get(key, "")
        status = f"(current: {current[:8]}...)" if current else "(not set)"
        print(f"  {info['label']} {status}")
        print(f"    Used by: {info['used_by']}")
        print(f"    Get key: {info['url']}")
        val = _ask(f"  {key}", default=current)
        if val and val != current:
            creds_env[key] = val
            print("    ✓ Updated")
        elif val:
            print("    ✓ Kept existing")
        else:
            print("    ⚠ Skipped (stage will be a no-op)")
        print()

    # ── Save ──────────────────────────────────────────────────────────────────
    _save_env(_SETTINGS_FILE, settings_env)
    _save_env(_CREDENTIALS_FILE, creds_env)

    # Create directories
    for d in (
        vault_path,
        inbox_path,
        vault_path / "STAGING",
        vault_path / "QUARANTINE",
        vault_path / "RUNS",
    ):
        d.mkdir(parents=True, exist_ok=True)

    print("=" * 60)
    print("  ✓ Setup complete! Configuration saved to:")
    print(f"    {_SETTINGS_FILE}")
    print(f"    {_CREDENTIALS_FILE}")
    print()
    print("  Run 'musaeus run' to start the pipeline.")
    print("  Run 'musaeus setup' to change these settings later.")
    print("=" * 60)
    print()
    return True


# ── API key manager (console 'Enter/Update API Keys' menu) ─────────────────────

# Exactly the four keys MusicConfig.from_env() actually reads (config.py) --
# a subset of the broader API_KEYS dict above, which also lists keys no
# stage currently consumes through the config object (MusicBrainz, Discogs,
# Spotify client id/secret).
MANAGED_KEYS = ["GROQ_API_KEY", "LASTFM_API_KEY", "OPENROUTER_API_KEY", "ACOUSTICID_API_KEY"]


def _confirm(prompt: str, default: bool = False) -> bool:
    suffix = " [Y/n]" if default else " [y/N]"
    try:
        val = input(f"  {prompt}{suffix}: ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        print()
        return default
    if not val:
        return default
    return val in ("y", "yes")


def _read_secret(prompt: str) -> str:
    """Read a secret value, masked via getpass when the terminal supports it."""
    if sys.stdin.isatty():
        try:
            return getpass.getpass(f"  {prompt}: ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return ""
    print("    ⚠ Non-interactive terminal — input will not be masked.")
    try:
        return input(f"  {prompt}: ").strip()
    except (EOFError, KeyboardInterrupt):
        print()
        return ""


def run_api_key_manager() -> None:
    """
    Interactive 'Enter/Update API Keys' menu: walk the four keys
    MusicConfig actually reads (config.py's from_env()), show current
    status with the same checkmark convention as MusicConfig.describe(),
    and let Grey update any of them one at a time. Always writes to
    credentials.env specifically -- never settings.env, which is reserved
    for paths -- since credentials.env is the file already gitignored for
    secrets (see module docstring / config.py's loading-priority list).

    Precedence gotcha: config.py's _load_env() loads both settings.env and
    credentials.env via os.environ.setdefault(), meaning anything already
    present in the process environment (most commonly a shell export)
    always wins over what's written here. Detected by comparing the
    resolved os.environ value against what credentials.env itself
    currently holds for that key -- a mismatch means some higher-
    precedence source is active and an update here will be saved but will
    have no effect until that source is removed.
    """
    print()
    print("=" * 60)
    print("  MUSAEUS — Enter/Update API Keys")
    print("=" * 60)

    _CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    creds_env = _load_env(_CREDENTIALS_FILE)
    changed = False

    for key in MANAGED_KEYS:
        info = API_KEYS[key]
        file_value = creds_env.get(key, "")
        env_value = os.environ.get(key, "")
        resolved_set = bool(env_value)

        print()
        print(f"  {info['label']} ({key})")
        print(f"    Used by: {info['used_by']}")
        print(f"    Status: {'✓ set' if resolved_set else '✗ not set'}")

        if env_value and env_value != file_value:
            print(
                f"    ⚠ A higher-precedence source (most likely a shell-exported "
                f"{key}) is currently overriding credentials.env. Updating it here "
                f"will be saved but will have NO effect until that export is removed."
            )

        if not _confirm("Update this key?", default=False):
            continue

        new_val = _read_secret(f"New value for {key}")
        if not new_val:
            print("    ⚠ Empty input — left unchanged.")
            continue

        creds_env[key] = new_val
        _save_env(_CREDENTIALS_FILE, creds_env)
        changed = True
        print(f"    ✓ Saved to {_CREDENTIALS_FILE}")

        if env_value and env_value != new_val:
            print(
                f"    ⚠ Reminder: {key} is still present in this session's environment "
                f"with a different value -- the saved key will not take effect until "
                f"that export is unset (and the process restarted)."
            )
        else:
            # No higher-precedence value was active, so it's safe -- and
            # matches what a fresh process would load from credentials.env
            # anyway -- to make this take effect immediately.
            os.environ[key] = new_val
            print("    ✓ Active immediately for this session.")

    print()
    print("=" * 60)
    if changed:
        print(f"  ✓ Credentials updated: {_CREDENTIALS_FILE}")
        print("  If any key above showed the shell-export warning, unset that")
        print("  variable (and restart musaeus) for the saved value to take effect.")
    else:
        print("  No changes made.")
    print("=" * 60)
    print()
