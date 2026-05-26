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

import os
import subprocess
from pathlib import Path
from typing import Iterable

from . import telemetry

BASE_BRANCH = "main"
WORKTREE_DIR = ".worktrees"


def add(repo: Path, identifier: str) -> Path:
    """Create a worktree branched off ``main`` for ``identifier``.

    Returns the absolute path to the new worktree.
    """
    worktree_path = repo / WORKTREE_DIR / identifier
    with telemetry.tracer.start_as_current_span("drain.worktree.add") as span:
        span.set_attribute("worktree.identifier", identifier)
        span.set_attribute("worktree.repo", repo.name)
        span.set_attribute("worktree.path", str(worktree_path))
        _run_git(
            ["worktree", "add", "-b", identifier, str(worktree_path), BASE_BRANCH],
            cwd=repo,
        )
    return worktree_path


def link_project_config(
    repo: Path, worktree_path: Path, names: Iterable[str]
) -> list[Path]:
    """Symlink gitignored project-scoped config from ``repo`` into the worktree.

    A git worktree checks out only tracked files, so gitignored project config
    (``.claude/`` settings/hooks/agents/skills, a root ``.mcp.json``) is absent.
    Linking the repo's real entries in gives a worker the same settings, hooks,
    agents, skills, and MCP config as an interactive session at the repo root —
    and because the link points at the live dir, a stateful hook reads and
    writes the repo's actual config exactly as a non-worktree run would.

    For each name: skip it if absent in ``repo`` (a clean no-op for repos
    without that config) or if something already occupies that path in the
    worktree (a tracked entry git checked out, or a pre-existing link). The
    check uses ``os.path.lexists`` so a dangling link counts as present and is
    never clobbered. Returns the links created.
    """
    created: list[Path] = []
    repo = repo.resolve()
    for name in names:
        source = repo / name
        if not source.exists():
            continue
        link = worktree_path / name
        if os.path.lexists(link):
            continue
        os.symlink(source, link)
        created.append(link)
    return created


def remove(repo: Path, worktree_path: Path) -> None:
    """Remove a worktree previously created by :func:`add`."""
    with telemetry.tracer.start_as_current_span("drain.worktree.remove") as span:
        span.set_attribute("worktree.repo", repo.name)
        span.set_attribute("worktree.path", str(worktree_path))
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
