"""Worktree teardown guard test for the orchestrator.

Pins that a worktree.remove failure on the Done path is caught, recorded in
the run-log entry's halt_reason, warned on stderr, and does not crash the
drain. The issue is already Done in Linear; the cycle continues to completion.
"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from drain_cycle import linear, orchestrator, repos, worktree


_TEST_REPO_NAME = "test-repo"


def _issue(
    identifier: str,
    priority: int,
    sort_order: float,
    *,
    repo_name: str = _TEST_REPO_NAME,
) -> dict:
    return {
        "id": f"id-{identifier}",
        "identifier": identifier,
        "title": f"Title for {identifier}",
        "description": f"Body for {identifier}",
        "priority": priority,
        "sortOrder": sort_order,
        "state": {"type": "unstarted", "name": "Todo"},
        "labels": [f"repo:{repo_name}"],
    }


def _stub_repos(repo_path: Path) -> repos.Repos:
    return repos.Repos(mapping={_TEST_REPO_NAME: repo_path})


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


def test_orchestrator_continues_and_records_teardown_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A RuntimeError from worktree.remove on the Done path must not crash the
    drain. The failure must be recorded in the run-log entry's halt_reason and a
    warning must appear on stderr. Since the issue is Done in Linear, the drain
    continues — exiting 0 when all issues are accounted for."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)
    monkeypatch.chdir(repo)
    monkeypatch.setenv("HOME", str(tmp_path))

    iss = _issue("ABA-FIRST", priority=1, sort_order=1.0)
    raw_issues = [iss]
    issues_by_id = {iss["id"]: iss}
    done_marker = tmp_path / "done-identifiers.txt"

    def fake_pending_issues(cycle_id: str) -> list[dict]:
        if iss["identifier"] in _completed_identifiers(done_marker):
            return []
        return [iss]

    def fake_get_issue(issue_id: str) -> dict:
        i = issues_by_id[issue_id]
        if i["identifier"] in _completed_identifiers(done_marker):
            return {**i, "state": {"type": "completed", "name": "Done"}}
        return i

    monkeypatch.setattr(linear, "current_cycle_id", lambda: "stub-cycle-id")
    monkeypatch.setattr(linear, "pending_issues", fake_pending_issues)
    monkeypatch.setattr(linear, "get_issue", fake_get_issue)
    monkeypatch.setattr(linear, "set_state", lambda issue_id, state_name: None)

    teardown_msg = "git worktree remove failed (exit 128): worktree is dirty"

    def failing_remove(repo_path: Path, wt_path: Path) -> None:
        raise RuntimeError(teardown_msg)

    monkeypatch.setattr(worktree, "remove", failing_remove)

    fake_claude = _write_fake_claude_script(tmp_path, done_marker)
    monkeypatch.setattr(orchestrator, "_CLAUDE_CMD", [str(fake_claude)])

    exit_code = orchestrator.run(_stub_repos(repo))

    # The issue is Done — teardown failure must not halt the drain.
    assert exit_code == 0

    # The failure is recorded in the run-log entry's halt_reason.
    runs_dir = tmp_path / ".drain-cycle" / "runs"
    payload = json.loads(next(runs_dir.glob("stub-cycle-id-*.json")).read_text())
    assert len(payload["entries"]) == 1
    entry = payload["entries"][0]
    assert entry["final_linear_state"] == "Done"
    assert entry["halt_reason"] is not None
    assert teardown_msg in entry["halt_reason"]

    # A warning lands on stderr; no "Halt: " line (drain didn't halt).
    stderr_lines = capsys.readouterr().err.splitlines()
    assert not any(line.startswith("Halt: ") for line in stderr_lines)
    warning_lines = [line for line in stderr_lines if "teardown failed" in line]
    assert len(warning_lines) == 1
    assert teardown_msg in warning_lines[0]
    assert iss["identifier"] in warning_lines[0]


def _completed_identifiers(marker: Path) -> set[str]:
    if not marker.exists():
        return set()
    return {line for line in marker.read_text().splitlines() if line}


def _write_fake_claude_script(tmp_path: Path, done_marker: Path) -> Path:
    script = tmp_path / "fake-claude.sh"
    script.write_text(
        "#!/bin/sh\n"
        f'printf "%s\\n" "$(basename "$PWD")" >> "{done_marker}"\n'
    )
    script.chmod(0o755)
    return script
