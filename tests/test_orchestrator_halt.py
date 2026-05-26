"""Halt-on-not-Done test for the orchestrator.

The iteration loop has a minimal non-Done branch that returns ``1`` without
removing the worktree. This test pins that contract:
if the spawned session exits without flipping the issue to Done, the
orchestrator must (a) exit non-zero, (b) leave the worktree on disk so the
operator can inspect it, and (c) not touch any subsequent issue.

The second test in this file pins the halt-path slice of the run-log
artefact: it must contain exactly one entry for the halted issue, with
`final_linear_state` reflecting the non-Done state the agent left it in
— so KR1 grading sees a halted attempt rather than a missing one.

Substitution choices mirror ``test_orchestrator_iteration.py``: real git
repo, in-process Linear stub via attribute monkey-patching, fake ``claude``
shell script as ``_CLAUDE_CMD``. The difference is the script: here it
exits without writing the marker file, so the stubbed ``get_issue`` keeps
returning the original Todo state — which is exactly the production
failure mode this test guards against.
"""
from __future__ import annotations

import json
import subprocess
from datetime import datetime
from pathlib import Path

import pytest

from drain_cycle import limits, linear, orchestrator, repos


_TEST_REPO_NAME = "test-repo"


def _issue(
    identifier: str,
    sort_order: float,
    *,
    repo_name: str = _TEST_REPO_NAME,
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


def test_orchestrator_halts_when_spawn_leaves_issue_not_done(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)
    monkeypatch.chdir(repo)
    monkeypatch.setenv("HOME", str(tmp_path))

    # Two issues; the first will be picked, the spawn will exit without
    # marking it Done. The second must remain untouched.
    first = _issue("ABA-FIRST", sort_order=1.0)
    second = _issue("ABA-SECOND", sort_order=2.0)
    raw_issues = [first, second]
    issues_by_id = {i["id"]: i for i in raw_issues}

    get_issue_calls: list[str] = []

    def fake_current_cycle_id() -> str:
        return "stub-cycle"

    def fake_pending_issues(cycle_id: str) -> list[dict]:
        return linear._plan(raw_issues)

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

    exit_code = orchestrator.run(_stub_repos(repo))

    assert exit_code != 0
    # Loop bailed before touching the second issue. The first id may be
    # re-fetched more than once (initial refresh + post-revert refresh);
    # the load-bearing invariant is the second issue is absent.
    assert get_issue_calls
    assert second["id"] not in get_issue_calls
    # Second issue's stub state is unchanged.
    assert second["state"] == {"type": "unstarted", "name": "Todo"}
    # First issue's worktree is still on disk for the operator to inspect.
    first_worktree = repo / ".worktrees" / first["identifier"]
    assert first_worktree.is_dir()
    # Second issue's worktree was never created.
    second_worktree = repo / ".worktrees" / second["identifier"]
    assert not second_worktree.exists()

    # Spec'd halt UX: a single line on stderr starting with
    # `Halt: ` carrying issue identifier, final state name, and the absolute
    # worktree path the operator must `cd` to. The `Halt:` token is the
    # unique grep anchor — no other stderr line on either branch starts
    # with it.
    stderr_lines = capsys.readouterr().err.splitlines()
    halt_lines = [line for line in stderr_lines if line.startswith("Halt: ")]
    assert len(halt_lines) == 1
    (halt_line,) = halt_lines
    assert first["identifier"] in halt_line
    assert first["state"]["name"] in halt_line
    assert str(first_worktree) in halt_line


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


def test_orchestrator_runlog_records_halted_issue_with_non_done_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Halt-path slice of the run-log artefact.

    The run-log file must contain exactly one entry — for the halted
    issue — with `final_linear_state` matching the non-Done state name
    the agent left it in. The second issue must be absent from entries
    (never attempted) and absent from disk (worktree never created).
    The halted issue's worktree must remain preserved.
    """
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)
    monkeypatch.chdir(repo)
    monkeypatch.setenv("HOME", str(tmp_path))

    first = _issue("ABA-FIRST", sort_order=1.0)
    second = _issue("ABA-SECOND", sort_order=2.0)
    raw_issues = [first, second]
    issues_by_id = {i["id"]: i for i in raw_issues}

    def fake_current_cycle_id() -> str:
        return "stub-cycle-id"

    def fake_pending_issues(cycle_id: str) -> list[dict]:
        return linear._plan(raw_issues)

    def fake_get_issue(issue_id: str) -> dict:
        return issues_by_id[issue_id]

    monkeypatch.setattr(linear, "current_cycle_id", fake_current_cycle_id)
    monkeypatch.setattr(linear, "pending_issues", fake_pending_issues)
    monkeypatch.setattr(linear, "get_issue", fake_get_issue)
    monkeypatch.setattr(linear, "set_state", lambda issue_id, state_name: None)

    fake_claude = _write_fake_claude_script(tmp_path)
    monkeypatch.setattr(orchestrator, "_CLAUDE_CMD", [str(fake_claude)])

    exit_code = orchestrator.run(_stub_repos(repo))
    assert exit_code != 0

    # Per-run filename: one file per drain-cycle invocation,
    # ``<cycle-id>-<run-timestamp>.json`` — glob to locate it.
    runs_dir = tmp_path / ".drain-cycle" / "runs"
    log_files = list(runs_dir.glob("stub-cycle-id-*.json"))
    assert len(log_files) == 1
    log_path = log_files[0]
    payload = json.loads(log_path.read_text())

    assert payload["cycle_id"] == "stub-cycle-id"
    assert len(payload["entries"]) == 1

    # Halt entry's wall-clock duration is the cycle's duration — KR2 still
    # accrues the unattended time we spent on the doomed issue.
    only_entry = payload["entries"][0]
    expected_duration = (
        datetime.fromisoformat(only_entry["finished_at"])
        - datetime.fromisoformat(only_entry["started_at"])
    ).total_seconds()
    assert payload["cycle_duration_seconds"] == pytest.approx(expected_duration)
    assert payload["cycle_duration_seconds"] >= 0.0

    entry = payload["entries"][0]
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
    assert set(entry.keys()) == required_keys
    assert entry["issue_identifier"] == first["identifier"]
    # The agent never flipped state, so the halted entry records "Todo"
    # — load-bearing for KR1 grading (must distinguish halt from success).
    assert entry["final_linear_state"] == first["state"]["name"]
    assert entry["final_linear_state"] != "Done"
    assert entry["worktree_path"] == str(repo / ".worktrees" / first["identifier"])

    # halt_reason equals the stderr halt line exactly — both
    # surfaces come from the same orchestrator._halt_message helper, so
    # the on-disk explanation cannot drift from what the operator saw at
    # halt time.
    stderr_lines = capsys.readouterr().err.splitlines()
    (halt_line,) = [line for line in stderr_lines if line.startswith("Halt: ")]
    assert entry["halt_reason"] == halt_line
    # And the halt line carries the identifier, state name, and absolute
    # worktree path (spec'd shape).
    assert first["identifier"] in halt_line
    assert first["state"]["name"] in halt_line
    assert str(repo / ".worktrees" / first["identifier"]) in halt_line

    # Second issue never attempted: absent from log AND from disk.
    second_worktree = repo / ".worktrees" / second["identifier"]
    assert not second_worktree.exists()
    assert all(
        e["issue_identifier"] != second["identifier"] for e in payload["entries"]
    )

    # Halted worktree preserved.
    assert (repo / ".worktrees" / first["identifier"]).is_dir()


def test_orchestrator_records_halt_when_setup_raises_before_spawn(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A pre-spawn failure (e.g. Linear outage during ``set_state``) must
    convert to a recorded halt — run-log entry + Halt: stderr line + exit 1
    — instead of an uncaught traceback. Without this, the orchestrator
    crashes mid-cycle and the runlog is missing the attempted entry, so
    KR1 grading sees a phantom-zero rather than a halt.
    """
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)
    monkeypatch.chdir(repo)
    monkeypatch.setenv("HOME", str(tmp_path))

    first = _issue("ABA-FIRST", sort_order=1.0)
    second = _issue("ABA-SECOND", sort_order=2.0)
    raw_issues = [first, second]

    def fake_current_cycle_id() -> str:
        return "stub-cycle-id"

    def fake_pending_issues(cycle_id: str) -> list[dict]:
        return linear._plan(raw_issues)

    def failing_set_state(issue_id: str, state_name: str) -> None:
        raise RuntimeError("Linear outage simulated")

    # get_issue must never be called — the orchestrator should halt before
    # the spawn, so there's no post-spawn state refresh.
    def forbidden_get_issue(issue_id: str) -> dict:
        raise AssertionError("get_issue called after setup failure")

    monkeypatch.setattr(linear, "current_cycle_id", fake_current_cycle_id)
    monkeypatch.setattr(linear, "pending_issues", fake_pending_issues)
    monkeypatch.setattr(linear, "set_state", failing_set_state)
    monkeypatch.setattr(linear, "get_issue", forbidden_get_issue)

    # Claude must never be spawned either — the failure is before spawn.
    forbidden_claude = tmp_path / "forbidden-claude.sh"
    forbidden_claude.write_text("#!/bin/sh\nexit 99\n")
    forbidden_claude.chmod(0o755)
    monkeypatch.setattr(orchestrator, "_CLAUDE_CMD", [str(forbidden_claude)])

    exit_code = orchestrator.run(_stub_repos(repo))
    assert exit_code == 1

    runs_dir = tmp_path / ".drain-cycle" / "runs"
    log_files = list(runs_dir.glob("stub-cycle-id-*.json"))
    assert len(log_files) == 1
    payload = json.loads(log_files[0].read_text())

    # One entry recorded for the failing-setup issue; second issue absent.
    assert len(payload["entries"]) == 1
    entry = payload["entries"][0]
    assert entry["issue_identifier"] == first["identifier"]
    # Final Linear state is the pre-failure state — set_state never landed.
    assert entry["final_linear_state"] == first["state"]["name"]
    # halt_reason carries the same string emitted to stderr.
    stderr_lines = capsys.readouterr().err.splitlines()
    (halt_line,) = [line for line in stderr_lines if line.startswith("Halt: ")]
    assert entry["halt_reason"] == halt_line
    # The halt message surfaces the underlying exception so the operator
    # can diagnose without re-reading a stack trace.
    assert "Linear outage simulated" in halt_line
    assert "setup failed" in halt_line

    # Second issue never attempted — same contract as a spawn-time halt.
    assert all(
        e["issue_identifier"] != second["identifier"] for e in payload["entries"]
    )


