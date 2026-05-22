"""Halt-on-not-Done test for the orchestrator (Task 5 / ABA-202).

Task 3 (ABA-200) added the iteration loop and a minimal non-Done branch that
returns ``1`` without removing the worktree. This test pins that contract:
if the spawned session exits without flipping the issue to Done, the
orchestrator must (a) exit non-zero, (b) leave the worktree on disk so the
operator can inspect it, and (c) not touch any subsequent issue.

Substitution choices mirror ``test_orchestrator_iteration.py``: real git
repo, in-process Linear stub via attribute monkey-patching, fake ``claude``
shell script as ``_CLAUDE_CMD``. The difference is the script: here it
exits without writing the marker file, so the stubbed ``get_issue`` keeps
returning the original Todo state — which is exactly the production
failure mode this test guards against.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from drain_cycle import linear, orchestrator


def _issue(identifier: str, priority: int, sort_order: float) -> dict:
    return {
        "id": f"id-{identifier}",
        "identifier": identifier,
        "title": f"Title for {identifier}",
        "description": f"Body for {identifier}",
        "priority": priority,
        "sortOrder": sort_order,
        "state": {"type": "unstarted", "name": "Todo"},
    }


def _init_repo(repo: Path) -> None:
    subprocess.run(["git", "init", "-b", "main"], cwd=repo, check=True, capture_output=True)
    subprocess.run(
        ["git", "config", "user.email", "test@example.com"], cwd=repo, check=True
    )
    subprocess.run(["git", "config", "user.name", "Test"], cwd=repo, check=True)
    (repo / "README.md").write_text("seed\n")
    subprocess.run(["git", "add", "."], cwd=repo, check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-m", "seed"], cwd=repo, check=True, capture_output=True
    )


def test_orchestrator_halts_when_spawn_leaves_issue_not_done(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)
    monkeypatch.chdir(repo)
    monkeypatch.setenv("HOME", str(tmp_path))

    # Two issues; the first will be picked, the spawn will exit without
    # marking it Done. The second must remain untouched.
    first = _issue("ABA-FIRST", priority=1, sort_order=1.0)
    second = _issue("ABA-SECOND", priority=2, sort_order=2.0)
    raw_issues = [first, second]
    issues_by_id = {i["id"]: i for i in raw_issues}

    get_issue_calls: list[str] = []

    def fake_current_cycle_id() -> str:
        return "stub-cycle"

    def fake_pending_issues(cycle_id: str) -> list[dict]:
        return linear._sort_pending_issues(raw_issues)

    def fake_get_issue(issue_id: str) -> dict:
        # The fake claude script never flips state, so this always returns
        # the original Todo issue. Recording the calls lets us prove the
        # orchestrator stopped after the first re-fetch.
        get_issue_calls.append(issue_id)
        return issues_by_id[issue_id]

    monkeypatch.setattr(linear, "current_cycle_id", fake_current_cycle_id)
    monkeypatch.setattr(linear, "pending_issues", fake_pending_issues)
    monkeypatch.setattr(linear, "get_issue", fake_get_issue)
    # set_state is exercised by tests/test_orchestrator_set_state.py; here it's
    # a no-op so this test stays focused on halt behaviour.
    monkeypatch.setattr(linear, "set_state", lambda issue_id, state_name: None)

    fake_claude = _write_fake_claude_script(tmp_path)
    monkeypatch.setattr(orchestrator, "_CLAUDE_CMD", [str(fake_claude)])

    exit_code = orchestrator.run()

    assert exit_code != 0
    # Only the first issue was ever re-fetched — the loop bailed before
    # touching the second.
    assert get_issue_calls == [first["id"]]
    # Second issue's stub state is unchanged.
    assert second["state"] == {"type": "unstarted", "name": "Todo"}
    # First issue's worktree is still on disk for the operator to inspect.
    first_worktree = repo / ".worktrees" / first["identifier"]
    assert first_worktree.is_dir()
    # Second issue's worktree was never created.
    second_worktree = repo / ".worktrees" / second["identifier"]
    assert not second_worktree.exists()


def _write_fake_claude_script(tmp_path: Path) -> Path:
    """No-op stand-in for ``claude -p`` that exits cleanly without doing work.

    The point of the test is the orchestrator's response when a spawn
    *succeeds at the process level* but leaves the issue in its original
    state — the most pernicious failure mode, because there is no
    non-zero exit code to alert on.
    """
    script = tmp_path / "fake-claude.sh"
    script.write_text("#!/bin/sh\nexit 0\n")
    script.chmod(0o755)
    return script
