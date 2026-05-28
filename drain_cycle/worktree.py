"""Thin wrapper around ``git worktree``.

Each issue gets ``.worktrees/<issue-identifier>/`` branched off ``main``,
used once, then removed on Done — or preserved on halt so a later re-run
can resume against the committed work (see ``docs/design-decisions.md``
§14).

``git worktree`` stderr is captured and surfaced in the raised
``RuntimeError`` on failure. The orchestrator's pre-spawn try/except
threads the message into the runlog's ``halt_reason`` so the operator
sees git's actual diagnostic (dirty tree, branch already exists,
missing ``main``) rather than just a non-zero exit code.
"""
from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from . import telemetry

BASE_BRANCH = "main"
WORKTREE_DIR = ".worktrees"


@dataclass(frozen=True)
class WorktreeHandle:
    """A prepared worktree, together with whether it was reused.

    ``resumed`` is ``True`` when ``ensure`` found a pre-existing worktree
    registered at the expected path (typically left behind by an earlier
    halted run). Callers thread the flag into the spawn-time prompt so
    the agent knows it is continuing from prior committed work rather
    than starting fresh.
    """

    path: Path
    resumed: bool


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


def ensure(repo: Path, identifier: str) -> WorktreeHandle:
    """Reuse a preserved worktree if one is already registered, else add.

    A worktree registered at ``repo/.worktrees/<identifier>`` is reused
    as-is — no mutating git command is run, so a dirty index, staged or
    untracked files, and the gitignored config symlinks all survive
    untouched. Any other state (no entry at that path) falls through to
    :func:`add`, whose ``RuntimeError`` on a leftover branch or orphan
    directory is what the orchestrator's existing pre-spawn handler
    turns into the clean ``Halt: … — setup failed: …`` line.
    """
    worktree_path = repo / WORKTREE_DIR / identifier
    with telemetry.tracer.start_as_current_span("drain.worktree.ensure") as span:
        span.set_attribute("worktree.identifier", identifier)
        span.set_attribute("worktree.repo", repo.name)
        span.set_attribute("worktree.path", str(worktree_path))
        if _is_registered_worktree(repo, worktree_path):
            span.set_attribute("worktree.resumed", True)
            return WorktreeHandle(path=worktree_path, resumed=True)
        span.set_attribute("worktree.resumed", False)
    return WorktreeHandle(path=add(repo, identifier), resumed=False)


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


def _is_registered_worktree(repo: Path, worktree_path: Path) -> bool:
    """Return ``True`` if git lists a worktree at ``worktree_path``.

    Parses ``git worktree list --porcelain -z`` for a ``worktree <path>``
    record matching ``worktree_path.resolve()``. ``-z`` makes each record
    NUL-separated and each field NUL-terminated, so paths with embedded
    spaces or newlines are unambiguous. The resolve step matches git's
    own canonicalisation (symlinks, ``..``) so a worktree under a
    symlinked repo path still matches the entry git printed. A non-zero
    git exit is treated as not-registered so ``ensure`` falls through to
    ``add``, whose error surfaces git's real diagnostic via the
    orchestrator's pre-spawn halt path.
    """
    target = worktree_path.resolve()
    result = subprocess.run(
        ["git", "worktree", "list", "--porcelain", "-z"],
        cwd=repo,
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        return False
    for field in result.stdout.split("\0"):
        if not field.startswith("worktree "):
            continue
        listed = Path(field.removeprefix("worktree "))
        if listed.resolve() == target:
            return True
    return False


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
