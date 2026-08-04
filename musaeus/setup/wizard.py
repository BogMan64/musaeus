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

import os
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
    "DISCOGS_API_KEY": {
        "label": "Discogs",
        "url": "https://www.discogs.com/settings/developers",
        "required": False,
        "used_by": "Genre classification (5-source voting)",
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
            print(f"    ✓ Updated")
        elif val:
            print(f"    ✓ Kept existing")
        else:
            print(f"    ⚠ Skipped (stage will be a no-op)")
        print()

    # ── Save ──────────────────────────────────────────────────────────────────
    _save_env(_SETTINGS_FILE, settings_env)
    _save_env(_CREDENTIALS_FILE, creds_env)

    # Create directories
    for d in (vault_path, inbox_path, vault_path / "STAGING",
              vault_path / "QUARANTINE", vault_path / "RUNS"):
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
