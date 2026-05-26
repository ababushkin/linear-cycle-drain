"""Cycle-halt path for the orchestrator.

Pins that when ``linear.pending_issues`` raises ``DependencyCycleError``,
the orchestrator:
  - exits 1
  - records ``cycle_halt_reason`` in the run log
  - does not create any worktrees
  - does not call ``linear.set_state``
  - does not spawn any claude session
"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from drain_cycle import linear, orchestrator, repos
from drain_cycle.linear import DependencyCycleError


_TEST_REPO_NAME = "test-repo"


def _stub_repos(repo_path: Path) -> repos.Repos:
    return repos.Repos(mapping={_TEST_REPO_NAME: repo_path})


def _init_repo(repo: Path) -> None:
    subprocess.run(["git", "init", "-b", "main"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "t@x"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "T"], cwd=repo, check=True)
    (repo / "README.md").write_text("seed\n")
    subprocess.run(["git", "add", "."], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "seed"], cwd=repo, check=True, capture_output=True)


def test_orchestrator_cycle_halt_exits_1_and_records_reason(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """DependencyCycleError from pending_issues causes exit 1, a Halt: line on
    stderr, cycle_halt_reason in the run log, no worktrees, and no set_state."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)
    monkeypatch.chdir(repo)
    monkeypatch.setenv("HOME", str(tmp_path))

    cycle_identifiers = ["ABA-X", "ABA-Y"]

    def raising_pending_issues(cycle_id: str):
        raise DependencyCycleError(cycle_identifiers)

    def forbidden_set_state(issue_id: str, state_name: str) -> None:
        raise AssertionError("set_state must not be called on cycle-halt path")

    forbidden_claude = tmp_path / "forbidden-claude.sh"
    forbidden_claude.write_text("#!/bin/sh\nexit 99\n")
    forbidden_claude.chmod(0o755)

    monkeypatch.setattr(linear, "current_cycle_id", lambda: "cycle-id")
    monkeypatch.setattr(linear, "pending_issues", raising_pending_issues)
    monkeypatch.setattr(linear, "set_state", forbidden_set_state)
    monkeypatch.setattr(orchestrator, "_CLAUDE_CMD", [str(forbidden_claude)])

    exit_code = orchestrator.run(_stub_repos(repo))

    assert exit_code == 1

    # cycle_halt_reason is recorded in the run log.
    runs_dir = tmp_path / ".drain-cycle" / "runs"
    payloads = [json.loads(p.read_text()) for p in runs_dir.glob("cycle-id-*.json")]
    assert len(payloads) == 1
    payload = payloads[0]
    assert payload["cycle_halt_reason"] is not None
    assert "ABA-X" in payload["cycle_halt_reason"]
    assert "ABA-Y" in payload["cycle_halt_reason"]

    # A Halt: line appears on stderr.
    stderr_lines = capsys.readouterr().err.splitlines()
    halt_lines = [line for line in stderr_lines if line.startswith("Halt:")]
    assert len(halt_lines) == 1
    assert "ABA-X" in halt_lines[0]
    assert "ABA-Y" in halt_lines[0]

    # No entries were recorded (nothing ran).
    assert payload["entries"] == []

    # No worktrees were created.
    worktrees_dir = repo / ".worktrees"
    assert not worktrees_dir.exists() or not any(worktrees_dir.iterdir())
