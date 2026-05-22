"""Tests for the ``worktree`` git wrapper.

The happy-path is exercised end-to-end by ``test_orchestrator_iteration``.
What these tests pin is the failure mode: git's stderr must be captured
and surfaced in the raised ``RuntimeError`` so the operator (and the
runlog ``halt_reason`` written by the orchestrator) sees what actually
went wrong, not just a non-zero exit code.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from drain_cycle import worktree


def _init_repo(repo: Path) -> None:
    subprocess.run(["git", "init", "-b", "main"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "t@x"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "T"], cwd=repo, check=True)
    (repo / "README.md").write_text("seed\n")
    subprocess.run(["git", "add", "."], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "seed"], cwd=repo, check=True, capture_output=True)


def test_add_failure_raises_with_git_stderr_in_message(tmp_path: Path) -> None:
    """``git worktree add`` fails when the branch name is already taken.
    The raised RuntimeError must carry git's stderr so the operator can
    diagnose without re-running by hand."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)

    # First add succeeds, taking the branch "ABA-X".
    worktree.add(repo, "ABA-X")
    # Second add fails — same branch.
    with pytest.raises(RuntimeError) as excinfo:
        worktree.add(repo, "ABA-X")

    msg = str(excinfo.value)
    # The message names the operation and includes git's actual complaint
    # (typically "already exists" or "already checked out").
    assert "worktree add" in msg
    assert "ABA-X" in msg


def test_remove_failure_raises_with_git_stderr_in_message(tmp_path: Path) -> None:
    """``git worktree remove`` on a non-existent path fails. The raised
    RuntimeError must carry git's stderr."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)

    bogus = repo / ".worktrees" / "never-existed"
    with pytest.raises(RuntimeError) as excinfo:
        worktree.remove(repo, bogus)

    msg = str(excinfo.value)
    assert "worktree remove" in msg
    # Either git's own "is not a working tree" message or the path itself.
    assert str(bogus) in msg or "working tree" in msg.lower()
