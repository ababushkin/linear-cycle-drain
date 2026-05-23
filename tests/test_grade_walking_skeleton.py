"""Walking-skeleton tests for ``drain-cycle grade``.

Pins the two exit paths: empty/missing runs dir
gives a clear stderr message and non-zero exit; one well-formed fixture
file gives exit 0 with its ``cycle_id`` on stdout. Later sub-issues
extend the output without breaking these guarantees.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from drain_cycle import grade


def _write_fixture(runs_dir: Path, cycle_id: str) -> Path:
    runs_dir.mkdir(parents=True, exist_ok=True)
    path = runs_dir / f"{cycle_id}-20260522T100000000000Z.json"
    path.write_text(
        json.dumps(
            {
                "cycle_id": cycle_id,
                "cycle_duration_seconds": 0.0,
                "entries": [],
            }
        )
        + "\n"
    )
    return path


def test_grade_exits_nonzero_with_clear_message_when_dir_missing(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    missing = tmp_path / "does-not-exist"

    exit_code = grade.run(missing)

    captured = capsys.readouterr()
    assert exit_code != 0
    assert str(missing) in captured.err
    assert "no run logs" in captured.err.lower()


def test_grade_exits_nonzero_with_clear_message_when_dir_empty(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    runs_dir = tmp_path / "runs"
    runs_dir.mkdir()

    exit_code = grade.run(runs_dir)

    captured = capsys.readouterr()
    assert exit_code != 0
    assert str(runs_dir) in captured.err
    assert "no run logs" in captured.err.lower()


def test_grade_reads_one_fixture_and_prints_cycle_id(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    runs_dir = tmp_path / "runs"
    _write_fixture(runs_dir, "stub-cycle-id")

    exit_code = grade.run(runs_dir)

    captured = capsys.readouterr()
    assert exit_code == 0
    assert "stub-cycle-id" in captured.out


def test_grade_skips_malformed_run_log_files_and_reads_the_rest(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """grade is the kill-condition gauge — one bad file on disk should
    warn on stderr and skip, not crash and block the operator from
    reading the rest."""
    runs_dir = tmp_path / "runs"
    _write_fixture(runs_dir, "good-cycle")
    # Invalid JSON.
    (runs_dir / "garbage-20260522T100000000000Z.json").write_text("{not json")
    # Valid JSON but missing required cycle_id key.
    (runs_dir / "missing-key-20260522T110000000000Z.json").write_text(
        json.dumps({"entries": []})
    )

    exit_code = grade.run(runs_dir)

    captured = capsys.readouterr()
    assert exit_code == 0
    # Good fixture still printed.
    assert "good-cycle" in captured.out
    # Both bad files warned about on stderr by name.
    assert "garbage-20260522T100000000000Z.json" in captured.err
    assert "missing-key-20260522T110000000000Z.json" in captured.err
    assert "malformed" in captured.err.lower()
