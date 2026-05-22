"""Verdict-section tests for ``drain-cycle grade`` (Task 4 / ABA-221).

Pins the OK / WATCH / KILL banding and the operator-judgement reminder
on the KILL path.
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


def _write_cycle(
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


def _verdict_section(out: str) -> str:
    return out.split("== Verdict ==", 1)[1]


@pytest.mark.parametrize(
    ("done", "halted", "expected_percent", "expected_label", "kill_reminder"),
    [
        (17, 3, 85, "OK", False),     # 17/20 = 85%
        (13, 7, 65, "WATCH", False),  # 13/20 = 65%
        (3, 7, 30, "KILL", True),     # 3/10 = 30%
    ],
)
def test_verdict_label_and_reminder_match_band(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    done: int,
    halted: int,
    expected_percent: int,
    expected_label: str,
    kill_reminder: bool,
) -> None:
    runs_dir = tmp_path / "runs"
    _write_cycle(
        runs_dir,
        cycle_id="cycle-most-recent",
        timestamp="20260522T100000000000Z",
        done=done,
        halted=halted,
    )

    exit_code = grade.run(runs_dir)

    captured = capsys.readouterr()
    assert exit_code == 0
    verdict = _verdict_section(captured.out)

    assert expected_label in verdict
    assert f"{expected_percent}%" in verdict
    # No cross-contamination — the other two band labels are absent.
    other_labels = {"OK", "WATCH", "KILL"} - {expected_label}
    for other in other_labels:
        assert other not in verdict

    reminder_present = "addressable within one cycle of fixes" in verdict
    assert reminder_present is kill_reminder


def test_verdict_uses_most_recent_cycle_not_earlier_ones(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The most-recent cycle (last in chronological order) drives the verdict."""
    runs_dir = tmp_path / "runs"
    # Earlier cycle: 100% Done — would say OK on its own.
    _write_cycle(
        runs_dir,
        cycle_id="cycle-earlier",
        timestamp="20260522T100000000000Z",
        done=5,
        halted=0,
    )
    # Most recent: 0/5 → KILL must win.
    _write_cycle(
        runs_dir,
        cycle_id="cycle-most-recent",
        timestamp="20260522T120000000000Z",
        done=0,
        halted=5,
    )

    exit_code = grade.run(runs_dir)

    captured = capsys.readouterr()
    assert exit_code == 0
    verdict = _verdict_section(captured.out)
    assert "KILL" in verdict
    assert "OK" not in verdict
    assert "WATCH" not in verdict


def test_per_cycle_and_across_cycles_tests_still_green_with_verdict_added(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    runs_dir = tmp_path / "runs"
    _write_cycle(
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
    assert "trend: flat" in captured.out
