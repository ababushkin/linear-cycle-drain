"""Orchestrator-writes-runlog integration test.

Pins that ``orchestrator.run()`` produces the on-disk run-log artefact
— one entry per attempted issue, in pick order, with all six
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


def test_orchestrator_writes_runlog_with_one_entry_per_successful_issue(
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
    expected_order = [i["identifier"] for i in raw_issues]
    issues_by_id = {i["id"]: i for i in raw_issues}
    done_marker = tmp_path / "done-identifiers.txt"

    def fake_current_cycle_id() -> str:
        return "stub-cycle-id"

    def fake_pending_issues(cycle_id: str):
        completed = _completed_identifiers(done_marker)
        return linear._plan(
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

    # Per-run filename: one file per drain-cycle invocation,
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
        "duration_seconds",
        "model",
        "usage",
        "cost_usd",
        "num_turns",
        "session_id",
        "is_error",
    }
    for entry in payload["entries"]:
        assert set(entry.keys()) == required_keys
        assert entry["final_linear_state"] == "Done"
        assert entry["exit_code"] == 0
        # Done entries carry halt_reason=null: the orchestrator
        # only populates it on the halt branch.
        assert entry["halt_reason"] is None
        # ISO 8601 round-trip — fromisoformat raises on garbage.
        start = datetime.fromisoformat(entry["started_at"])
        finish = datetime.fromisoformat(entry["finished_at"])
        assert start <= finish
        # Worktree path is recorded as the absolute path the orchestrator used.
        assert entry["worktree_path"] == str(repo / ".worktrees" / entry["issue_identifier"])


def test_runlog_done_entry_carries_worker_usage_from_stream(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """End-to-end: a Done issue's run-log entry carries the spawned
    session's model, cumulative + peak-context tokens, cost_usd, num_turns
    and session_id — parsed from the worker's stream-json output — and the
    per-invocation aggregates roll those up."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)
    monkeypatch.chdir(repo)
    monkeypatch.setenv("HOME", str(tmp_path))

    only = _issue("ABA-ONE", sort_order=1.0)
    raw_issues = [only]
    issues_by_id = {i["id"]: i for i in raw_issues}
    done_marker = tmp_path / "done-identifiers.txt"

    def fake_pending_issues(cycle_id: str):
        completed = _completed_identifiers(done_marker)
        return linear._plan(
            [i for i in raw_issues if i["identifier"] not in completed]
        )

    def fake_get_issue(issue_id: str) -> dict:
        issue = issues_by_id[issue_id]
        if issue["identifier"] in _completed_identifiers(done_marker):
            return {**issue, "state": {"type": "completed", "name": "Done"}}
        return issue

    monkeypatch.setattr(linear, "current_cycle_id", lambda: "stub-cycle-id")
    monkeypatch.setattr(linear, "pending_issues", fake_pending_issues)
    monkeypatch.setattr(linear, "get_issue", fake_get_issue)
    monkeypatch.setattr(linear, "set_state", lambda issue_id, state_name: None)

    fake_claude = _write_streaming_claude_script(tmp_path, done_marker)
    monkeypatch.setattr(orchestrator, "_CLAUDE_CMD", [str(fake_claude)])

    exit_code = orchestrator.run(repos.Repos(mapping={"test-repo": repo}))
    assert exit_code == 0

    runs_dir = tmp_path / ".drain-cycle" / "runs"
    payload = json.loads(next(runs_dir.glob("stub-cycle-id-*.json")).read_text())
    (entry,) = payload["entries"]

    # Default model (no model: label) reaches the run log.
    assert entry["model"] == "claude-sonnet-4-6"
    # Two turns summed (msg_a 10/4/100/0, msg_b 5/20/0/300) → cumulative 439.
    assert entry["usage"]["cumulative"] == 439
    assert entry["usage"]["peak_context"] == 305
    # Session summary from the canned result event.
    assert entry["cost_usd"] == 0.42
    assert entry["num_turns"] == 2
    assert entry["session_id"] == "sess-1"
    assert entry["is_error"] is False

    # Per-invocation aggregates roll up the single entry.
    assert payload["cycle_cost_usd"] == 0.42
    assert payload["cycle_tokens_cumulative"] == 439


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


def _write_streaming_claude_script(tmp_path: Path, done_marker: Path) -> Path:
    """A ``claude -p`` stand-in that marks the issue Done *and* emits a
    canned stream-json sequence (two turns + a result) to stdout, so the
    worker has real usage to parse."""
    stream_lines = [
        json.dumps({"type": "system", "subtype": "init", "session_id": "sess-1"}),
        json.dumps(
            {
                "type": "assistant",
                "message": {
                    "id": "msg_a",
                    "usage": {
                        "input_tokens": 10,
                        "output_tokens": 4,
                        "cache_creation_input_tokens": 100,
                        "cache_read_input_tokens": 0,
                    },
                },
            }
        ),
        json.dumps(
            {
                "type": "assistant",
                "message": {
                    "id": "msg_b",
                    "usage": {
                        "input_tokens": 5,
                        "output_tokens": 20,
                        "cache_creation_input_tokens": 0,
                        "cache_read_input_tokens": 300,
                    },
                },
            }
        ),
        json.dumps(
            {
                "type": "result",
                "total_cost_usd": 0.42,
                "num_turns": 2,
                "session_id": "sess-1",
                "is_error": False,
            }
        ),
    ]
    stream_file = tmp_path / "stream.jsonl"
    stream_file.write_text("\n".join(stream_lines) + "\n")
    script = tmp_path / "fake-claude.sh"
    script.write_text(
        "#!/bin/sh\n"
        f'printf "%s\\n" "$(basename "$PWD")" >> "{done_marker}"\n'
        f'cat "{stream_file}"\n'
    )
    script.chmod(0o755)
    return script
