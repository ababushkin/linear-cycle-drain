"""Drain-the-cycle iteration test for the orchestrator (Task 3 / ABA-200).

Walking-skeleton (Task 1) covered one issue end-to-end. This test pins the
behaviour that *every* sorted Todo/Backlog issue is processed in order, each
in its own worktree, and that every worktree is removed when the spawned
session signals completion.

What we substitute and why:

* Linear: stubbed in-process. The orchestrator imports the linear module by
  attribute, so monkey-patching ``cycle_id`` / ``pending_issues`` / ``get_issue``
  on it is sufficient. We do **not** stub the GraphQL transport — the layer
  under test here is the orchestrator loop, not the wire format (the wire
  format is exercised by Task 6 / ABA-203).
* Spawned ``claude -p``: replaced with a real shell script via
  ``_CLAUDE_CMD``. The script writes the basename of its cwd (the issue
  identifier — that's how the orchestrator names worktrees) into a shared
  marker file. The stubbed ``get_issue`` reads that file to decide which
  issues are Done. This satisfies the spec's "no-op script that calls back
  into the stubbed Linear" — the callback path is the marker file rather
  than an in-process call, because the script runs in a separate process.
* Git: a real ``git init`` repo with one commit on ``main``. ``git worktree``
  is exercised for real; this is the cheapest way to be sure the orchestrator
  doesn't paper over a worktree problem.
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
    """Create a real git repo with one commit on ``main`` for worktree tests."""
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


def test_orchestrator_drains_every_issue_in_sorted_order(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)
    monkeypatch.chdir(repo)
    monkeypatch.setenv("HOME", str(tmp_path))

    # Three issues — chosen so a "naïve" iteration order (input order) and
    # the priority sort disagree, proving the loop consumes the *sorted*
    # list rather than the unsorted one.
    raw_issues = [
        _issue("ABA-X", priority=3, sort_order=2.0),  # Medium
        _issue("ABA-Y", priority=1, sort_order=1.0),  # Urgent — runs first
        _issue("ABA-Z", priority=2, sort_order=3.0),  # High
    ]
    sorted_issues = linear._sort_pending_issues(raw_issues)
    expected_order = [i["identifier"] for i in sorted_issues]
    assert expected_order == ["ABA-Y", "ABA-Z", "ABA-X"]

    issues_by_id = {i["id"]: i for i in raw_issues}
    done_marker = tmp_path / "done-identifiers.txt"

    def fake_current_cycle_id() -> str:
        return "stub-cycle"

    def fake_pending_issues(cycle_id: str) -> list[dict]:
        # Re-sort each call (mirrors the real client, which sorts the wire
        # response) and exclude anything the fake claude script has already
        # marked Done — so an accidental re-fetch can't re-feed the same
        # issue back into the loop.
        completed = _completed_identifiers(done_marker)
        return linear._sort_pending_issues(
            [i for i in raw_issues if i["identifier"] not in completed]
        )

    def fake_get_issue(issue_id: str) -> dict:
        issue = issues_by_id[issue_id]
        if issue["identifier"] in _completed_identifiers(done_marker):
            return {**issue, "state": {"type": "completed", "name": "Done"}}
        return issue

    monkeypatch.setattr(linear, "current_cycle_id", fake_current_cycle_id)
    monkeypatch.setattr(linear, "pending_issues", fake_pending_issues)
    monkeypatch.setattr(linear, "get_issue", fake_get_issue)
    # set_state is exercised by tests/test_orchestrator_set_state.py; here it's
    # a no-op so this test stays focused on iteration order + worktree cleanup.
    monkeypatch.setattr(linear, "set_state", lambda issue_id, state_name: None)

    fake_claude = _write_fake_claude_script(tmp_path, done_marker)
    monkeypatch.setattr(orchestrator, "_CLAUDE_CMD", [str(fake_claude)])

    exit_code = orchestrator.run()

    assert exit_code == 0
    # The script appends the worktree-basename (== issue identifier) on each
    # run, so the file contents are the exact processing order.
    assert done_marker.read_text().splitlines() == expected_order
    # Every worktree was removed on success.
    worktrees_dir = repo / ".worktrees"
    assert not worktrees_dir.exists() or not any(worktrees_dir.iterdir())
    # ``git worktree list`` should show only the main checkout.
    listed = subprocess.run(
        ["git", "worktree", "list", "--porcelain"],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    assert listed.count("worktree ") == 1


def _completed_identifiers(marker: Path) -> set[str]:
    if not marker.exists():
        return set()
    return {line for line in marker.read_text().splitlines() if line}


def _write_fake_claude_script(tmp_path: Path, done_marker: Path) -> Path:
    """A no-op stand-in for ``claude -p``.

    Records the basename of its cwd (the issue identifier — orchestrator
    names worktrees ``.worktrees/<identifier>/``) into ``done_marker``.
    The stubbed ``get_issue`` reads this file to learn which issues the
    'agent' has completed.
    """
    script = tmp_path / "fake-claude.sh"
    script.write_text(
        "#!/bin/sh\n"
        f'printf "%s\\n" "$(basename "$PWD")" >> "{done_marker}"\n'
    )
    script.chmod(0o755)
    return script
