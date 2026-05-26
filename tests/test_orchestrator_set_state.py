"""Orchestrator In-Progress-before-spawn test.

If a spawned agent moves its issue `Todo → Done` directly, it leaves
`startedAt: null` in Linear and the issue's history never shows
"In Progress → Done". This test pins the fix: before spawning ``claude -p``
for an issue, the orchestrator must call
``linear.set_state(issue_id, "In Progress")``, and it must do so *before*
the spawn — not after, not during, not skipped.

Substitution choices mirror ``test_orchestrator_iteration.py``: real git
repo, in-process Linear stub via attribute monkey-patching, fake ``claude``
shell script as ``_CLAUDE_CMD``. The difference is what the marker file
records: each ``set_state`` stub call appends ``set-state:<identifier>`` and
each fake-claude invocation appends ``spawn:<identifier>``, so the file
contents are the exact interleaving — proving the transition lands before
the spawn for every issue.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

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
    subprocess.run(
        ["git", "config", "user.email", "test@example.com"], cwd=repo, check=True
    )
    subprocess.run(["git", "config", "user.name", "Test"], cwd=repo, check=True)
    (repo / "README.md").write_text("seed\n")
    subprocess.run(["git", "add", "."], cwd=repo, check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-m", "seed"], cwd=repo, check=True, capture_output=True
    )


def test_orchestrator_transitions_to_in_progress_before_each_spawn(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)
    monkeypatch.chdir(repo)
    monkeypatch.setenv("HOME", str(tmp_path))

    first = _issue("ABA-FIRST", sort_order=1.0)
    second = _issue("ABA-SECOND", sort_order=2.0)
    raw_issues = [first, second]
    issues_by_id = {i["id"]: i for i in raw_issues}

    trace = tmp_path / "trace.txt"
    set_state_calls: list[tuple[str, str]] = []

    def fake_current_cycle_id() -> str:
        return "stub-cycle"

    def fake_pending_issues(cycle_id: str):
        completed = _completed_identifiers(trace)
        return linear._plan(
            [i for i in raw_issues if i["identifier"] not in completed]
        )

    def fake_set_state(issue_id: str, state_name: str) -> None:
        set_state_calls.append((issue_id, state_name))
        identifier = issues_by_id[issue_id]["identifier"]
        with trace.open("a") as f:
            f.write(f"set-state:{identifier}:{state_name}\n")

    def fake_get_issue(issue_id: str) -> dict:
        issue = issues_by_id[issue_id]
        if issue["identifier"] in _completed_identifiers(trace):
            return {**issue, "state": {"type": "completed", "name": "Done"}}
        return issue

    monkeypatch.setattr(linear, "current_cycle_id", fake_current_cycle_id)
    monkeypatch.setattr(linear, "pending_issues", fake_pending_issues)
    monkeypatch.setattr(linear, "set_state", fake_set_state)
    monkeypatch.setattr(linear, "get_issue", fake_get_issue)

    fake_claude = _write_fake_claude_script(tmp_path, trace)
    monkeypatch.setattr(orchestrator, "_CLAUDE_CMD", [str(fake_claude)])

    exit_code = orchestrator.run(repos.Repos(mapping={"test-repo": repo}))

    assert exit_code == 0
    # Every picked issue had set_state called with "In Progress", in pick order.
    assert set_state_calls == [
        (first["id"], "In Progress"),
        (second["id"], "In Progress"),
    ]
    # Trace file shows the full interleaving — set-state must precede spawn
    # for *each* issue (not just globally), so a future refactor that batches
    # all set_state calls up front and then spawns is still flagged here.
    lines = trace.read_text().splitlines()
    assert lines == [
        f"set-state:{first['identifier']}:In Progress",
        f"spawn:{first['identifier']}",
        f"set-state:{second['identifier']}:In Progress",
        f"spawn:{second['identifier']}",
    ]
    # Both worktrees removed on success.
    worktrees_dir = repo / ".worktrees"
    assert not worktrees_dir.exists() or not any(worktrees_dir.iterdir())


def _completed_identifiers(trace: Path) -> set[str]:
    if not trace.exists():
        return set()
    completed = set()
    for line in trace.read_text().splitlines():
        if line.startswith("spawn:"):
            completed.add(line.split(":", 1)[1])
    return completed


def _write_fake_claude_script(tmp_path: Path, trace: Path) -> Path:
    """Fake ``claude -p`` that records its identifier into the shared trace.

    Writing the trace line *and* the identifier-as-completion signal in one
    place keeps the stubbed Linear in sync: the next ``pending_issues`` call
    sees the issue as completed and drops it from the queue, and the
    ``get_issue`` re-fetch returns Done — same pattern as
    ``test_orchestrator_iteration.py``.
    """
    script = tmp_path / "fake-claude.sh"
    script.write_text(
        "#!/bin/sh\n"
        f'printf "spawn:%s\\n" "$(basename "$PWD")" >> "{trace}"\n'
    )
    script.chmod(0o755)
    return script