def test_orchestrator_records_halt_when_claude_subprocess_times_out(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A spawned ``claude -p`` session that exceeds the per-issue timeout
    must convert to a halt — run-log entry + Halt: stderr line + exit 1
    — instead of stalling the cycle forever. The worktree is preserved
    so the operator can inspect what state the hung session left behind.
    """
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)
    monkeypatch.chdir(repo)
    monkeypatch.setenv("HOME", str(tmp_path))

    first = _issue("ABA-FIRST", sort_order=1.0)
    second = _issue("ABA-SECOND", sort_order=2.0)
    raw_issues = [first, second]

    def fake_current_cycle_id() -> str:
        return "stub-cycle-id"

    def fake_pending_issues(cycle_id: str) -> list[dict]:
        return linear._plan(raw_issues)

    issues_by_id = {i["id"]: i for i in raw_issues}

    def fake_get_issue(issue_id: str) -> dict:
        # The timeout halt path reverts state and refreshes — return the
        # original Todo state so the refresh sees the revert.
        return issues_by_id[issue_id]

    monkeypatch.setattr(linear, "current_cycle_id", fake_current_cycle_id)
    monkeypatch.setattr(linear, "pending_issues", fake_pending_issues)
    monkeypatch.setattr(linear, "set_state", lambda issue_id, state_name: None)
    monkeypatch.setattr(linear, "get_issue", fake_get_issue)

    # Fake claude that sleeps longer than the test timeout so the real
    # ``subprocess.run(timeout=…)`` raises TimeoutExpired.
    hanging_claude = tmp_path / "hanging-claude.sh"
    hanging_claude.write_text("#!/bin/sh\nsleep 10\n")
    hanging_claude.chmod(0o755)
    monkeypatch.setattr(orchestrator, "_CLAUDE_CMD", [str(hanging_claude)])

    exit_code = orchestrator.run(
        _stub_repos(repo), limits.Limits(per_issue_seconds=0.3)
    )
    assert exit_code == 1

    runs_dir = tmp_path / ".drain-cycle" / "runs"
    payload = json.loads(next(runs_dir.glob("stub-cycle-id-*.json")).read_text())
    assert len(payload["entries"]) == 1
    entry = payload["entries"][0]
    assert entry["issue_identifier"] == first["identifier"]
    assert entry["final_linear_state"] == first["state"]["name"]

    stderr_lines = capsys.readouterr().err.splitlines()
    (halt_line,) = [line for line in stderr_lines if line.startswith("Halt: ")]
    assert entry["halt_reason"] == halt_line
    assert "per-issue time cap exceeded" in halt_line

    # Worktree preserved for inspection (existing halt contract).
    assert (repo / ".worktrees" / first["identifier"]).is_dir()
    # Second issue never attempted.
    assert all(
        e["issue_identifier"] != second["identifier"] for e in payload["entries"]
    )


def test_orchestrator_reverts_state_on_timeout_halt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """On spawn-timeout halt, the orchestrator must revert the issue's state
    back to the pre-halt state name captured from the cycle query — so a
    re-run picks it up again rather than silently skipping it (the
    silent-skip case).
    """
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)
    monkeypatch.chdir(repo)
    monkeypatch.setenv("HOME", str(tmp_path))

    first = _issue("ABA-FIRST", sort_order=1.0)
    raw_issues = [first]
    issues_by_id = {i["id"]: i for i in raw_issues}
    set_state_calls: list[tuple[str, str]] = []
    # Tracks Linear-side state per issue so a successful revert is visible
    # on the post-revert refresh.
    live_states: dict[str, dict[str, str]] = {
        first["id"]: first["state"].copy(),
    }

    def fake_set_state(issue_id: str, state_name: str) -> None:
        set_state_calls.append((issue_id, state_name))
        live_states[issue_id] = {
            "type": "started" if state_name == "In Progress" else "unstarted",
            "name": state_name,
        }

    def fake_get_issue(issue_id: str) -> dict:
        return {**issues_by_id[issue_id], "state": live_states[issue_id]}

    monkeypatch.setattr(linear, "current_cycle_id", lambda: "stub-cycle-id")
    monkeypatch.setattr(
        linear, "pending_issues", lambda cycle_id: linear._plan(raw_issues)
    )
    monkeypatch.setattr(linear, "set_state", fake_set_state)
    monkeypatch.setattr(linear, "get_issue", fake_get_issue)

    hanging_claude = tmp_path / "hanging-claude.sh"
    hanging_claude.write_text("#!/bin/sh\nsleep 10\n")
    hanging_claude.chmod(0o755)
    monkeypatch.setattr(orchestrator, "_CLAUDE_CMD", [str(hanging_claude)])

    exit_code = orchestrator.run(
        _stub_repos(repo), limits.Limits(per_issue_seconds=0.3)
    )
    assert exit_code == 1

    # Two set_state calls in pick order: In Progress (pre-spawn), then
    # revert to the original "Todo" (post-halt).
    assert set_state_calls == [
        (first["id"], "In Progress"),
        (first["id"], first["state"]["name"]),
    ]
    # Linear ends in the pre-halt state — what makes a re-run pick it up.
    assert live_states[first["id"]]["name"] == first["state"]["name"]

    # The halt entry's final_linear_state reflects the post-revert state
    # observed via refresh, not the in-flight "In Progress".
    runs_dir = tmp_path / ".drain-cycle" / "runs"
    payload = json.loads(next(runs_dir.glob("stub-cycle-id-*.json")).read_text())
    entry = payload["entries"][0]
    assert entry["final_linear_state"] == first["state"]["name"]

    # Same string lands on stderr (invariant).
    stderr_lines = capsys.readouterr().err.splitlines()
    (halt_line,) = [line for line in stderr_lines if line.startswith("Halt: ")]
    assert entry["halt_reason"] == halt_line
    assert first["state"]["name"] in halt_line


def test_orchestrator_reverts_state_on_post_spawn_not_done(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """When the agent exits without flipping to Done — leaving the issue in
    "In Progress" — the orchestrator must revert to the original
    Todo/Backlog state and the run-log records the reverted state.
    """
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)
    monkeypatch.chdir(repo)
    monkeypatch.setenv("HOME", str(tmp_path))

    first = _issue("ABA-FIRST", sort_order=1.0)
    raw_issues = [first]
    issues_by_id = {i["id"]: i for i in raw_issues}
    set_state_calls: list[tuple[str, str]] = []
    live_states: dict[str, dict[str, str]] = {
        first["id"]: first["state"].copy(),
    }

    def fake_set_state(issue_id: str, state_name: str) -> None:
        set_state_calls.append((issue_id, state_name))
        live_states[issue_id] = {
            "type": "started" if state_name == "In Progress" else "unstarted",
            "name": state_name,
        }

    def fake_get_issue(issue_id: str) -> dict:
        return {**issues_by_id[issue_id], "state": live_states[issue_id]}

    monkeypatch.setattr(linear, "current_cycle_id", lambda: "stub-cycle-id")
    monkeypatch.setattr(
        linear, "pending_issues", lambda cycle_id: linear._plan(raw_issues)
    )
    monkeypatch.setattr(linear, "set_state", fake_set_state)
    monkeypatch.setattr(linear, "get_issue", fake_get_issue)

    # Fake claude exits cleanly without flipping state — the poison-pill
    # shape.
    fake_claude = _write_fake_claude_script(tmp_path)
    monkeypatch.setattr(orchestrator, "_CLAUDE_CMD", [str(fake_claude)])

    exit_code = orchestrator.run(_stub_repos(repo))
    assert exit_code == 1

    assert set_state_calls == [
        (first["id"], "In Progress"),
        (first["id"], first["state"]["name"]),
    ]
    assert live_states[first["id"]]["name"] == first["state"]["name"]

    runs_dir = tmp_path / ".drain-cycle" / "runs"
    payload = json.loads(next(runs_dir.glob("stub-cycle-id-*.json")).read_text())
    entry = payload["entries"][0]
    assert entry["final_linear_state"] == first["state"]["name"]

    stderr_lines = capsys.readouterr().err.splitlines()
    (halt_line,) = [line for line in stderr_lines if line.startswith("Halt: ")]
    assert entry["halt_reason"] == halt_line


def test_orchestrator_halt_records_revert_failure_non_fatally(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """If the revert ``set_state`` call raises, the orchestrator still exits
    1 with a halt entry written. The halt-reason explicitly
    notes the revert failure so the operator can fix the state by hand,
    and no traceback escapes.
    """
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)
    monkeypatch.chdir(repo)
    monkeypatch.setenv("HOME", str(tmp_path))

    first = _issue("ABA-FIRST", sort_order=1.0)
    raw_issues = [first]
    issues_by_id = {i["id"]: i for i in raw_issues}
    set_state_calls: list[tuple[str, str]] = []
    revert_error_message = "Linear unavailable during revert"

    def fake_set_state(issue_id: str, state_name: str) -> None:
        set_state_calls.append((issue_id, state_name))
        # First call (Todo → In Progress) succeeds; second call (revert)
        # raises to simulate a Linear outage at exactly the wrong moment.
        if len(set_state_calls) >= 2:
            raise RuntimeError(revert_error_message)

    def fake_get_issue(issue_id: str) -> dict:
        # Agent left the issue in In Progress. Revert will be attempted
        # but will fail.
        return {
            **issues_by_id[issue_id],
            "state": {"type": "started", "name": "In Progress"},
        }

    monkeypatch.setattr(linear, "current_cycle_id", lambda: "stub-cycle-id")
    monkeypatch.setattr(
        linear, "pending_issues", lambda cycle_id: linear._plan(raw_issues)
    )
    monkeypatch.setattr(linear, "set_state", fake_set_state)
    monkeypatch.setattr(linear, "get_issue", fake_get_issue)

    fake_claude = _write_fake_claude_script(tmp_path)
    monkeypatch.setattr(orchestrator, "_CLAUDE_CMD", [str(fake_claude)])

    exit_code = orchestrator.run(_stub_repos(repo))
    assert exit_code == 1

    # Revert was attempted: In Progress (pre-spawn) and then the failing
    # revert call targeting the original state.
    assert set_state_calls == [
        (first["id"], "In Progress"),
        (first["id"], first["state"]["name"]),
    ]

    runs_dir = tmp_path / ".drain-cycle" / "runs"
    payload = json.loads(next(runs_dir.glob("stub-cycle-id-*.json")).read_text())
    assert len(payload["entries"]) == 1
    entry = payload["entries"][0]
    assert entry["issue_identifier"] == first["identifier"]

    # Halt-reason on stderr and in the runlog explicitly surfaces the
    # revert failure so the operator knows the state needs hand-fixing.
    stderr_lines = capsys.readouterr().err.splitlines()
    (halt_line,) = [line for line in stderr_lines if line.startswith("Halt: ")]
    assert entry["halt_reason"] == halt_line
    assert "revert" in halt_line.lower()
    assert revert_error_message in halt_line
    # No traceback escaped onto stderr.
    assert not any("Traceback" in line for line in stderr_lines)


def test_orchestrator_setup_failure_does_not_attempt_revert(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Pre-spawn setup-failure path is unchanged — when
    ``worktree.add`` or the initial ``linear.set_state`` itself raised,
    no revert is needed because the state never moved. This pins the
    "only one set_state call observed" invariant.
    """
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)
    monkeypatch.chdir(repo)
    monkeypatch.setenv("HOME", str(tmp_path))

    first = _issue("ABA-FIRST", sort_order=1.0)
    raw_issues = [first]
    set_state_calls: list[tuple[str, str]] = []

    def failing_set_state(issue_id: str, state_name: str) -> None:
        set_state_calls.append((issue_id, state_name))
        raise RuntimeError("Linear outage simulated")

    monkeypatch.setattr(linear, "current_cycle_id", lambda: "stub-cycle-id")
    monkeypatch.setattr(
        linear, "pending_issues", lambda cycle_id: linear._plan(raw_issues)
    )
    monkeypatch.setattr(linear, "set_state", failing_set_state)
    monkeypatch.setattr(
        linear,
        "get_issue",
        lambda issue_id: (_ for _ in ()).throw(AssertionError("get_issue called")),
    )

    forbidden_claude = tmp_path / "forbidden-claude.sh"
    forbidden_claude.write_text("#!/bin/sh\nexit 99\n")
    forbidden_claude.chmod(0o755)
    monkeypatch.setattr(orchestrator, "_CLAUDE_CMD", [str(forbidden_claude)])

    exit_code = orchestrator.run(_stub_repos(repo))
    assert exit_code == 1
    # Exactly one set_state call — the failed pre-spawn In Progress
    # transition. No revert attempt follows, because the state never moved.
    assert set_state_calls == [(first["id"], "In Progress")]


