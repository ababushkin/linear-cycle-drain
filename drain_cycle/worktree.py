"""Thin wrapper around ``git worktree``.

Each issue gets ``.worktrees/<issue-identifier>/`` branched off ``main`` per
README §3, used once, then removed on Done.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

BASE_BRANCH = "main"
WORKTREE_DIR = ".worktrees"


def add(repo: Path, identifier: str) -> Path:
    """Create a worktree branched off ``main`` for ``identifier``.

    Returns the absolute path to the new worktree.
    """
    worktree_path = repo / WORKTREE_DIR / identifier
    subprocess.run(
        [
            "git",
            "worktree",
            "add",
            "-b",
            identifier,
            str(worktree_path),
            BASE_BRANCH,
        ],
        cwd=repo,
        check=True,
    )
    return worktree_path


def remove(repo: Path, worktree_path: Path) -> None:
    """Remove a worktree previously created by :func:`add`."""
    subprocess.run(
        ["git", "worktree", "remove", str(worktree_path)],
        cwd=repo,
        check=True,
    )
