"""Workspace discovery: worktrees, the current-checkout marker, and docs.

These build real git repositories rather than mocking `git`. The one fact
this module exists to report -- *which checkout is this process running
from* -- is a property of real paths and real worktrees, and a mocked
`subprocess.run` would assert the shape of a call rather than the effect,
which is the failure mode this project keeps finding.
"""

from __future__ import annotations

import subprocess

import pytest

from musaeus import workspace
from musaeus.workspace import (
    Worktree,
    _parse_worktree_porcelain,
    doc_root,
    list_docs,
    list_worktrees,
    unmerged_commits,
)


def _git(args, cwd):
    return subprocess.run(
        ["git", *args], cwd=str(cwd), capture_output=True, text=True, check=True
    )


@pytest.fixture
def repo(tmp_path):
    """A real repo on `main` with one commit."""
    root = tmp_path / "repo"
    root.mkdir()
    _git(["init", "-q", "-b", "main"], root)
    _git(["config", "user.email", "t@example.invalid"], root)
    _git(["config", "user.name", "Test"], root)
    (root / "a.txt").write_text("one\n")
    _git(["add", "-A"], root)
    _git(["commit", "-qm", "first"], root)
    return root


# ── porcelain parsing ─────────────────────────────────────────────────────────


def test_parse_porcelain_splits_records_on_blank_lines():
    text = (
        "worktree /a\nHEAD abc123\nbranch refs/heads/main\n"
        "\n"
        "worktree /b\nHEAD def456\nbranch refs/heads/work/x\n"
    )
    recs = _parse_worktree_porcelain(text)
    assert [r["worktree"] for r in recs] == ["/a", "/b"]
    assert recs[1]["branch"] == "refs/heads/work/x"


def test_parse_porcelain_keeps_bare_flags():
    recs = _parse_worktree_porcelain("worktree /a\nHEAD abc\ndetached\n")
    assert "detached" in recs[0]


# ── discovery ─────────────────────────────────────────────────────────────────


def test_lists_the_main_worktree_with_branch_stripped(repo):
    trees = list_worktrees(repo)
    assert len(trees) == 1
    assert trees[0].branch == "main"  # not "refs/heads/main"
    assert trees[0].exists


def test_finds_a_linked_worktree(repo, tmp_path):
    other = tmp_path / "wt"
    _git(["worktree", "add", "-q", "-b", "work/x", str(other)], repo)
    branches = {t.branch for t in list_worktrees(repo)}
    assert branches == {"main", "work/x"}


def test_marks_only_the_checkout_the_call_came_from(repo, tmp_path):
    """The whole point: `is_current` must follow the path, not the repo."""
    other = tmp_path / "wt"
    _git(["worktree", "add", "-q", "-b", "work/x", str(other)], repo)

    from_main = {t.branch: t.is_current for t in list_worktrees(repo)}
    assert from_main == {"main": True, "work/x": False}

    from_other = {t.branch: t.is_current for t in list_worktrees(other)}
    assert from_other == {"main": False, "work/x": True}


def test_clean_and_dirty_are_distinguishable(repo):
    assert list_worktrees(repo)[0].dirty == 0
    (repo / "b.txt").write_text("untracked\n")
    assert list_worktrees(repo)[0].dirty == 1


def test_a_worktree_whose_path_is_gone_is_reported_not_raised(repo, tmp_path):
    """An unmounted drive must degrade to a note, never take the console down."""
    other = tmp_path / "wt"
    _git(["worktree", "add", "-q", "-b", "work/x", str(other)], repo)
    for p in sorted(other.rglob("*"), reverse=True):
        p.unlink() if p.is_file() else p.rmdir()
    other.rmdir()

    gone = [t for t in list_worktrees(repo) if t.branch == "work/x"][0]
    assert not gone.exists
    assert gone.dirty == -1
    assert "MISSING" in gone.describe()


def test_not_a_repo_returns_empty_rather_than_raising(tmp_path):
    assert list_worktrees(tmp_path) == []


# ── unmerged commits ──────────────────────────────────────────────────────────


