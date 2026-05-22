"""Unit tests for the run-log artefact (Task 1 / ABA-215, US-C / ABA-196).

Pins the on-disk shape — file path resolved from ``$HOME``, top-level
``cycle_id`` / ``cycle_duration_seconds`` / ``entries`` keys, and the
six required per-entry fields — without spinning up the orchestrator.
Integration of the runlog into ``orchestrator.run()`` is exercised
separately in ``test_orchestrator_runlog.py``.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from drain_cycle import runlog


def test_runlog_initialises_file_with_empty_entries_and_zero_duration(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))

    log = runlog.RunLog(cycle_id="stub-cycle")

    expected_path = tmp_path / ".drain-cycle" / "runs" / "stub-cycle.json"
    assert log.path == expected_path
    assert expected_path.is_file()
    payload = json.loads(expected_path.read_text())
    assert payload == {
        "cycle_id": "stub-cycle",
        "cycle_duration_seconds": 0.0,
        "entries": [],
    }


def test_append_entry_persists_two_entries_in_order_with_required_fields(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))

    log = runlog.RunLog(cycle_id="stub-cycle")
    log.append_entry(
        issue_identifier="ABA-X",
        started_at="2026-05-22T10:00:00+00:00",
        finished_at="2026-05-22T10:05:00+00:00",
        exit_code=0,
        final_linear_state="Done",
        worktree_path="/tmp/repo/.worktrees/ABA-X",
    )
    log.append_entry(
        issue_identifier="ABA-Y",
        started_at="2026-05-22T10:05:00+00:00",
        finished_at="2026-05-22T10:10:00+00:00",
        exit_code=1,
        final_linear_state="Todo",
        worktree_path="/tmp/repo/.worktrees/ABA-Y",
    )

    payload = json.loads(log.path.read_text())
    assert payload["cycle_id"] == "stub-cycle"
    # Spans 10:00:00 → 10:10:00 across the two entries' min-start / max-finish.
    assert payload["cycle_duration_seconds"] == 600.0
    assert isinstance(payload["entries"], list)
    assert len(payload["entries"]) == 2

    first, second = payload["entries"]
    required_keys = {
        "issue_identifier",
        "started_at",
        "finished_at",
        "exit_code",
        "final_linear_state",
        "worktree_path",
    }
    assert set(first.keys()) == required_keys
    assert set(second.keys()) == required_keys

    # Append order pinned: first entry is ABA-X, second is ABA-Y.
    assert first["issue_identifier"] == "ABA-X"
    assert second["issue_identifier"] == "ABA-Y"

    # Per-field types — guards against accidental int→str / bool→int slips.
    for entry in payload["entries"]:
        assert isinstance(entry["issue_identifier"], str)
        assert isinstance(entry["started_at"], str)
        assert isinstance(entry["finished_at"], str)
        assert isinstance(entry["exit_code"], int)
        assert isinstance(entry["final_linear_state"], str)
        assert isinstance(entry["worktree_path"], str)

    # Spot-check non-zero exit code and non-Done final state survived the
    # round-trip — those are the load-bearing fields for the halt path.
    assert second["exit_code"] == 1
    assert second["final_linear_state"] == "Todo"
