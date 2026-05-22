"""Verdict-section tests for ``drain-cycle grade`` (Task 4 / ABA-221).

Pins the HEALTHY / WATCH / CONCERNING banding per ABA-197 acceptance.
No project-specific reminders are baked into the output (ABA-197
out-of-scope clause).
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
    ("done", "halted", "expected_percent", "expected_label"),
    [
        (17, 3, 85, "HEALTHY"),      # 17/20 = 85%
        (13, 7, 65, "WATCH"),         # 13/20 = 65%
        (3, 7, 30, "CONCERNING"),     # 3/10 = 30%
    ],
)
def test_verdict_label_matches_band(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    done: int,
    halted: int,
    expected_percent: int,
    expected_label: str,
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
    other_labels = {"HEALTHY", "WATCH", "CONCERNING"} - {expected_label}
    for other in other_labels:
        assert other not in verdict


def test_verdict_output_carries_no_project_specific_reminder(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """ABA-197 out-of-scope clause: project-specific kill-condition strings
    or reminders must not be baked into the CLI output. The verdict is a
    general health band; interpretation belongs to the operator."""
    runs_dir = tmp_path / "runs"
    _write_cycle(
        runs_dir,
        cycle_id="cycle-failing",
        timestamp="20260522T100000000000Z",
        done=0,
        halted=5,
    )

    exit_code = grade.run(runs_dir)

    captured = capsys.readouterr()
    assert exit_code == 0
    verdict = _verdict_section(captured.out)
    assert "kill condition" not in verdict.lower()
    assert "addressable within one cycle" not in verdict.lower()
    assert "reminder" not in verdict.lower()


def test_verdict_uses_most_recent_cycle_not_earlier_ones(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The most-recent cycle (last in chronological order) drives the verdict."""
    runs_dir = tmp_path / "runs"
    # Earlier cycle: 100% Done — would say HEALTHY on its own.
    _write_cycle(
        runs_dir,
        cycle_id="cycle-earlier",
        timestamp="20260522T100000000000Z",
        done=5,
        halted=0,
    )
    # Most recent: 0/5 → CONCERNING must win.
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
    assert "CONCERNING" in verdict
    assert "HEALTHY" not in verdict
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
