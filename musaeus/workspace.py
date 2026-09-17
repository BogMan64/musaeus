#!/usr/bin/env python3
"""
MUSAEUS — Workspace: sibling worktrees and the documentation archive.

Why this exists
---------------
Two facts about this project are invisible from inside a running console,
and both have already cost a live batch:

  1. **Which checkout is this code running from?** Finding #17 — a running
     process cannot pick up edits to its own source — was found because a
     console launched hours earlier executed stale code. `SourceWatch`
     now catches drift *within* a checkout. It says nothing about *which*
     checkout, and the campaign runs from one while fixes are written in
     another.

  2. **What is parked on the other worktrees?** `work/act23` and
     `work/mbid-tags` hold finished, tested work that is deliberately not
     merged, because any edit in the campaign checkout reaches the next
     batch. Nothing in the console said they existed.

So: discover worktrees rather than hardcode them (`git worktree list`
already knows, and stays right when they move), mark the one this process
is running from, and point at the documentation archive.

Read-only. Nothing here mutates a repo, checks out a branch, or merges.
"""

from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass
from pathlib import Path

_GIT_TIMEOUT = 15

# The documentation archive is a plain folder, not a checkout, and is not
# under version control (see its own README). Overridable, like every other
# path in this project.
_DEFAULT_DOC_ROOT = "/mnt/FORGE2TB/DOCUMENTATION"


def _git(args: list[str], cwd: Path) -> str:
    """Run a git command, returning stdout. Empty string on any failure.

    Deliberately swallowing: this module is a status display. A missing
    git, a detached HEAD, or a worktree on an unmounted drive must degrade
    to "unknown", never take down the console.
    """
    try:
        out = subprocess.run(
            ["git", *args],
            cwd=str(cwd),
            capture_output=True,
            text=True,
            timeout=_GIT_TIMEOUT,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    return out.stdout.strip() if out.returncode == 0 else ""


def package_root() -> Path:
    """The checkout this running code was imported from."""
    return Path(__file__).resolve().parent.parent


@dataclass(frozen=True)
class Worktree:
    """One checkout. `is_current` is the one this process is running from."""

    path: Path
    branch: str
    head: str
    is_current: bool
    exists: bool
    dirty: int  # count of modified/untracked entries; -1 when unknown

    @property
    def label(self) -> str:
        return self.branch or (self.head[:7] if self.head else "detached")

    def describe(self) -> str:
        marker = "*" if self.is_current else " "
        if not self.exists:
            state = "MISSING (path not present)"
        elif self.dirty < 0:
            state = "clean/dirty unknown"
        elif self.dirty == 0:
            state = "clean"
        else:
            state = f"{self.dirty} uncommitted change(s)"
        return f"{marker} {self.label:24s} {self.head[:7]:8s} {state}\n    {self.path}"


def _parse_worktree_porcelain(text: str) -> list[dict[str, str]]:
    """Parse `git worktree list --porcelain` into one dict per worktree.

    Records are separated by blank lines; each line is `key value` or a
    bare flag such as `detached`.
    """
    records: list[dict[str, str]] = []
    current: dict[str, str] = {}
    for line in text.splitlines():
        if not line.strip():
            if current:
                records.append(current)
                current = {}
            continue
        key, _, value = line.partition(" ")
        current[key] = value
    if current:
        records.append(current)
    return records


def list_worktrees(repo: Path | None = None) -> list[Worktree]:
    """Every checkout git knows about, with the current one marked.

    Returns [] when the path is not a git repository — the console shows
    that as a note rather than an error, because a packaged install
    legitimately has no repo.
    """
    root = repo or package_root()
    raw = _git(["worktree", "list", "--porcelain"], root)
    if not raw:
        return []

    here = root.resolve()
    trees: list[Worktree] = []
    for rec in _parse_worktree_porcelain(raw):
        if "worktree" not in rec:
            continue
        path = Path(rec["worktree"])
        branch = rec.get("branch", "")
        if branch.startswith("refs/heads/"):
            branch = branch[len("refs/heads/") :]
        exists = path.is_dir()
        dirty = -1
        if exists:
            status = _git(["status", "--porcelain"], path)
            # "" is ambiguous -- a clean tree and a failed call look alike.
            # Ask git for a value that distinguishes them.
            if status:
                dirty = len(status.splitlines())
            elif _git(["rev-parse", "--is-inside-work-tree"], path) == "true":
                dirty = 0
        trees.append(
            Worktree(
                path=path,
                branch=branch,
                head=rec.get("HEAD", ""),
                is_current=exists and path.resolve() == here,
                exists=exists,
                dirty=dirty,
            )
        )
    return trees


def unmerged_commits(worktree: Worktree, base: str = "main", repo: Path | None = None) -> list[str]:
    """Commits on `worktree`'s branch that `base` does not have.

    This is the question the TODO's "parked on branches" section answers by
    hand today. Empty list when the branch is merged, unknown, or base is
    missing.
    """
    if not worktree.branch or not worktree.exists:
        return []
    root = repo or package_root()
    out = _git(["log", "--oneline", f"{base}..{worktree.branch}"], root)
    return out.splitlines() if out else []


def doc_root() -> Path:
    """The documentation archive. `MUSAEUS_DOC_ROOT` overrides."""
    return Path(os.environ.get("MUSAEUS_DOC_ROOT", _DEFAULT_DOC_ROOT))


def list_docs(subdir: str = "docs/musaeus", limit: int = 30) -> list[Path]:
    """Markdown under the archive, newest first. [] when the root is absent."""
    root = doc_root() / subdir
    if not root.is_dir():
        return []
    try:
        docs = [p for p in root.rglob("*.md") if p.is_file()]
    except OSError:
        return []
    docs.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    return docs[:limit]
