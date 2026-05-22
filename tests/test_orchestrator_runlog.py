"""Orchestrator-writes-runlog integration test (Task 1 / ABA-215).

Pins that ``orchestrator.run()`` produces the on-disk artefact US-C / ABA-196
specifies — one entry per attempted issue, in pick order, with all six
required fields populated correctly on the happy path, and the top-level
``cycle_duration_seconds`` derived from the spanned timestamps.

Substitution choices mirror ``test_orchestrator_iteration.py``: real git
repo, in-process Linear stub via attribute monkey-patching, fake ``claude``
shell script as ``_CLAUDE_CMD``. The differences are (a) ``HOME`` is
monkeypatched so the runlog lands under ``tmp_path`` and (b) we assert
the file shape rather than the worktree-cleanup side-effects (covered by
the iteration test).
"""
from __future__ import annotations

import json
import subprocess
from datetime import datetime
from pathlib import Path

import pytest

from drain_cycle import linear, orchestrator, repos


def _issue(
    identifier: str,
    priority: int,
    sort_order: float,
    *,
    repo_name: str = "test-repo",
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


def test_orchestrator_writes_runlog_with_one_entry_per_successful_issue(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)
    monkeypatch.chdir(repo)
    monkeypatch.setenv("HOME", str(tmp_path))

    first = _issue("ABA-FIRST", priority=1, sort_order=1.0)
    second = _issue("ABA-SECOND", priority=2, sort_order=2.0)
    raw_issues = [first, second]
    expected_order = [i["identifier"] for i in raw_issues]
    issues_by_id = {i["id"]: i for i in raw_issues}
    done_marker = tmp_path / "done-identifiers.txt"

    def fake_current_cycle_id() -> str:
        return "stub-cycle-id"

    def fake_pending_issues(cycle_id: str) -> list[dict]:
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
    monkeypatch.setattr(linear, "set_state", lambda issue_id, state_name: None)

    fake_claude = _write_fake_claude_script(tmp_path, done_marker)
    monkeypatch.setattr(orchestrator, "_CLAUDE_CMD", [str(fake_claude)])

    exit_code = orchestrator.run(repos.Repos(mapping={"test-repo": repo}))
    assert exit_code == 0

    # Per-run filename (ABA-230): one file per drain-cycle invocation,
    # ``<cycle-id>-<run-timestamp>.json`` — glob to locate it.
    runs_dir = tmp_path / ".drain-cycle" / "runs"
    log_files = list(runs_dir.glob("stub-cycle-id-*.json"))
    assert len(log_files) == 1
    log_path = log_files[0]
    payload = json.loads(log_path.read_text())

    assert payload["cycle_id"] == "stub-cycle-id"
    assert isinstance(payload["entries"], list)
    assert len(payload["entries"]) == len(raw_issues)

    # cycle_duration_seconds == max(finished_at) − min(started_at) over entries.
    starts = [datetime.fromisoformat(e["started_at"]) for e in payload["entries"]]
    finishes = [datetime.fromisoformat(e["finished_at"]) for e in payload["entries"]]
    expected_duration = (max(finishes) - min(starts)).total_seconds()
    assert payload["cycle_duration_seconds"] == pytest.approx(expected_duration)
    assert payload["cycle_duration_seconds"] >= 0.0

    # Entries are in pick order.
    assert [e["issue_identifier"] for e in payload["entries"]] == expected_order

    required_keys = {
        "issue_identifier",
        "started_at",
        "finished_at",
        "exit_code",
        "final_linear_state",
        "worktree_path",
        "halt_reason",
    }
    for entry in payload["entries"]:
        assert set(entry.keys()) == required_keys
        assert entry["final_linear_state"] == "Done"
        assert entry["exit_code"] == 0
        # Done entries carry halt_reason=null (ABA-213): the orchestrator
        # only populates it on the halt branch.
        assert entry["halt_reason"] is None
        # ISO 8601 round-trip — fromisoformat raises on garbage.
        start = datetime.fromisoformat(entry["started_at"])
        finish = datetime.fromisoformat(entry["finished_at"])
        assert start <= finish
        # Worktree path is recorded as the absolute path the orchestrator used.
        assert entry["worktree_path"] == str(repo / ".worktrees" / entry["issue_identifier"])


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
