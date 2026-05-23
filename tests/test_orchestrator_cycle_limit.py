"""Cycle-wide circuit-breaker test for the orchestrator.

The per-issue caps (exercised in ``test_worker.py``) cannot catch
death-by-aggregate: every issue stays under its own token cap while their
sum drains the quota. This test pins that path — each issue completes Done
under generous (here: disabled) per-issue caps, but once the *cycle* token
total crosses ``cycle_tokens`` the orchestrator stops, records the reason in
the top-level ``cycle_halt_reason``, and leaves the remaining issue
untouched.

Substitution choices mirror ``test_orchestrator_iteration.py``: a real git
repo, an in-process Linear stub via attribute monkey-patching, and a fake
``claude`` shell script as ``_CLAUDE_CMD``. The difference is the script: it
emits a real ``stream-json`` ``assistant`` event carrying a fixed token
count (so the worker's usage parser accumulates a known per-issue total)
before marking the issue Done.
"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from drain_cycle import limits, linear, orchestrator, repos

_TEST_REPO_NAME = "test-repo"
_TOKENS_PER_ISSUE = 6_000_000


def _issue(identifier: str, priority: int, sort_order: float) -> dict:
    return {
        "id": f"id-{identifier}",
        "identifier": identifier,
        "title": f"Title for {identifier}",
        "description": f"Body for {identifier}",
        "priority": priority,
        "sortOrder": sort_order,
        "state": {"type": "unstarted", "name": "Todo"},
        "labels": [f"repo:{_TEST_REPO_NAME}"],
    }


def _init_repo(repo: Path) -> None:
    subprocess.run(["git", "init", "-b", "main"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=repo, check=True)
    (repo / "README.md").write_text("seed\n")
    subprocess.run(["git", "add", "."], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "seed"], cwd=repo, check=True, capture_output=True)


def _completed(marker: Path) -> set[str]:
    if not marker.exists():
        return set()
    return {line for line in marker.read_text().splitlines() if line}


def _write_token_emitting_claude(tmp_path: Path, done_marker: Path) -> Path:
    """A ``claude -p`` stand-in: emit one assistant turn worth
    ``_TOKENS_PER_ISSUE`` cumulative tokens, then mark the issue Done."""
    event = json.dumps(
        {
            "type": "assistant",
            "message": {
                "id": "m1",
                "usage": {
                    "input_tokens": _TOKENS_PER_ISSUE,
                    "output_tokens": 0,
                    "cache_creation_input_tokens": 0,
                    "cache_read_input_tokens": 0,
                },
            },
        }
    )
    stream_file = tmp_path / "stream.jsonl"
    stream_file.write_text(event + "\n")
    script = tmp_path / "token-claude.sh"
    script.write_text(
        "#!/bin/sh\n"
        f'cat "{stream_file}"\n'
        f'printf "%s\\n" "$(basename "$PWD")" >> "{done_marker}"\n'
    )
    script.chmod(0o755)
    return script


def test_orchestrator_stops_cycle_when_cycle_token_cap_breached(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)
    monkeypatch.chdir(repo)
    monkeypatch.setenv("HOME", str(tmp_path))

    # Three issues, each Done at 6M tokens. Cap the cycle at 10M: A leaves
    # the total at 6M (continue), B at 12M (breach) — C is never attempted.
    a = _issue("ABA-A", priority=1, sort_order=1.0)
    b = _issue("ABA-B", priority=2, sort_order=2.0)
    c = _issue("ABA-C", priority=3, sort_order=3.0)
    raw_issues = [a, b, c]
    issues_by_id = {i["id"]: i for i in raw_issues}
    done_marker = tmp_path / "done.txt"

    def fake_get_issue(issue_id: str) -> dict:
        issue = issues_by_id[issue_id]
        if issue["identifier"] in _completed(done_marker):
            return {**issue, "state": {"type": "completed", "name": "Done"}}
        return issue

    monkeypatch.setattr(linear, "current_cycle_id", lambda: "stub-cycle-id")
    monkeypatch.setattr(
        linear, "pending_issues", lambda cycle_id: linear._sort_pending_issues(raw_issues)
    )
    monkeypatch.setattr(linear, "get_issue", fake_get_issue)
    monkeypatch.setattr(linear, "set_state", lambda issue_id, state_name: None)

    fake_claude = _write_token_emitting_claude(tmp_path, done_marker)
    monkeypatch.setattr(orchestrator, "_CLAUDE_CMD", [str(fake_claude)])

    # Per-issue caps off so each issue completes; only the cycle-token cap is
    # live, isolating the death-by-aggregate path.
    lim = limits.Limits(
        per_issue_tokens=None,
        per_issue_seconds=None,
        per_issue_cost_usd=None,
        cycle_tokens=10_000_000,
        cycle_cost_usd=None,
        cycle_seconds=None,
    )

    exit_code = orchestrator.run(repos.Repos(mapping={_TEST_REPO_NAME: repo}), lim)
    assert exit_code == 1

    payload = json.loads(
        next((tmp_path / ".drain-cycle" / "runs").glob("stub-cycle-id-*.json")).read_text()
    )
    # A and B ran (both Done); C was never attempted.
    identifiers = [e["issue_identifier"] for e in payload["entries"]]
    assert identifiers == ["ABA-A", "ABA-B"]
    assert all(e["final_linear_state"] == "Done" for e in payload["entries"])
    assert payload["cycle_tokens_cumulative"] == 2 * _TOKENS_PER_ISSUE

    # The cycle stop is recorded at the top level (not in any entry's
    # halt_reason — both issues finished cleanly), and matches the stderr line.
    assert payload["cycle_halt_reason"] is not None
    assert "cycle token cap exceeded" in payload["cycle_halt_reason"]
    stderr_lines = capsys.readouterr().err.splitlines()
    (halt_line,) = [line for line in stderr_lines if line.startswith("Halt: ")]
    assert payload["cycle_halt_reason"] == halt_line
    assert all(e["halt_reason"] is None for e in payload["entries"])

    # C's worktree was never created.
    assert not (repo / ".worktrees" / "ABA-C").exists()
