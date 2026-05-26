"""Active-run marker lifecycle tests for the orchestrator.

Pins that the orchestrator writes ``~/.drain-cycle/active.json`` just before
spawning a worker and removes it in every exit path (Done, not-Done halt,
breach, pre-spawn error). A marker left behind after a normal exit would make
``drain-cycle status`` lie about a live run.
"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from drain_cycle import limits, linear, orchestrator, progress, repos


_TEST_REPO_NAME = "test-repo"


def _issue(identifier: str, sort_order: float) -> dict:
    return {
        "id": f"id-{identifier}",
        "identifier": identifier,
        "title": f"Title for {identifier}",
        "description": f"Body for {identifier}",
        "sortOrder": sort_order,
        "state": {"type": "unstarted", "name": "Todo"},
        "labels": [f"repo:{_TEST_REPO_NAME}"],
    }


def _stub_repos(repo_path: Path) -> repos.Repos:
    return repos.Repos(mapping={_TEST_REPO_NAME: repo_path})


def _init_repo(repo: Path) -> None:
    subprocess.run(["git", "init", "-b", "main"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=repo, check=True)
    (repo / "README.md").write_text("seed\n")
    subprocess.run(["git", "add", "."], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "seed"], cwd=repo, check=True, capture_output=True)


def _write_done_script(tmp_path: Path, done_marker: Path, identifier: str) -> Path:
    """Script that signals completion by writing the identifier to a marker file."""
    script = tmp_path / "fake-claude-done.sh"
    script.write_text(f'#!/bin/sh\necho "{identifier}" >> "{done_marker}"\nexit 0\n')
    script.chmod(0o755)
    return script


def _write_noop_script(tmp_path: Path) -> Path:
    """Script that exits cleanly without doing any work."""
    script = tmp_path / "fake-claude-noop.sh"
    script.write_text("#!/bin/sh\nexit 0\n")
    script.chmod(0o755)
    return script


def _stub_linear_done(monkeypatch, raw_issues: list[dict], done_marker: Path) -> None:
    issues_by_id = {i["id"]: i for i in raw_issues}
    done_state = {"type": "completed", "name": "Done"}

    def fake_current_cycle_id() -> str:
        return "stub-cycle"

    def fake_pending_issues(cycle_id: str) -> list[dict]:
        return linear._plan(raw_issues)

    def fake_get_issue(issue_id: str) -> dict:
        completed = done_marker.read_text().splitlines() if done_marker.exists() else []
        issue = dict(issues_by_id[issue_id])
        if issue["identifier"] in completed:
            issue = {**issue, "state": done_state}
        return issue

    monkeypatch.setattr(linear, "current_cycle_id", fake_current_cycle_id)
    monkeypatch.setattr(linear, "pending_issues", fake_pending_issues)
    monkeypatch.setattr(linear, "get_issue", fake_get_issue)
    monkeypatch.setattr(linear, "set_state", lambda issue_id, state_name: None)


def _stub_linear_noop(monkeypatch, raw_issues: list[dict]) -> None:
    issues_by_id = {i["id"]: i for i in raw_issues}

    def fake_current_cycle_id() -> str:
        return "stub-cycle"

    def fake_pending_issues(cycle_id: str) -> list[dict]:
        return linear._plan(raw_issues)

    def fake_get_issue(issue_id: str) -> dict:
        return issues_by_id[issue_id]

    monkeypatch.setattr(linear, "current_cycle_id", fake_current_cycle_id)
    monkeypatch.setattr(linear, "pending_issues", fake_pending_issues)
    monkeypatch.setattr(linear, "get_issue", fake_get_issue)
    monkeypatch.setattr(linear, "set_state", lambda issue_id, state_name: None)


def test_marker_written_before_spawn_and_cleared_after_done(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Marker exists during spawn; is absent after a Done issue."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)
    monkeypatch.chdir(repo)
    monkeypatch.setenv("HOME", str(tmp_path))

    issue = _issue("ABA-1", sort_order=1.0)
    done_marker = tmp_path / "done.txt"
    _stub_linear_done(monkeypatch, [issue], done_marker)

    # Intercept worker.run_issue to capture the marker state at call time.
    marker_at_spawn: dict | None = None
    real_run_issue = orchestrator.worker.run_issue

    def spy_run_issue(**kwargs):
        nonlocal marker_at_spawn
        marker_at_spawn = progress.read()
        # Write done marker so the fake get_issue returns Done.
        done_marker.write_text("ABA-1\n")
        return real_run_issue(**kwargs)

    monkeypatch.setattr(orchestrator.worker, "run_issue", spy_run_issue)
    noop_script = _write_noop_script(tmp_path)
    monkeypatch.setattr(orchestrator, "_CLAUDE_CMD", [str(noop_script)])

    orchestrator.run(_stub_repos(repo))

    # Marker was present at spawn time.
    assert marker_at_spawn is not None
    assert marker_at_spawn["issue"]["identifier"] == "ABA-1"
    assert marker_at_spawn["index"] == 1
    assert marker_at_spawn["total"] == 1

    # Marker is gone after the run.
    assert progress.read() is None


def test_marker_cleared_after_not_done_halt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Marker is removed even when the worker leaves the issue in a non-Done state."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)
    monkeypatch.chdir(repo)
    monkeypatch.setenv("HOME", str(tmp_path))

    issue = _issue("ABA-HALT", sort_order=1.0)
    _stub_linear_noop(monkeypatch, [issue])

    noop_script = _write_noop_script(tmp_path)
    monkeypatch.setattr(orchestrator, "_CLAUDE_CMD", [str(noop_script)])

    rc = orchestrator.run(_stub_repos(repo))
    assert rc != 0
    assert progress.read() is None


def test_marker_not_written_on_pre_spawn_resolution_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No marker is written when the issue can't be mapped to a repo."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)
    monkeypatch.chdir(repo)
    monkeypatch.setenv("HOME", str(tmp_path))

    # Issue references a repo name that isn't in our repos mapping.
    issue = {
        "id": "id-ABA-X",
        "identifier": "ABA-X",
        "title": "Unresolvable",
        "description": "",
        "sortOrder": 1.0,
        "state": {"type": "unstarted", "name": "Todo"},
        "labels": ["repo:nonexistent-repo"],
    }
    monkeypatch.setattr(linear, "current_cycle_id", lambda: "c")
    monkeypatch.setattr(
        linear, "pending_issues", lambda _: linear._plan([issue])
    )
    monkeypatch.setattr(linear, "set_state", lambda *_: None)

    rc = orchestrator.run(_stub_repos(repo))
    assert rc != 0
    assert progress.read() is None


def test_marker_index_and_total_correct_for_multiple_issues(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``index`` increments per issue; ``total`` is the full cycle count."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)
    monkeypatch.chdir(repo)
    monkeypatch.setenv("HOME", str(tmp_path))

    issues = [
        _issue("ABA-1", sort_order=1.0),
        _issue("ABA-2", sort_order=2.0),
    ]
    done_marker = tmp_path / "done.txt"
    _stub_linear_done(monkeypatch, issues, done_marker)

    captured: list[dict] = []
    real_run_issue = orchestrator.worker.run_issue

    def spy_run_issue(**kwargs):
        m = progress.read()
        if m:
            captured.append({"index": m["index"], "total": m["total"]})
        done_marker.write_text("\n".join(i["identifier"] for i in issues) + "\n")
        return real_run_issue(**kwargs)

    monkeypatch.setattr(orchestrator.worker, "run_issue", spy_run_issue)
    noop_script = _write_noop_script(tmp_path)
    monkeypatch.setattr(orchestrator, "_CLAUDE_CMD", [str(noop_script)])

    rc = orchestrator.run(_stub_repos(repo))
    assert rc == 0

    assert len(captured) == 2
    assert captured[0] == {"index": 1, "total": 2}
    assert captured[1] == {"index": 2, "total": 2}
    assert progress.read() is None


def test_marker_progress_updated_via_on_progress_callback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The on_progress callback writes progress into the marker file."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)
    monkeypatch.chdir(repo)
    monkeypatch.setenv("HOME", str(tmp_path))

    issue = _issue("ABA-PROG", sort_order=1.0)
    done_marker = tmp_path / "done.txt"
    _stub_linear_done(monkeypatch, [issue], done_marker)

    progress_snapshots: list[dict] = []
    real_run_issue = orchestrator.worker.run_issue

    def capturing_run_issue(**kwargs):
        on_progress = kwargs.get("on_progress")
        if on_progress is not None:
            # Simulate two turn events.
            on_progress(1, 1000, 500, None, 5.0)
            on_progress(2, 2000, 800, 0.5, 10.0)
            snap = progress.read()
            if snap:
                progress_snapshots.append(snap["progress"])
        done_marker.write_text("ABA-PROG\n")
        return real_run_issue(
            claude_cmd=kwargs["claude_cmd"],
            model=kwargs["model"],
            prompt=kwargs["prompt"],
            cwd=kwargs["cwd"],
            token_limit=kwargs["token_limit"],
            time_limit_seconds=kwargs["time_limit_seconds"],
            cost_limit_usd=kwargs["cost_limit_usd"],
        )

    monkeypatch.setattr(orchestrator.worker, "run_issue", capturing_run_issue)
    noop_script = _write_noop_script(tmp_path)
    monkeypatch.setattr(orchestrator, "_CLAUDE_CMD", [str(noop_script)])

    orchestrator.run(_stub_repos(repo))

    assert len(progress_snapshots) == 1
    snap = progress_snapshots[0]
    assert snap["turns"] == 2
    assert snap["cumulative_tokens"] == 2000
    assert snap["cost_usd"] == 0.5
    assert snap["elapsed_seconds"] == pytest.approx(10.0)