def test_orchestrator_rerun_after_halt_picks_up_same_issue(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """After a halted run that successfully reverts state, a
    second ``orchestrator.run(_stub_repos(repo))`` invocation against the same cycle picks
    up the previously-halted issue first — because the revert restored
    it to a ``_PENDING_STATE_TYPES`` value and the existing sort key
    preserves its priority position.
    """
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)
    monkeypatch.chdir(repo)
    monkeypatch.setenv("HOME", str(tmp_path))

    first = _issue("ABA-FIRST", sort_order=1.0)
    second = _issue("ABA-SECOND", sort_order=2.0)
    raw_issues = [first, second]
    issues_by_id = {i["id"]: i for i in raw_issues}
    live_states: dict[str, dict[str, str]] = {
        i["id"]: i["state"].copy() for i in raw_issues
    }
    picked_first: list[str] = []

    def fake_set_state(issue_id: str, state_name: str) -> None:
        live_states[issue_id] = {
            "type": "started" if state_name == "In Progress" else "unstarted",
            "name": state_name,
        }

    def fake_pending_issues(cycle_id: str):
        # Mirror the real filter: only Backlog/Todo (unstarted) come back.
        return linear._plan(
            [
                {**issues_by_id[i["id"]], "state": live_states[i["id"]]}
                for i in raw_issues
                if live_states[i["id"]]["type"] in ("backlog", "unstarted")
            ]
        )

    def fake_get_issue(issue_id: str) -> dict:
        return {**issues_by_id[issue_id], "state": live_states[issue_id]}

    monkeypatch.setattr(linear, "current_cycle_id", lambda: "stub-cycle-id")
    monkeypatch.setattr(linear, "pending_issues", fake_pending_issues)
    monkeypatch.setattr(linear, "set_state", fake_set_state)
    monkeypatch.setattr(linear, "get_issue", fake_get_issue)

    # Capture the first-picked identifier per run. The fake records it on
    # invocation (when cwd == the worktree for the picked issue), then
    # exits without flipping state — halting the run.
    trace = tmp_path / "picked.log"
    fake_claude = tmp_path / "fake-claude.sh"
    fake_claude.write_text(
        "#!/bin/sh\n"
        f'printf "%s\\n" "$(basename "$PWD")" >> "{trace}"\n'
        "exit 0\n"
    )
    fake_claude.chmod(0o755)
    monkeypatch.setattr(orchestrator, "_CLAUDE_CMD", [str(fake_claude)])

    first_run_exit = orchestrator.run(_stub_repos(repo))
    assert first_run_exit == 1
    # State reverted to Todo so a re-run can rediscover it.
    assert live_states[first["id"]]["name"] == first["state"]["name"]

    # Remove the halted worktree so the second run can recreate one for
    # the same issue (git worktree add fails if the directory exists).
    halted_worktree = repo / ".worktrees" / first["identifier"]
    if halted_worktree.is_dir():
        import shutil
        shutil.rmtree(halted_worktree)
    subprocess.run(
        ["git", "worktree", "prune"], cwd=repo, check=True, capture_output=True
    )
    subprocess.run(
        ["git", "branch", "-D", first["identifier"]],
        cwd=repo,
        check=False,
        capture_output=True,
    )

    second_run_exit = orchestrator.run(_stub_repos(repo))
    assert second_run_exit == 1

    picked = trace.read_text().splitlines()
    # First pick on each run is the same identifier — the previously
    # halted issue, not the lower-priority second issue.
    assert picked[0] == first["identifier"]
    assert picked[1] == first["identifier"]
