"""Tests for ``drain_cycle/progress.py`` — active-run marker helpers."""
from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from drain_cycle import progress


def test_write_creates_marker_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    data = {"pid": 42, "cycle_id": "c1", "issue": {"identifier": "ABA-1"}}
    progress.write(data)
    written = json.loads(progress.active_path().read_text())
    assert written["pid"] == 42
    assert written["issue"]["identifier"] == "ABA-1"


def test_write_is_atomic_via_tmp_rename(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The temp file is gone after write (renamed to active.json)."""
    monkeypatch.setenv("HOME", str(tmp_path))
    progress.write({"pid": 1})
    tmp = progress.active_path().parent / (progress.active_path().name + ".tmp")
    assert not tmp.exists()
    assert progress.active_path().exists()


def test_clear_removes_marker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    progress.write({"pid": 1})
    assert progress.active_path().exists()
    progress.clear()
    assert not progress.active_path().exists()


def test_clear_is_noop_when_absent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    progress.clear()  # must not raise


def test_read_returns_none_when_absent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    assert progress.read() is None


def test_read_returns_none_on_corrupt_json(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    progress.active_path().parent.mkdir(parents=True, exist_ok=True)
    progress.active_path().write_text("not json")
    assert progress.read() is None


def test_read_roundtrips_written_data(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    data = {"pid": 99, "index": 2, "total": 5, "progress": {"turns": 7}}
    progress.write(data)
    assert progress.read() == data


def test_is_pid_alive_current_process() -> None:
    assert progress.is_pid_alive(os.getpid()) is True


def test_is_pid_alive_nonexistent_pid() -> None:
    # PID 1 is always init (on macOS it's launchd) — present.
    # Use a definitely-dead pid by finding one that doesn't exist.
    # The safest approach: use a very high pid that almost certainly doesn't exist.
    # We can't be 100% sure, so just test a pid we know is gone via fork+exit.
    import subprocess
    proc = subprocess.Popen(["true"])
    proc.wait()
    dead_pid = proc.pid
    assert progress.is_pid_alive(dead_pid) is False


@pytest.mark.parametrize(
    "n, expected",
    [
        (0, "0"),
        (999, "999"),
        (1_000, "1k"),
        (1_500, "1k"),
        (999_999, "999k"),
        (1_000_000, "1.0M"),
        (8_100_000, "8.1M"),
        (10_000_000, "10.0M"),
    ],
)
def test_fmt_tokens(n: int, expected: str) -> None:
    assert progress.fmt_tokens(n) == expected


@pytest.mark.parametrize(
    "seconds, expected",
    [
        (0, "0s"),
        (42, "42s"),
        (59, "59s"),
        (60, "1m"),
        (840, "14m"),
        (3599, "59m"),
        (3600, "1h0m"),
        (3661, "1h1m"),
        (5400, "1h30m"),
    ],
)
def test_fmt_elapsed(seconds: float, expected: str) -> None:
    assert progress.fmt_elapsed(seconds) == expected


def test_format_progress_line() -> None:
    line = progress.format_progress_line(
        "ABA-205",
        turns=42,
        cumulative_tokens=8_100_000,
        peak_context_tokens=180_000,
        elapsed_seconds=840,
    )
    assert line == "ABA-205 · turn 42 · 8.1M tok (peak 180k) · 14m"
