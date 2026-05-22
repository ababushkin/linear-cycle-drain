"""Across-cycles section tests for ``drain-cycle grade`` (Task 3 / ABA-220).

Pins the trend label and recurrent-tuple counts over the last
``_TREND_WINDOW`` cycles.
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


def _cycle_with_completion(
    runs_dir: Path, cycle_id: str, timestamp: str, *, done: int, halted: int
) -> Path:
    runs_dir.mkdir(parents=True, exist_ok=True)
    entries: list[dict] = []
    for i in range(done):
        entries.append(_entry(f"{cycle_id}-DONE-{i}", "Done", 0))
    for i in range(halted):
        entries.append(_entry(f"{cycle_id}-HALT-{i}", "In Progress", 1))
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


def _across_section(out: str) -> str:
    return out.split("== Across cycles ==", 1)[1]


@pytest.mark.parametrize(
    ("percents", "expected_trend"),
    [
        ([(2, 3), (3, 2), (4, 1)], "improving"),   # 40%, 60%, 80%
        ([(4, 1), (3, 2), (2, 3)], "regressing"),  # 80%, 60%, 40%
        ([(3, 2), (4, 1), (3, 2)], "flat"),        # 60%, 80%, 60%
    ],
)
def test_trend_label_matches_strict_monotonic_rule(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    percents: list[tuple[int, int]],
    expected_trend: str,
) -> None:
    runs_dir = tmp_path / "runs"
    timestamps = [
        "20260522T100000000000Z",
        "20260522T110000000000Z",
        "20260522T120000000000Z",
    ]
    for idx, ((done, halted), ts) in enumerate(zip(percents, timestamps)):
        _cycle_with_completion(
            runs_dir,
            cycle_id=f"cycle-{idx}",
            timestamp=ts,
            done=done,
            halted=halted,
        )

    exit_code = grade.run(runs_dir)

    captured = capsys.readouterr()
    assert exit_code == 0
    assert f"trend: {expected_trend}" in _across_section(captured.out)


def test_recurrent_tuple_in_two_of_three_cycles_listed_with_count_two(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    runs_dir = tmp_path / "runs"
    # Cycle A: halt at (In Progress, 1) + a Done entry.
    runs_dir.mkdir(parents=True, exist_ok=True)
    (runs_dir / "cycle-a-20260522T100000000000Z.json").write_text(
        json.dumps(
            {
                "cycle_id": "cycle-a",
                "cycle_duration_seconds": 0.0,
                "entries": [
                    _entry("ABA-1", "Done", 0),
                    _entry("ABA-2", "In Progress", 1),
                ],
            }
        )
    )
    # Cycle B: halt at (In Progress, 1) again — recurrent tuple.
    (runs_dir / "cycle-b-20260522T110000000000Z.json").write_text(
        json.dumps(
            {
                "cycle_id": "cycle-b",
                "cycle_duration_seconds": 0.0,
                "entries": [_entry("ABA-3", "In Progress", 1)],
            }
        )
    )
    # Cycle C: halt at (Todo, 2) — one-off, must NOT be listed.
    (runs_dir / "cycle-c-20260522T120000000000Z.json").write_text(
        json.dumps(
            {
                "cycle_id": "cycle-c",
                "cycle_duration_seconds": 0.0,
                "entries": [_entry("ABA-4", "Todo", 2)],
            }
        )
    )

    exit_code = grade.run(runs_dir)

    captured = capsys.readouterr()
    assert exit_code == 0
    across = _across_section(captured.out)

    assert "(In Progress, 1) x 2" in across
    # The (Done, 0) tuple is present in only cycle A → not recurrent.
    assert "(Done, 0)" not in across
    # The one-off (Todo, 2) is present in only cycle C → not recurrent.
    assert "(Todo, 2)" not in across


def test_per_cycle_tests_still_green_with_across_section_added(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Sanity: Task 2's required facts still appear after Task 3 lands."""
    runs_dir = tmp_path / "runs"
    _cycle_with_completion(
        runs_dir,
        cycle_id="cycle-keep",
        timestamp="20260522T100000000000Z",
        done=2,
        halted=1,
    )

    exit_code = grade.run(runs_dir)

    captured = capsys.readouterr()
    assert exit_code == 0
    assert "cycle-keep" in captured.out
    assert "attempted: 3" in captured.out
    assert "67%" in captured.out
