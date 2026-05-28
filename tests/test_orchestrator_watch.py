"""Watch mode: tmux pane lifecycle and watch log wiring.

Tests cover:
- watch log written for each issue when watch=True
- non-tmux path: log written, no tmux subprocess spawned, no crash
- tmux path: pane opened before session, prior pane killed before next issue,
  final pane left open
- tmux failure swallowed (drain continues)
"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

import pytest

from drain_cycle import linear, orchestrator, repos


def _issue(
    identifier: str,
    sort_order: float,
    *,
    repo_name: str = "test-repo",
) -> dict:
    return {
        "id": f"id-{identifier}",
        "identifier": identifier,
        "title": f"Title for {identifier}",
        "description": f"Body for {identifier}",
        "sortOrder": sort_order,
        "state": {"type": "unstarted", "name": "Todo"},
        "labels": [f"repo:{repo_name}"],
    }


def _init_repo(repo: Path) -> None:
    subprocess.run(["git", "init", "-b", "main"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=repo, check=True)
    (repo / "README.md").write_text("seed\n")
    subprocess.run(["git", "add", "."], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "seed"], cwd=repo, check=True, capture_output=True)


def _write_done_script(tmp_path: Path, done_marker: Path) -> Path:
    script = tmp_path / "fake-claude.sh"
    script.write_text(
        "#!/bin/sh\n"
        f'printf "%s\\n" "$(basename "$PWD")" >> "{done_marker}"\n'
    )
    script.chmod(0o755)
    return script


def _completed(marker: Path) -> set[str]:
    if not marker.exists():
        return set()
    return {line for line in marker.read_text().splitlines() if line}


def _setup_linear_stubs(monkeypatch: pytest.MonkeyPatch, raw_issues: list[dict], done_marker: Path) -> None:
    issues_by_id = {i["id"]: i for i in raw_issues}

    def fake_pending_issues(cycle_id: str):
        completed = _completed(done_marker)
        return linear._plan([i for i in raw_issues if i["identifier"] not in completed])

    def fake_get_issue(issue_id: str) -> dict:
        issue = issues_by_id[issue_id]
        if issue["identifier"] in _completed(done_marker):
            return {**issue, "state": {"type": "completed", "name": "Done"}}
        return issue

    monkeypatch.setattr(linear, "current_cycle_id", lambda: "stub-cycle-id")
    monkeypatch.setattr(linear, "pending_issues", fake_pending_issues)
    monkeypatch.setattr(linear, "get_issue", fake_get_issue)
    monkeypatch.setattr(linear, "set_state", lambda issue_id, state_name: None)


def test_watch_log_created_per_issue_when_watch_true(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When watch=True, a watch log file is created for each issue beside
    the run log, even without $TMUX set."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)
    monkeypatch.chdir(repo)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("TMUX", raising=False)

    done_marker = tmp_path / "done.txt"
    raw_issues = [_issue("ABA-W1", 1.0), _issue("ABA-W2", 2.0)]
    _setup_linear_stubs(monkeypatch, raw_issues, done_marker)
    fake_claude = _write_done_script(tmp_path, done_marker)
    monkeypatch.setattr(orchestrator, "_CLAUDE_CMD", [str(fake_claude)])

    exit_code = orchestrator.run(
        repos.Repos(mapping={"test-repo": repo}), watch=True
    )
    assert exit_code == 0

    runs_dir = tmp_path / ".drain-cycle" / "runs"
    watch_logs = list(runs_dir.glob("*.watch.log"))
    names = [p.name for p in watch_logs]
    # One watch log per issue, name contains the issue identifier.
    assert any("ABA-W1" in n for n in names)
    assert any("ABA-W2" in n for n in names)


def test_no_tmux_call_when_tmux_env_not_set(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When $TMUX is unset, no tmux subprocess is ever invoked."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)
    monkeypatch.chdir(repo)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("TMUX", raising=False)

    done_marker = tmp_path / "done.txt"
    raw_issues = [_issue("ABA-NT", 1.0)]
    _setup_linear_stubs(monkeypatch, raw_issues, done_marker)
    fake_claude = _write_done_script(tmp_path, done_marker)
    monkeypatch.setattr(orchestrator, "_CLAUDE_CMD", [str(fake_claude)])

    tmux_calls: list[list[str]] = []
    real_run = subprocess.run

    def capturing_run(args: Any, **kwargs: Any) -> Any:
        if isinstance(args, list) and args and args[0] == "tmux":
            tmux_calls.append(args)
        return real_run(args, **kwargs)

    monkeypatch.setattr(subprocess, "run", capturing_run)

    exit_code = orchestrator.run(
        repos.Repos(mapping={"test-repo": repo}), watch=True
    )
    assert exit_code == 0
    assert tmux_calls == [], f"unexpected tmux calls: {tmux_calls}"


def test_tmux_failure_does_not_crash_drain(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failing tmux split-window must be swallowed; the drain completes."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)
    monkeypatch.chdir(repo)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("TMUX", "/tmp/tmux-stub,1234,0")  # pretend we're in tmux

    done_marker = tmp_path / "done.txt"
    raw_issues = [_issue("ABA-TF", 1.0)]
    _setup_linear_stubs(monkeypatch, raw_issues, done_marker)
    fake_claude = _write_done_script(tmp_path, done_marker)
    monkeypatch.setattr(orchestrator, "_CLAUDE_CMD", [str(fake_claude)])

    # Make tmux always raise an error.
    real_run = subprocess.run

    def failing_tmux(args: Any, **kwargs: Any) -> Any:
        if isinstance(args, list) and args and args[0] == "tmux":
            raise FileNotFoundError("tmux not found")
        return real_run(args, **kwargs)

    monkeypatch.setattr(subprocess, "run", failing_tmux)

    exit_code = orchestrator.run(
        repos.Repos(mapping={"test-repo": repo}), watch=True
    )
    # Drain must succeed even though tmux failed.
    assert exit_code == 0


def test_tmux_pane_opened_and_prior_pane_killed_on_next_issue(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With $TMUX set and two issues: a pane is opened before each session;
    the first pane is killed before the second issue starts; the second pane
    is left open after the drain completes."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)
    monkeypatch.chdir(repo)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("TMUX", "/tmp/tmux-stub,1234,0")

    done_marker = tmp_path / "done.txt"
    raw_issues = [_issue("ABA-P1", 1.0), _issue("ABA-P2", 2.0)]
    _setup_linear_stubs(monkeypatch, raw_issues, done_marker)
    fake_claude = _write_done_script(tmp_path, done_marker)
    monkeypatch.setattr(orchestrator, "_CLAUDE_CMD", [str(fake_claude)])

    tmux_calls: list[list[str]] = []
    pane_id_counter = [0]
    real_run = subprocess.run

    def fake_tmux(args: Any, **kwargs: Any) -> Any:
        if not (isinstance(args, list) and args and args[0] == "tmux"):
            return real_run(args, **kwargs)
        tmux_calls.append(list(args))
        # Return a fake pane id for display-message calls.
        if len(args) > 1 and args[1] == "display-message":
            pane_id_counter[0] += 1
            mock = type("R", (), {"stdout": f"%{pane_id_counter[0]}\n", "returncode": 0})()
            return mock
        return type("R", (), {"returncode": 0})()

    monkeypatch.setattr(subprocess, "run", fake_tmux)

    exit_code = orchestrator.run(
        repos.Repos(mapping={"test-repo": repo}), watch=True
    )
    assert exit_code == 0

    split_calls = [c for c in tmux_calls if len(c) > 1 and c[1] == "split-window"]
    kill_calls = [c for c in tmux_calls if len(c) > 1 and c[1] == "kill-pane"]

    # Two issues → two split-window calls.
    assert len(split_calls) == 2
    # First pane killed before second issue starts.
    assert len(kill_calls) >= 1
    # Final pane NOT killed (only 1 kill for 2 splits).
    assert len(kill_calls) == 1


def test_watch_false_by_default_no_watch_logs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Default invocation (watch=False) writes no watch log files."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)
    monkeypatch.chdir(repo)
    monkeypatch.setenv("HOME", str(tmp_path))

    done_marker = tmp_path / "done.txt"
    raw_issues = [_issue("ABA-DEF", 1.0)]
    _setup_linear_stubs(monkeypatch, raw_issues, done_marker)
    fake_claude = _write_done_script(tmp_path, done_marker)
    monkeypatch.setattr(orchestrator, "_CLAUDE_CMD", [str(fake_claude)])

    exit_code = orchestrator.run(repos.Repos(mapping={"test-repo": repo}))
    assert exit_code == 0

    runs_dir = tmp_path / ".drain-cycle" / "runs"
    watch_logs = list(runs_dir.glob("*.watch.log"))
    assert watch_logs == []
