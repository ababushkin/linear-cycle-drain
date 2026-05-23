"""Unit tests for the run-log artefact.

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

    runs_dir = tmp_path / ".drain-cycle" / "runs"
    # Filename is ``<cycle-id>-<run-timestamp>.json`` — one file
    # per drain-cycle invocation, no clobber on re-run.
    assert log.path.parent == runs_dir
    assert log.path.name.startswith("stub-cycle-")
    assert log.path.suffix == ".json"
    assert log.path.is_file()
    files = list(runs_dir.glob("stub-cycle-*.json"))
    assert files == [log.path]
    payload = json.loads(log.path.read_text())
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
        halt_reason="Halt: ABA-Y (final state: Todo) at /tmp/repo/.worktrees/ABA-Y",
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
        "halt_reason",
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

    # halt_reason round-trip: the Done entry's default-None is
    # persisted as JSON `null`, and the halt entry's string survives
    # verbatim — those are the on-disk shapes kill-condition tooling
    # reads.
    assert first["halt_reason"] is None
    assert second["halt_reason"] == (
        "Halt: ABA-Y (final state: Todo) at /tmp/repo/.worktrees/ABA-Y"
    )
    # The raw JSON text carries `null` (not "null"), so consumers that
    # treat the string "null" specially are not misled.
    assert '"halt_reason": null' in log.path.read_text()


def test_two_runlogs_same_cycle_id_write_to_distinct_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Regression pin: distinct files per run on the same cycle.

    Re-running ``drain-cycle`` against the same cycle (after fixing the
    cause of a mid-cycle halt) used to clobber the first run's on-disk
    artefact, silently losing every entry. With per-run filenames
    (``<cycle-id>-<run-timestamp>.json``), both runs survive on disk and
    downstream readers can merge across them by grouping on ``cycle_id``.
    """
    monkeypatch.setenv("HOME", str(tmp_path))

    first = runlog.RunLog(cycle_id="re-run-cycle")
    first.append_entry(
        issue_identifier="ABA-1",
        started_at="2026-05-22T10:00:00+00:00",
        finished_at="2026-05-22T10:01:00+00:00",
        exit_code=0,
        final_linear_state="Done",
        worktree_path="/tmp/repo/.worktrees/ABA-1",
    )

    second = runlog.RunLog(cycle_id="re-run-cycle")
    second.append_entry(
        issue_identifier="ABA-2",
        started_at="2026-05-22T11:00:00+00:00",
        finished_at="2026-05-22T11:02:00+00:00",
        exit_code=0,
        final_linear_state="Done",
        worktree_path="/tmp/repo/.worktrees/ABA-2",
    )

    assert first.path != second.path

    runs_dir = tmp_path / ".drain-cycle" / "runs"
    on_disk = sorted(p.name for p in runs_dir.glob("re-run-cycle-*.json"))
    assert len(on_disk) == 2

    # First run's data survived — not clobbered by the second construction.
    first_payload = json.loads(first.path.read_text())
    assert [e["issue_identifier"] for e in first_payload["entries"]] == ["ABA-1"]

    # Second run's file carries only its own entry.
    second_payload = json.loads(second.path.read_text())
    assert [e["issue_identifier"] for e in second_payload["entries"]] == ["ABA-2"]

    # Both files share the cycle_id, so a downstream merger can group
    # them without reading the filename.
    assert first_payload["cycle_id"] == second_payload["cycle_id"] == "re-run-cycle"