def test_unmerged_commits_counts_only_what_base_lacks(repo, tmp_path):
    other = tmp_path / "wt"
    _git(["worktree", "add", "-q", "-b", "work/x", str(other)], repo)
    (other / "c.txt").write_text("parked\n")
    _git(["add", "-A"], other)
    _git(["commit", "-qm", "parked work"], other)

    tree = [t for t in list_worktrees(repo) if t.branch == "work/x"][0]
    commits = unmerged_commits(tree, base="main", repo=repo)
    assert len(commits) == 1
    assert "parked work" in commits[0]

    main_tree = [t for t in list_worktrees(repo) if t.branch == "main"][0]
    assert unmerged_commits(main_tree, base="main", repo=repo) == []


def test_unmerged_commits_on_a_missing_base_is_empty_not_an_error(repo, tmp_path):
    other = tmp_path / "wt"
    _git(["worktree", "add", "-q", "-b", "work/x", str(other)], repo)
    tree = [t for t in list_worktrees(repo) if t.branch == "work/x"][0]
    assert unmerged_commits(tree, base="no-such-branch", repo=repo) == []


# ── describe ──────────────────────────────────────────────────────────────────


def test_describe_marks_the_current_checkout_with_a_star():
    here = Worktree(
        path=__import__("pathlib").Path("/x"), branch="work/x", head="abc1234",
        is_current=True, exists=True, dirty=0,
    )
    there = Worktree(
        path=__import__("pathlib").Path("/y"), branch="main", head="def5678",
        is_current=False, exists=True, dirty=0,
    )
    assert here.describe().startswith("* ")
    assert there.describe().startswith("  ")


def test_label_falls_back_to_the_short_head_when_detached():
    wt = Worktree(
        path=__import__("pathlib").Path("/x"), branch="", head="abcdef1234567",
        is_current=False, exists=True, dirty=0,
    )
    assert wt.label == "abcdef1"


# ── documentation archive ─────────────────────────────────────────────────────


def test_doc_root_honours_the_env_override(monkeypatch, tmp_path):
    monkeypatch.setenv("MUSAEUS_DOC_ROOT", str(tmp_path))
    assert doc_root() == tmp_path


def test_docs_come_back_newest_first(monkeypatch, tmp_path):
    d = tmp_path / "docs" / "musaeus"
    d.mkdir(parents=True)
    for name, mtime in (("old.md", 1_000_000), ("new.md", 2_000_000)):
        p = d / name
        p.write_text("#\n")
        import os
        os.utime(p, (mtime, mtime))
    monkeypatch.setenv("MUSAEUS_DOC_ROOT", str(tmp_path))
    assert [p.name for p in list_docs()] == ["new.md", "old.md"]


def test_docs_respect_the_limit(monkeypatch, tmp_path):
    d = tmp_path / "docs" / "musaeus"
    d.mkdir(parents=True)
    for i in range(10):
        (d / f"{i}.md").write_text("#\n")
    monkeypatch.setenv("MUSAEUS_DOC_ROOT", str(tmp_path))
    assert len(list_docs(limit=3)) == 3


def test_a_missing_doc_root_is_empty_not_an_error(monkeypatch, tmp_path):
    monkeypatch.setenv("MUSAEUS_DOC_ROOT", str(tmp_path / "nope"))
    assert list_docs() == []


def test_git_failure_degrades_to_empty_string(tmp_path):
    """`_git` swallows on purpose -- assert the swallow, so nobody 'fixes' it."""
    assert workspace._git(["no-such-subcommand"], tmp_path) == ""


def test_a_path_that_exists_but_is_not_a_work_tree_reads_unknown_not_clean(repo, tmp_path):
    """`git status --porcelain` returns "" for a clean tree AND for a failed call.

    Left ambiguous, a directory git cannot read reports "clean" -- a false
    reassurance about a checkout nobody can see. Registered worktree,
    directory present, contents gone: the answer must be "unknown".
    """
    other = tmp_path / "wt"
    _git(["worktree", "add", "-q", "-b", "work/x", str(other)], repo)
    (other / ".git").unlink()  # still a directory, no longer a work tree

    tree = [t for t in list_worktrees(repo) if t.branch == "work/x"][0]
    assert tree.exists
    assert tree.dirty == -1, "a git-unreadable directory must not read as clean"
    assert "unknown" in tree.describe()
