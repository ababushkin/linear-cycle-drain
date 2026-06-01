"""Watch mode: tmux pane lifecycle and the claude-in-pane FIFO wiring.

In watch mode the pane *is* the claude session: the orchestrator runs
``claude ... | tee <fifo>`` in a split pane and reads the same stream-json
bytes off the FIFO. These tests mock tmux; where the external (pane) path is
meant to succeed, a fake-pane writer thread opens the FIFO and streams a
``result`` event (standing in for the pane's claude), so the orchestrator
takes the FIFO path rather than spawning a subprocess.

Tests cover:
- watch=True drives claude through the pane (``| tee <fifo>``); no watch log
  file is left behind
- non-tmux path: no tmux subprocess spawned, drain falls back and completes
- tmux failure swallowed (drain falls back to a normal spawn)
- pane opened before each session, prior pane killed before the next issue,
  final pane left open — using the ID split-window returned
- FIFO startup timeout: pane torn down, drain falls back to a normal spawn
"""
from __future__ import annotations

import json
import shlex
import subprocess
import threading
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


def _fifo_from_pipeline(pipeline: str) -> str:
    """Pull the FIFO path out of a ``... | tee <fifo>; exec ...`` pane command.

    The shell separates ``tee <fifo>`` from the trailing ``exec`` on the ``;``;
    ``shlex.split`` doesn't treat ``;`` as a separator, so the fifo token keeps
    a trailing ``;`` when no space precedes it — strip it back off."""
    tokens = shlex.split(pipeline)
    return tokens[tokens.index("tee") + 1].rstrip(";")


def _make_fake_tmux(
    tmux_calls: list[list[str]],
    pane_ids: list[int],
    *,
    done_marker: Path | None,
) -> Any:
    """A tmux stand-in that captures calls and, when ``done_marker`` is given,
    simulates the pane's claude by writing a ``result`` event into the FIFO and
    marking the issue done — so the orchestrator takes the external FIFO path.

    With ``done_marker=None`` the pane is "opened" but never writes, so the
    FIFO read times out and the orchestrator falls back to a normal spawn.
    """
    real_run = subprocess.run

    def fake_tmux(args: Any, **kwargs: Any) -> Any:
        if not (isinstance(args, list) and args and args[0] == "tmux"):
            return real_run(args, **kwargs)
        tmux_calls.append(list(args))
        if len(args) > 1 and args[1] == "split-window":
            pane_ids[0] += 1
            pane = f"%{pane_ids[0]}"
            if done_marker is not None:
                pipeline = args[-1]
                fifo = _fifo_from_pipeline(pipeline)
                cwd = args[args.index("-c") + 1]
                identifier = Path(cwd).name
                _spawn_fake_pane(fifo, identifier, done_marker)
            return type("R", (), {"stdout": f"{pane}\n", "returncode": 0})()
        return type("R", (), {"stdout": "", "returncode": 0})()

    return fake_tmux


def _spawn_fake_pane(fifo: str, identifier: str, done_marker: Path) -> None:
    """Open the FIFO and stream a single ``result`` event, then mark the issue
    done before closing (so the marker is set before the reader sees EOF)."""

    def _write() -> None:
        f = open(fifo, "w")  # blocks until the orchestrator opens the read end
        f.write(
            json.dumps(
                {
                    "type": "result",
                    "total_cost_usd": 0.0,
                    "num_turns": 1,
                    "session_id": "s",
                    "is_error": False,
                }
            )
            + "\n"
        )
        f.flush()
        with open(done_marker, "a") as d:
            d.write(identifier + "\n")
        f.close()

    threading.Thread(target=_write, daemon=True).start()


