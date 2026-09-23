#!/usr/bin/env python3
"""
MUSAEUS — Push Notification Utility

Sends failure alerts via ntfy.sh (free, no signup required), falling
back to a self-hosted Gotify server if configured. Deliberately a
standalone, MUSAEUS-owned copy rather than an import from ORPHEUS's
SCRIPTS/notify.py -- musaeus_overnight.sh depending on another
project's script path is exactly the kind of fragile cross-project
assumption that caused the original "musaeus: command not found" bug
this notifier exists to catch (ACTIVE_PROJECTS/ORPHEUS isn't even a git
repo, so its layout has no stability guarantee this project can rely
on). Small and stable enough that duplicating it here beats coupling
to it.

Usage (from musaeus_overnight.sh, a shell script -- so CLI, not import):
    python3 scripts/musaeus_notify.py --title "..." --message "..." [--tags warning,skull]

Environment variables (optional):
    NTFY_TOPIC     — ntfy.sh topic (default: orpheus-alerts, since
                     that's the one topic already known to exist in
                     this environment -- override with your own via
                     this env var if you'd rather keep MUSAEUS alerts
                     separate from ORPHEUS's)
    GOTIFY_URL     — Gotify server URL (optional, overrides ntfy)
    GOTIFY_TOKEN   — Gotify app token (required if using Gotify)
"""

from __future__ import annotations

import pathlib

import argparse
import os
import subprocess
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from musaeus import network_policy  # noqa: E402

NTFY_TOPIC = os.environ.get("NTFY_TOPIC", "orpheus-alerts")
NTFY_URL = f"https://ntfy.sh/{NTFY_TOPIC}"

GOTIFY_URL = os.environ.get("GOTIFY_URL", "")
GOTIFY_TOKEN = os.environ.get("GOTIFY_TOKEN", "")


def notify_via_ntfy(title: str, message: str, tags: list[str] | None = None) -> bool:
    """Send a push notification via ntfy.sh using a curl subprocess."""
    if not NTFY_TOPIC:
        return False

    cmd = [
        "curl",
        "-s",
        "-o",
        "/dev/null",
        "-w",
        "%{http_code}",
        "-H",
        f"Title: {title}",
        "-H",
        "Priority: default",
        "-H",
        "Markdown: no",
        "-d",
        message,
        NTFY_URL,
    ]
    if tags:
        cmd.insert(4, "-H")
        cmd.insert(5, f"Tags: {','.join(tags)}")

    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
        http_code = result.stdout.strip()
        if http_code.startswith(("2", "3")):
            return True
        print(f"  [notify] ntfy.sh returned HTTP {http_code}", file=sys.stderr)
        return False
    except Exception as exc:
        print(f"  [notify] Failed to send ntfy.sh notification: {exc}", file=sys.stderr)
        return False


def notify_via_gotify(title: str, message: str, priority: int = 8) -> bool:
    """Send a push notification via a self-hosted Gotify server."""
    if not GOTIFY_URL or not GOTIFY_TOKEN:
        return False

    cmd = [
        "curl",
        "-s",
        "-o",
        "/dev/null",
        "-w",
        "%{http_code}",
        "-X",
        "POST",
        "-H",
        f"X-Gotify-Key: {GOTIFY_TOKEN}",
        "-H",
        "Content-Type: application/json",
        "-d",
        f'{{"title": "{title}", "message": "{message}", "priority": {priority}}}',
        f"{GOTIFY_URL.rstrip('/')}/message",
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
        http_code = result.stdout.strip()
        if http_code.startswith("2"):
            return True
        print(f"  [notify] Gotify returned HTTP {http_code}", file=sys.stderr)
        return False
    except Exception as exc:
        print(f"  [notify] Failed to send Gotify notification: {exc}", file=sys.stderr)
        return False


def _policy_from_env() -> network_policy.NetworkPolicy:
    """Notifying is ALLOWED unless something explicitly says otherwise.

    The default matters more than it looks. network_policy's own default is
    LOCAL_ONLY, which is right for a preview that must not phone home -- but
    wrong here: this script exists to say "the overnight run failed", and a
    failure nobody hears about is worse than the run failing. So the default
    is inverted at this one call site, deliberately, and MUSAEUS_NETWORK is
    how a caller that must stay offline says so.

    P0-19's rehearsal is exactly such a caller. It aborted on the disk guard,
    the wrapper called this script, and an outbound attempt was made from
    inside a rehearsal whose whole contract is that nothing reaches the
    network. That is what this reads.
    """
    raw = os.environ.get("MUSAEUS_NETWORK", "").strip().lower()
    if raw in {"local-only", "local_only", "offline", "off", "none", "deny", "0"}:
        return network_policy.NetworkPolicy.LOCAL_ONLY
    return network_policy.NetworkPolicy.ALLOWED


def send(title: str, message: str, tags: list[str] | None = None) -> bool:
    """Try ntfy.sh first, fall back to Gotify. Returns True if either succeeded."""
    with network_policy.policy(_policy_from_env()):
        try:
            network_policy.check(NTFY_URL)
        except network_policy.NetworkDenied:
            # Loudly, and to the run log -- a suppressed alert must never be a
            # silent one. The topic is not echoed; the host is enough to say
            # what was refused.
            print(
                "  [notify] SUPPRESSED by network policy (MUSAEUS_NETWORK): "
                "no request was made to ntfy.sh",
                file=sys.stderr,
            )
            print(f"  [notify] the alert would have read: {title} -- {message}", file=sys.stderr)
            return False

    sent = notify_via_ntfy(title, message, tags=tags)
    if not sent:
        sent = notify_via_gotify(title, message)
    return sent


def main() -> int:
    parser = argparse.ArgumentParser(description="Send a MUSAEUS push notification")
    parser.add_argument("--title", required=True, help="Notification title")
    parser.add_argument("--message", required=True, help="Message body")
    parser.add_argument(
        "--tags", default="warning", help="Comma-separated ntfy.sh tags (default: warning)"
    )
    args = parser.parse_args()

    tags = [t.strip() for t in args.tags.split(",") if t.strip()]
    if send(args.title, args.message, tags=tags):
        print(f"  [notify] Alert sent to ntfy.sh/{NTFY_TOPIC}")
        return 0
    print("  [notify] No notification channel reachable.")
    print("           Set NTFY_TOPIC env var or GOTIFY_URL + GOTIFY_TOKEN")
    return 1


if __name__ == "__main__":
    sys.exit(main())
