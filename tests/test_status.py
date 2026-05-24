"""Tests for ``drain-cycle status`` (``drain_cycle/status.py``)."""
from __future__ import annotations

import os
from pathlib import Path

import pytest

from drain_cycle import limits, progress, status


def _marker(
    *,
    pid: int | None = None,
    identifier: str = "ABA-1",
    title: str = "Fix something",
    repo: str = "my-repo",
    index: int = 1,
    total: int = 3,
    model: str = "claude-sonnet-4-6",
    progress_block: dict | None = None,
) -> dict:
    return {
        "pid": pid if pid is not None else os.getpid(),
        "cycle_id": "cycle-abc",
        "run_log_path": "/tmp/run.json",
        "issue": {
            "identifier": identifier,
            "title": title,
            "repo": repo,
            "worktree_path": "/tmp/worktree",
        },
        "model": model,
        "started_at": "2026-05-24T06:00:00+00:00",
        "index": index,
        "total": total,
        "progress": progress_block or {},
    }


def test_status_no_marker(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    rc = status.run()
    assert rc == 0
    out = capsys.readouterr().out
    assert "No active" in out
    assert "grade" in out


def test_status_stale_marker(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A marker whose pid is gone is reported as stale."""
    import subprocess
    proc = subprocess.Popen(["true"])
    proc.wait()
    dead_pid = proc.pid

    monkeypatch.setenv("HOME", str(tmp_path))
    progress.write(_marker(pid=dead_pid))

    rc = status.run()
    assert rc == 0
    out = capsys.readouterr().out
    assert "Stale" in out
    assert str(dead_pid) in out
    assert "rm " in out


def test_status_active_run_prints_all_fields(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(limits, "load", lambda *_a, **_kw: limits.Limits())

    m = _marker(
        identifier="ABA-99",
        title="Important task",
        repo="pde-skills",
        index=2,
        total=5,
        model="claude-sonnet-4-6",
        progress_block={
            "turns": 42,
            "cumulative_tokens": 8_100_000,
            "peak_context_tokens": 180_000,
            "cost_usd": 12.30,
            "elapsed_seconds": 840.0,
            "last_event_at": "2026-05-24T06:14:00+00:00",
        },
    )
    progress.write(m)

    rc = status.run()
    assert rc == 0
    out = capsys.readouterr().out

    assert "ABA-99" in out
    assert "[2/5]" in out
    assert "Important task" in out
    assert "pde-skills" in out
    assert "claude-sonnet-4-6" in out
    assert "14m" in out
    assert "42" in out
    assert "8.1M" in out
    assert "180k" in out
    assert "$12.30" in out


def test_status_token_cap_warning_shown_when_exceeded(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    # Set a very low token cap so the progress is already over it.
    low_cap = limits.Limits(per_issue_tokens=1_000_000)
    monkeypatch.setattr(limits, "load", lambda *_a, **_kw: low_cap)

    m = _marker(
        progress_block={
            "turns": 10,
            "cumulative_tokens": 8_000_001,
            "peak_context_tokens": 100_000,
            "cost_usd": 5.0,
            "elapsed_seconds": 120.0,
        },
    )
    progress.write(m)

    status.run()
    out = capsys.readouterr().out
    assert "⚠" in out


def test_status_cap_shown_without_warning_when_under(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(limits, "load", lambda *_a, **_kw: limits.Limits())

    m = _marker(
        progress_block={
            "turns": 5,
            "cumulative_tokens": 100_000,
            "peak_context_tokens": 50_000,
            "cost_usd": 1.0,
            "elapsed_seconds": 60.0,
        },
    )
    progress.write(m)

    status.run()
    out = capsys.readouterr().out
    assert "[cap:" in out
    assert "⚠" not in out


def test_status_empty_progress_block(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A freshly-written marker with no progress data renders without crashing."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(limits, "load", lambda *_a, **_kw: limits.Limits())
    progress.write(_marker(progress_block={}))
    rc = status.run()
    assert rc == 0
    out = capsys.readouterr().out
    assert "ABA-1" in out