def test_watch_runs_claude_in_pane_via_fifo(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """watch=True inside tmux opens a split pane running ``claude ... | tee
    <fifo>`` per issue, reads the session off the FIFO, and leaves no watch
    log file behind."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)
    monkeypatch.chdir(repo)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("TMUX", "/tmp/tmux-stub,1234,0")

    done_marker = tmp_path / "done.txt"
    raw_issues = [_issue("ABA-W1", 1.0), _issue("ABA-W2", 2.0)]
    _setup_linear_stubs(monkeypatch, raw_issues, done_marker)
    fake_claude = _write_done_script(tmp_path, done_marker)
    monkeypatch.setattr(orchestrator, "_CLAUDE_CMD", [str(fake_claude)])

    tmux_calls: list[list[str]] = []
    monkeypatch.setattr(
        subprocess, "run", _make_fake_tmux(tmux_calls, [0], done_marker=done_marker)
    )

    exit_code = orchestrator.run(repos.Repos(mapping={"test-repo": repo}), watch=True)
    assert exit_code == 0

    split_calls = [c for c in tmux_calls if len(c) > 1 and c[1] == "split-window"]
    assert len(split_calls) == 2
    # The pane command runs the claude argv piped through tee into a FIFO —
    # not a `tail -f` of any log file.
    for call in split_calls:
        pipeline = call[-1]
        assert str(fake_claude) in pipeline
        assert "--output-format" in pipeline and "stream-json" in pipeline
        assert "| tee " in pipeline
        assert "tail -f" not in pipeline

    # No intermediate watch log artifact is left behind.
    runs_dir = tmp_path / ".drain-cycle" / "runs"
    assert list(runs_dir.glob("*.watch.log")) == []


def test_no_tmux_call_when_tmux_env_not_set(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When $TMUX is unset, no tmux subprocess is invoked and the drain falls
    back to a normal spawn."""
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

    exit_code = orchestrator.run(repos.Repos(mapping={"test-repo": repo}), watch=True)
    assert exit_code == 0
    assert tmux_calls == [], f"unexpected tmux calls: {tmux_calls}"


def test_tmux_failure_does_not_crash_drain(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failing tmux split-window is swallowed; the drain falls back to a
    normal spawn and completes."""
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

    real_run = subprocess.run

    def failing_tmux(args: Any, **kwargs: Any) -> Any:
        if isinstance(args, list) and args and args[0] == "tmux":
            raise FileNotFoundError("tmux not found")
        return real_run(args, **kwargs)

    monkeypatch.setattr(subprocess, "run", failing_tmux)

    exit_code = orchestrator.run(repos.Repos(mapping={"test-repo": repo}), watch=True)
    # Drain must succeed (via the subprocess fallback) even though tmux failed.
    assert exit_code == 0
    assert "ABA-TF" in _completed(done_marker)


def test_tmux_pane_opened_and_prior_pane_killed_on_next_issue(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With $TMUX set and two issues: a pane is opened before each session;
    the first pane is killed before the second issue starts (using the ID that
    split-window returned, not the operator's active pane); the second pane is
    left open after the drain completes."""
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
    monkeypatch.setattr(
        subprocess, "run", _make_fake_tmux(tmux_calls, [0], done_marker=done_marker)
    )

    exit_code = orchestrator.run(repos.Repos(mapping={"test-repo": repo}), watch=True)
    assert exit_code == 0

    split_calls = [c for c in tmux_calls if len(c) > 1 and c[1] == "split-window"]
    kill_calls = [c for c in tmux_calls if len(c) > 1 and c[1] == "kill-pane"]

    # Two issues → two split-window calls.
    assert len(split_calls) == 2
    # Exactly one kill: first pane killed before second issue, final left open.
    assert len(kill_calls) == 1
    # The killed pane ID must be the one returned by the first split-window (%1),
    # not the operator's active pane — this is the correctness pin for the
    # split-window -P -F fix.
    killed_target = kill_calls[0][kill_calls[0].index("-t") + 1]
    assert killed_target == "%1", (
        f"wrong pane killed: expected %1 (first pane), got {killed_target!r}"
    )


def test_fifo_timeout_falls_back_to_subprocess(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If the pane opens but never produces output, the FIFO read times out;
    the pane is torn down and the drain falls back to a normal spawn."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)
    monkeypatch.chdir(repo)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("TMUX", "/tmp/tmux-stub,1234,0")
    # Keep the startup window short so the timeout path is quick.
    monkeypatch.setattr(orchestrator, "_WATCH_FIFO_TIMEOUT_SECONDS", 0.2)

    done_marker = tmp_path / "done.txt"
    raw_issues = [_issue("ABA-TO", 1.0)]
    _setup_linear_stubs(monkeypatch, raw_issues, done_marker)
    fake_claude = _write_done_script(tmp_path, done_marker)
    monkeypatch.setattr(orchestrator, "_CLAUDE_CMD", [str(fake_claude)])

    tmux_calls: list[list[str]] = []
    # done_marker=None → the fake pane never writes, so the FIFO read times out.
    monkeypatch.setattr(
        subprocess, "run", _make_fake_tmux(tmux_calls, [0], done_marker=None)
    )

    exit_code = orchestrator.run(repos.Repos(mapping={"test-repo": repo}), watch=True)
    # Fallback spawn ran fake-claude, which marked the issue done.
    assert exit_code == 0
    assert "ABA-TO" in _completed(done_marker)

    split_calls = [c for c in tmux_calls if len(c) > 1 and c[1] == "split-window"]
    kill_calls = [c for c in tmux_calls if len(c) > 1 and c[1] == "kill-pane"]
    assert len(split_calls) == 1
    # The timed-out pane is torn down before the fallback spawn.
    assert len(kill_calls) == 1
    assert kill_calls[0][kill_calls[0].index("-t") + 1] == "%1"


def test_watch_false_by_default_no_panes_or_logs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Default invocation (watch=False) opens no panes and writes no watch
    log files."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)
    monkeypatch.chdir(repo)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("TMUX", "/tmp/tmux-stub,1234,0")

    done_marker = tmp_path / "done.txt"
    raw_issues = [_issue("ABA-DEF", 1.0)]
    _setup_linear_stubs(monkeypatch, raw_issues, done_marker)
    fake_claude = _write_done_script(tmp_path, done_marker)
    monkeypatch.setattr(orchestrator, "_CLAUDE_CMD", [str(fake_claude)])

    tmux_calls: list[list[str]] = []
    monkeypatch.setattr(
        subprocess, "run", _make_fake_tmux(tmux_calls, [0], done_marker=done_marker)
    )

    exit_code = orchestrator.run(repos.Repos(mapping={"test-repo": repo}))
    assert exit_code == 0
    assert tmux_calls == []

    runs_dir = tmp_path / ".drain-cycle" / "runs"
    assert list(runs_dir.glob("*.watch.log")) == []
