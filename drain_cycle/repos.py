"""Per-issue target-repo resolution.

The orchestrator used to be single-repo by construction: every worktree
landed under ``Path.cwd()``. Cycles in this workspace span multiple repos
by design — ``linear-workflow.md`` makes "Affected repos" part of the
six initiative-readiness fields, and the Ops slot deliberately holds
cross-repo maintenance issues — so each issue now carries a
``repo:<name>`` label and ``~/.drain-cycle/repos.yml`` maps the name to
an absolute path on disk.

Two distinct error types so the two halt paths can branch cleanly:

* ``RepoConfigError`` — startup-time problems (missing file, malformed
  YAML, empty, wrong top-level shape). The CLI catches it before any
  Linear traffic, prints to stderr, exits 1, and does **not** write a
  run-log entry — there is no cycle to log against yet.
* ``RepoResolutionError`` — per-issue problems (no ``repo:`` label,
  multiple ``repo:`` labels, name not in config, resolved path missing).
  The orchestrator catches it as a pre-spawn halt: write a run-log
  entry, print the matching ``Halt:`` line, exit 1. No revert is
  attempted because no Linear state was moved yet.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

_CONFIG_DISPLAY = "~/.drain-cycle/repos.yml"
_REPO_LABEL_PREFIX = "repo:"

DEFAULT_WORKTREE_CONFIG_PATHS = (".claude", ".mcp.json")
"""Project-scoped config a worker should inherit inside its worktree.

A git worktree checks out only tracked files, so gitignored project config
(`.claude/`, a root `.mcp.json`) is absent unless symlinked in. These two
defaults suit any repo; an operator whose repo uses other tooling (e.g. the
`entire.io` tool's `.entire/`) extends the set via ``worktree_config_paths`` in
``repos.yml``. See :func:`drain_cycle.worktree.link_project_config`."""

_MISSING_CONFIG_HINT = (
    "create it mapping each repo: label to an absolute path, e.g.:\n\n"
    "  repos:\n"
    "    drain-cycle: /Users/you/src/drain-cycle\n"
    "    pde-skills:  /Users/you/src/pde-skills\n\n"
    "see docs/repos.example.yml for a template."
)


class RepoConfigError(RuntimeError):
    """``repos.yml`` is missing or malformed."""


class RepoResolutionError(RuntimeError):
    """An issue cannot be mapped to a target repo."""


def default_config_path() -> Path:
    """Resolved per call so tests can redirect via ``monkeypatch.setenv("HOME", ...)``."""
    return Path.home() / ".drain-cycle" / "repos.yml"


@dataclass(frozen=True)
class Repos:
    """Loaded ``repos.yml`` mapping (``name`` → absolute ``Path``)."""

    mapping: dict[str, Path]
    worktree_config_paths: tuple[str, ...] = DEFAULT_WORKTREE_CONFIG_PATHS

    def resolve(self, issue: dict[str, Any]) -> Path:
        labels = [
            label
            for label in issue.get("labels", [])
            if label.startswith(_REPO_LABEL_PREFIX)
        ]
        if not labels:
            raise RepoResolutionError(f"no {_REPO_LABEL_PREFIX} label on issue")
        if len(labels) > 1:
            names = ", ".join(sorted(label.removeprefix(_REPO_LABEL_PREFIX) for label in labels))
            raise RepoResolutionError(f"multiple {_REPO_LABEL_PREFIX} labels: {names}")
        name = labels[0].removeprefix(_REPO_LABEL_PREFIX)
        try:
            path = self.mapping[name]
        except KeyError:
            raise RepoResolutionError(
                f'repo "{name}" not in {_CONFIG_DISPLAY}'
            ) from None
        if not path.exists():
            raise RepoResolutionError(f"resolved path {path} does not exist")
        return path


def _parse_worktree_config_paths(value: Any) -> tuple[str, ...]:
    """Validate the optional ``worktree_config_paths`` key.

    ``None`` (key absent) yields the default. A present value must be a
    non-empty list of relative-path strings — entries are symlinked into a
    worktree, so an absolute path or one containing ``..`` could point outside
    the repo and is rejected. Every failure raises ``RepoConfigError`` naming
    the offending entry.
    """
    if value is None:
        return DEFAULT_WORKTREE_CONFIG_PATHS
    if not isinstance(value, list) or not value:
        raise RepoConfigError(
            f"{_CONFIG_DISPLAY} 'worktree_config_paths' must be a non-empty list"
        )
    entries: list[str] = []
    for entry in value:
        if not isinstance(entry, str) or not entry.strip():
            raise RepoConfigError(
                f"{_CONFIG_DISPLAY} 'worktree_config_paths' entry {entry!r}: "
                "must be a non-empty string"
            )
        if Path(entry).is_absolute():
            raise RepoConfigError(
                f"{_CONFIG_DISPLAY} 'worktree_config_paths' entry {entry!r}: "
                "must be a relative path, not absolute"
            )
        if ".." in Path(entry).parts:
            raise RepoConfigError(
                f"{_CONFIG_DISPLAY} 'worktree_config_paths' entry {entry!r}: "
                "must not contain '..'"
            )
        entries.append(entry)
    return tuple(entries)


def load(path: Path | None = None) -> Repos:
    """Read ``repos.yml`` and return a validated ``Repos``.

    Tilde-prefixed paths inside the file are expanded against ``$HOME``.
    Every failure mode raises ``RepoConfigError`` with a message that
    names the on-disk problem so the operator can fix it without
    re-reading source.
    """
    if path is None:
        path = default_config_path()
    if not path.exists():
        raise RepoConfigError(
            f"{_CONFIG_DISPLAY} not found at {path}\n{_MISSING_CONFIG_HINT}"
        )
    try:
        data = yaml.safe_load(path.read_text())
    except yaml.YAMLError as exc:
        raise RepoConfigError(
            f"{_CONFIG_DISPLAY} is not valid YAML: {exc}"
        ) from None
    if data is None:
        raise RepoConfigError(f"{_CONFIG_DISPLAY} is empty")
    if not isinstance(data, dict) or "repos" not in data:
        raise RepoConfigError(
            f"{_CONFIG_DISPLAY} must have a top-level 'repos:' key"
        )
    block = data["repos"]
    if not isinstance(block, dict) or not block:
        raise RepoConfigError(
            f"{_CONFIG_DISPLAY} 'repos:' must be a non-empty mapping"
        )
    mapping: dict[str, Path] = {}
    for name, value in block.items():
        if not isinstance(value, str):
            raise RepoConfigError(
                f"{_CONFIG_DISPLAY} entry {name!r}: path must be a string"
            )
        mapping[str(name)] = Path(value).expanduser()
    worktree_config_paths = _parse_worktree_config_paths(
        data.get("worktree_config_paths")
    )
    return Repos(mapping=mapping, worktree_config_paths=worktree_config_paths)
