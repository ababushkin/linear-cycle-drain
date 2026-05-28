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
        "cycle_cost_usd": 0.0,
        "cycle_tokens_cumulative": 0,
        "cycle_halt_reason": None,
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
        duration_seconds=300.0,
        model="claude-opus-4-7",
        usage={
            "input_tokens": 15,
            "output_tokens": 24,
            "cache_creation_input_tokens": 100,
            "cache_read_input_tokens": 200,
            "cumulative": 339,
            "peak_context": 215,
        },
        cost_usd=1.25,
        num_turns=4,
        session_id="sess-abc",
        is_error=False,
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
    # Aggregates roll up only the entries that carry usage/cost: ABA-X
    # contributes, the null-usage halt entry ABA-Y contributes zero.
    assert payload["cycle_cost_usd"] == 1.25
    assert payload["cycle_tokens_cumulative"] == 339
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
        "duration_seconds",
        "model",
        "usage",
        "cost_usd",
        "num_turns",
        "session_id",
        "is_error",
    }
    assert set(first.keys()) == required_keys
    assert set(second.keys()) == required_keys

    # Worker fields round-trip on the entry that carried them.
    assert first["model"] == "claude-opus-4-7"
    assert first["usage"]["cumulative"] == 339
    assert first["usage"]["peak_context"] == 215
    assert first["cost_usd"] == 1.25
    assert first["num_turns"] == 4
    assert first["session_id"] == "sess-abc"
    assert first["is_error"] is False
    assert first["duration_seconds"] == 300.0

    # The halt entry passed no worker fields: every usage field is null,
    # and duration_seconds is derived from the timestamps (10:05 → 10:10).
    assert second["model"] is None
    assert second["usage"] is None
    assert second["cost_usd"] is None
    assert second["num_turns"] is None
    assert second["session_id"] is None
    assert second["is_error"] is None
    assert second["duration_seconds"] == 300.0

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


def test_debug_path_sits_beside_run_log_named_per_issue(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The opt-in debug-capture path is a sibling of the run log, suffixed
    with the issue identifier so each session's capture is distinct and the
    run-start timestamp keeps re-runs from clobbering it."""
    monkeypatch.setenv("HOME", str(tmp_path))

    log = runlog.RunLog(cycle_id="stub-cycle")
    debug = log.debug_path("ABA-238")

    assert debug.parent == log.path.parent
    assert debug.name == f"{log.path.stem}-ABA-238.debug.log"
    # Distinct per issue, so concurrent-issue captures never collide.
    assert log.debug_path("ABA-1") != log.debug_path("ABA-2")


def test_watch_path_sits_beside_run_log_named_per_issue(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))

    log = runlog.RunLog(cycle_id="stub-cycle")
    watch = log.watch_path("ABA-338")

    assert watch.parent == log.path.parent
    assert watch.name == f"{log.path.stem}-ABA-338.watch.log"
    assert log.watch_path("ABA-1") != log.watch_path("ABA-2")
    # Distinct from the debug path for the same issue.
    assert log.watch_path("ABA-1") != log.debug_path("ABA-1")


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
