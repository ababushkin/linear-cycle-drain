"""Thin wrapper around ``git worktree``.

Each issue gets ``.worktrees/<issue-identifier>/`` branched off ``main`` per
README §3, used once, then removed on Done.

``git worktree`` stderr is captured and surfaced in the raised
``RuntimeError`` on failure. The orchestrator's pre-spawn try/except
threads the message into the runlog's ``halt_reason`` so the operator
sees git's actual diagnostic (dirty tree, branch already exists,
missing ``main``) rather than just a non-zero exit code.
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
    _run_git(
        ["worktree", "add", "-b", identifier, str(worktree_path), BASE_BRANCH],
        cwd=repo,
    )
    return worktree_path


def remove(repo: Path, worktree_path: Path) -> None:
    """Remove a worktree previously created by :func:`add`."""
    _run_git(["worktree", "remove", str(worktree_path)], cwd=repo)


def _run_git(args: list[str], *, cwd: Path) -> None:
    result = subprocess.run(
        ["git", *args],
        cwd=cwd,
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"git {' '.join(args)} failed (exit {result.returncode}): "
            f"{result.stderr.strip() or result.stdout.strip() or '<no output>'}"
        )
