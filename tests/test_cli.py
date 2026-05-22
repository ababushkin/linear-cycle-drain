"""CLI dispatch tests for ``drain-cycle``.

Pins the four dispatch paths in ``cli.main``: zero-arg → orchestrator,
``grade`` → grade.run, ``-h`` / ``--help`` → usage + exit 0, anything
else → usage on stderr + exit 2. The behaviour guarded against is the
old fall-through where a misspelled subcommand silently triggered a
real cycle drain.
"""
from __future__ import annotations

import pytest

from drain_cycle import cli, grade, orchestrator


def _stub_no_op_orchestrator(monkeypatch: pytest.MonkeyPatch) -> list[bool]:
    called: list[bool] = []

    def fake_run() -> int:
        called.append(True)
        return 0

    monkeypatch.setattr(orchestrator, "run", fake_run)
    monkeypatch.setattr(cli, "load_dotenv", lambda *_a, **_kw: False)
    return called


def _stub_no_op_grade(monkeypatch: pytest.MonkeyPatch) -> list[bool]:
    called: list[bool] = []

    def fake_run(runs_dir) -> int:
        called.append(True)
        return 0

    monkeypatch.setattr(grade, "run", fake_run)
    monkeypatch.setattr(cli, "load_dotenv", lambda *_a, **_kw: False)
    return called


def test_no_args_dispatches_to_orchestrator(monkeypatch: pytest.MonkeyPatch) -> None:
    called = _stub_no_op_orchestrator(monkeypatch)
    monkeypatch.setattr("sys.argv", ["drain-cycle"])

    with pytest.raises(SystemExit) as exc:
        cli.main()

    assert exc.value.code == 0
    assert called == [True]


def test_grade_subcommand_dispatches_to_grade_run(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    called = _stub_no_op_grade(monkeypatch)
    monkeypatch.setattr("sys.argv", ["drain-cycle", "grade"])

    with pytest.raises(SystemExit) as exc:
        cli.main()

    assert exc.value.code == 0
    assert called == [True]


@pytest.mark.parametrize("flag", ["-h", "--help"])
def test_help_flag_prints_usage_and_exits_zero(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    flag: str,
) -> None:
    monkeypatch.setattr(cli, "load_dotenv", lambda *_a, **_kw: False)
    monkeypatch.setattr("sys.argv", ["drain-cycle", flag])

    with pytest.raises(SystemExit) as exc:
        cli.main()

    assert exc.value.code == 0
    captured = capsys.readouterr()
    assert "usage:" in captured.out
    assert "drain the current Linear cycle" in captured.out


@pytest.mark.parametrize(
    "argv_tail",
    [
        ["bogus"],
        ["grade", "extra"],
        ["grade", "--verbose"],
        ["--unknown"],
    ],
)
def test_unknown_invocation_prints_usage_to_stderr_and_exits_two(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    argv_tail: list[str],
) -> None:
    """The old fall-through silently ran the orchestrator on `drain-cycle bogus`.
    That's a foot-gun — the orchestrator is destructive (creates worktrees,
    transitions Linear state). Misspellings must fail loudly."""
    forbid_orchestrator(monkeypatch)
    forbid_grade(monkeypatch)
    monkeypatch.setattr(cli, "load_dotenv", lambda *_a, **_kw: False)
    monkeypatch.setattr("sys.argv", ["drain-cycle", *argv_tail])

    with pytest.raises(SystemExit) as exc:
        cli.main()

    assert exc.value.code == 2
    captured = capsys.readouterr()
    assert "unknown invocation" in captured.err
    assert "usage:" in captured.err


def forbid_orchestrator(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom() -> int:
        raise AssertionError("orchestrator.run() must not be called on unknown args")

    monkeypatch.setattr(orchestrator, "run", boom)


def forbid_grade(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(_runs_dir) -> int:
        raise AssertionError("grade.run() must not be called on unknown args")

    monkeypatch.setattr(grade, "run", boom)
