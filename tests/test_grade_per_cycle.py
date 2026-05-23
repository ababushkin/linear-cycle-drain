"""Per-cycle section tests for ``drain-cycle grade``.

Pins the four facts the per-cycle section must report: cycle_id,
attempted count, integer completion %, and halted entries rendered as
``<identifier>: (<state>, <exit_code>)``.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from drain_cycle import grade


def _entry(identifier: str, state: str, exit_code: int) -> dict:
    return {
        "issue_identifier": identifier,
        "started_at": "2026-05-22T10:00:00+00:00",
        "finished_at": "2026-05-22T10:05:00+00:00",
        "exit_code": exit_code,
        "final_linear_state": state,
        "worktree_path": f"/tmp/repo/.worktrees/{identifier}",
        "halt_reason": None if state == "Done" else f"Halt: {identifier}",
    }


def _write_run_log(
    runs_dir: Path, cycle_id: str, timestamp: str, entries: list[dict]
) -> Path:
    runs_dir.mkdir(parents=True, exist_ok=True)
    path = runs_dir / f"{cycle_id}-{timestamp}.json"
    path.write_text(
        json.dumps(
            {
                "cycle_id": cycle_id,
                "cycle_duration_seconds": 0.0,
                "entries": entries,
            }
        )
        + "\n"
    )
    return path


def test_per_cycle_section_renders_four_required_facts(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    runs_dir = tmp_path / "runs"
    _write_run_log(
        runs_dir,
        cycle_id="cycle-abc",
        timestamp="20260522T100000000000Z",
        entries=[
            _entry("ABA-1", "Done", 0),
            _entry("ABA-2", "Done", 0),
            _entry("ABA-3", "In Progress", 1),
        ],
    )

    exit_code = grade.run(runs_dir)

    captured = capsys.readouterr()
    assert exit_code == 0

    out = captured.out
    # Fact 1 — cycle_id.
    assert "cycle-abc" in out
    # Fact 2 — attempted count of 3.
    assert "attempted: 3" in out
    # Fact 3 — integer completion % (round(2/3 * 100) = 67).
    assert "67%" in out
    # Fact 4 — halted tuple appears alongside the halted identifier;
    # the two Done identifiers do not appear in the halted list.
    halted_block = out.split("halted:", 1)[1]
    assert "ABA-3: (In Progress, 1)" in halted_block
    assert "ABA-1" not in halted_block
    assert "ABA-2" not in halted_block


def test_walking_skeleton_happy_path_still_prints_cycle_id_and_exits_zero(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The walking-skeleton happy path must stay green."""
    runs_dir = tmp_path / "runs"
    _write_run_log(
        runs_dir,
        cycle_id="stub-cycle-id",
        timestamp="20260522T100000000000Z",
        entries=[_entry("ABA-1", "Done", 0)],
    )

    exit_code = grade.run(runs_dir)

    captured = capsys.readouterr()
    assert exit_code == 0
    assert "stub-cycle-id" in captured.out


def test_per_cycle_sections_ordered_chronologically_by_earliest_filename(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Two cycles → printed oldest-first by their earliest filename."""
    runs_dir = tmp_path / "runs"
    _write_run_log(
        runs_dir,
        cycle_id="cycle-second",
        timestamp="20260522T120000000000Z",
        entries=[_entry("ABA-9", "Done", 0)],
    )
    _write_run_log(
        runs_dir,
        cycle_id="cycle-first",
        timestamp="20260522T100000000000Z",
        entries=[_entry("ABA-1", "Done", 0)],
    )

    exit_code = grade.run(runs_dir)

    captured = capsys.readouterr()
    assert exit_code == 0
    assert captured.out.index("cycle-first") < captured.out.index("cycle-second")


def test_grade_unchanged_by_worker_usage_fields(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Run logs carrying the additive worker-usage fields (and the new
    top-level aggregates) grade identically to ones without them — grade
    reads only ``cycle_id`` / ``final_linear_state`` / ``exit_code``."""
    runs_dir = tmp_path / "runs"
    runs_dir.mkdir(parents=True, exist_ok=True)
    rich_entry = {
        **_entry("ABA-1", "Done", 0),
        "duration_seconds": 12.5,
        "model": "claude-sonnet-4-6",
        "usage": {
            "input_tokens": 15,
            "output_tokens": 24,
            "cache_creation_input_tokens": 100,
            "cache_read_input_tokens": 300,
            "cumulative": 439,
            "peak_context": 305,
        },
        "cost_usd": 0.42,
        "num_turns": 2,
        "session_id": "sess-1",
        "is_error": False,
    }
    halted_entry = {**_entry("ABA-2", "In Progress", 1), "usage": None, "cost_usd": None}
    (runs_dir / "cycle-rich-20260522T100000000000Z.json").write_text(
        json.dumps(
            {
                "cycle_id": "cycle-rich",
                "cycle_duration_seconds": 12.5,
                "cycle_cost_usd": 0.42,
                "cycle_tokens_cumulative": 439,
                "entries": [rich_entry, halted_entry],
            }
        )
        + "\n"
    )

    exit_code = grade.run(runs_dir)

    captured = capsys.readouterr()
    assert exit_code == 0
    assert "cycle-rich" in captured.out
    assert "attempted: 2" in captured.out
    assert "50%" in captured.out
    halted_block = captured.out.split("halted:", 1)[1]
    assert "ABA-2: (In Progress, 1)" in halted_block


def test_cycle_with_two_files_merges_entries_for_completion_percent(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """One cycle, two files, merged on cycle_id."""
    runs_dir = tmp_path / "runs"
    _write_run_log(
        runs_dir,
        cycle_id="merge-me",
        timestamp="20260522T100000000000Z",
        entries=[_entry("ABA-1", "Done", 0)],
    )
    _write_run_log(
        runs_dir,
        cycle_id="merge-me",
        timestamp="20260522T120000000000Z",
        entries=[_entry("ABA-2", "In Progress", 1)],
    )

    exit_code = grade.run(runs_dir)

    captured = capsys.readouterr()
    assert exit_code == 0
    # Two entries across two files → 1 Done / 2 attempted → 50%.
    assert "attempted: 2" in captured.out
    assert "50%" in captured.out
    # One per-cycle block, not two.
    assert captured.out.count("cycle_id: merge-me") == 1
