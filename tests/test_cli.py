"""CLI dispatch tests for ``drain-cycle``.

Pins the four dispatch paths in ``cli.main``: zero-arg → orchestrator,
``grade`` → grade.run, ``-h`` / ``--help`` → usage + exit 0, anything
else → usage on stderr + exit 2. The behaviour guarded against is the
old fall-through where a misspelled subcommand silently triggered a
real cycle drain.

A fifth path: zero-arg must first eagerly load ``repos.yml`` and exit 1
(without invoking the orchestrator, without writing any run-log) when
the config is broken. That test sits below.
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest

from drain_cycle import cli, grade, limits, orchestrator, repos, status

_SECRET = "DRAIN_CYCLE_TEST_SECRET"


def _write_env(directory: Path, value: str) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / ".env"
    path.write_text(f"{_SECRET}={value}\n")
    return path


def test_load_secrets_reads_home_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An installed tool with no repo-root .env still finds the
    secret in ~/.drain-cycle/.env."""
    monkeypatch.setattr(os, "environ", dict(os.environ))
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv(_SECRET, raising=False)
    monkeypatch.setattr(cli, "_REPO_ENV", tmp_path / "nonexistent" / ".env")
    _write_env(tmp_path / ".drain-cycle", "from-home")

    cli._load_secrets()

    assert os.environ[_SECRET] == "from-home"


def test_load_secrets_shell_var_wins_over_home_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A shell export always beats the file (load_dotenv override=False)."""
    monkeypatch.setattr(os, "environ", dict(os.environ))
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv(_SECRET, "from-shell")
    monkeypatch.setattr(cli, "_REPO_ENV", tmp_path / "nonexistent" / ".env")
    _write_env(tmp_path / ".drain-cycle", "from-home")

    cli._load_secrets()

    assert os.environ[_SECRET] == "from-shell"


def test_load_secrets_home_env_wins_over_repo_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """First hit wins: ~/.drain-cycle/.env is read before the repo-root
    fallback, so it takes precedence when both define the key."""
    monkeypatch.setattr(os, "environ", dict(os.environ))
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv(_SECRET, raising=False)
    repo_env = _write_env(tmp_path / "repo", "from-repo")
    monkeypatch.setattr(cli, "_REPO_ENV", repo_env)
    _write_env(tmp_path / ".drain-cycle", "from-home")

    cli._load_secrets()

    assert os.environ[_SECRET] == "from-home"


def _stub_no_op_orchestrator(monkeypatch: pytest.MonkeyPatch) -> list[dict]:
    calls: list[dict] = []

    def fake_run(loaded_repos, loaded_limits, *, watch: bool = False) -> int:
        calls.append({"watch": watch})
        return 0

    monkeypatch.setattr(orchestrator, "run", fake_run)
    monkeypatch.setattr(cli, "load_dotenv", lambda *_a, **_kw: False)
    monkeypatch.setattr(repos, "load", lambda *_a, **_kw: repos.Repos(mapping={}))
    monkeypatch.setattr(limits, "load", lambda *_a, **_kw: limits.Limits())
    return calls


def _stub_no_op_grade(monkeypatch: pytest.MonkeyPatch) -> list[bool]:
    called: list[bool] = []

    def fake_run(runs_dir) -> int:
        called.append(True)
        return 0

    monkeypatch.setattr(grade, "run", fake_run)
    monkeypatch.setattr(cli, "load_dotenv", lambda *_a, **_kw: False)
    return called


def test_no_args_dispatches_to_orchestrator(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _stub_no_op_orchestrator(monkeypatch)
    monkeypatch.setattr("sys.argv", ["drain-cycle"])

    with pytest.raises(SystemExit) as exc:
        cli.main()

    assert exc.value.code == 0
    assert calls == [{"watch": False}]


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
    def boom(*_a, **_kw) -> int:
        raise AssertionError("orchestrator.run() must not be called on unknown args")

    monkeypatch.setattr(orchestrator, "run", boom)


def forbid_grade(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(_runs_dir) -> int:
        raise AssertionError("grade.run() must not be called on unknown args")

    monkeypatch.setattr(grade, "run", boom)


def test_status_subcommand_dispatches_to_status_run(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    called: list[bool] = []

    def fake_status_run() -> int:
        called.append(True)
        return 0

    monkeypatch.setattr(status, "run", fake_status_run)
    monkeypatch.setattr(cli, "load_dotenv", lambda *_a, **_kw: False)
    monkeypatch.setattr("sys.argv", ["drain-cycle", "status"])

    with pytest.raises(SystemExit) as exc:
        cli.main()

    assert exc.value.code == 0
    assert called == [True]


def test_zero_arg_invocation_eagerly_validates_repos_yml(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A broken/missing ``repos.yml`` halts the zero-arg
    invocation before any orchestrator call — no Linear traffic, no
    run-log file, exit 1 on stderr. Stubs orchestrator with a tripwire
    so a regression that defers validation until after the orchestrator
    starts (and consequently after Linear is hit) fails loudly here."""
    forbid_orchestrator(monkeypatch)
    monkeypatch.setattr(cli, "load_dotenv", lambda *_a, **_kw: False)

    def failing_load(*_a, **_kw) -> repos.Repos:
        raise repos.RepoConfigError("~/.drain-cycle/repos.yml not found at /nope")

    monkeypatch.setattr(repos, "load", failing_load)
    monkeypatch.setattr("sys.argv", ["drain-cycle"])

    with pytest.raises(SystemExit) as exc:
        cli.main()

    assert exc.value.code == 1
    captured = capsys.readouterr()
    assert "repos.yml" in captured.err
    # Pure stderr surface — nothing on stdout for the operator to parse.
    assert captured.out == ""


def test_zero_arg_invocation_eagerly_validates_limits_yml(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A malformed ``limits.yml`` halts the zero-arg invocation exit 1 on
    stderr before the orchestrator runs — same eager-validation contract as
    ``repos.yml``, so a typo'd guardrail can't silently fall back to defaults
    mid-run."""
    forbid_orchestrator(monkeypatch)
    monkeypatch.setattr(cli, "load_dotenv", lambda *_a, **_kw: False)
    monkeypatch.setattr(repos, "load", lambda *_a, **_kw: repos.Repos(mapping={}))

    def failing_load(*_a, **_kw) -> limits.Limits:
        raise limits.LimitsConfigError(
            "~/.drain-cycle/limits.yml entry 'per_issue_tokens': must be a "
            "positive number or null (got 'lots')"
        )

    monkeypatch.setattr(limits, "load", failing_load)
    monkeypatch.setattr("sys.argv", ["drain-cycle"])

    with pytest.raises(SystemExit) as exc:
        cli.main()

    assert exc.value.code == 1
    captured = capsys.readouterr()
    assert "limits.yml" in captured.err
    assert captured.out == ""


@pytest.mark.parametrize("flag", ["--watch", "-w"])
def test_watch_flag_passes_watch_true_to_orchestrator(
    monkeypatch: pytest.MonkeyPatch,
    flag: str,
) -> None:
    """``--watch`` and ``-w`` both pass ``watch=True`` to ``orchestrator.run``."""
    calls = _stub_no_op_orchestrator(monkeypatch)
    monkeypatch.delenv("TMUX", raising=False)
    monkeypatch.setattr("sys.argv", ["drain-cycle", flag])

    with pytest.raises(SystemExit) as exc:
        cli.main()

    assert exc.value.code == 0
    assert calls == [{"watch": True}]


def test_watch_flag_prints_warning_when_tmux_not_set(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """When ``--watch`` is used outside tmux, a one-time warning is printed."""
    _stub_no_op_orchestrator(monkeypatch)
    monkeypatch.delenv("TMUX", raising=False)
    monkeypatch.setattr("sys.argv", ["drain-cycle", "--watch"])

    with pytest.raises(SystemExit):
        cli.main()

    captured = capsys.readouterr()
    assert "TMUX" in captured.err or "tmux" in captured.err.lower()


def test_watch_flag_no_warning_when_tmux_set(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Inside a tmux session no extra warning is printed for ``--watch``."""
    _stub_no_op_orchestrator(monkeypatch)
    monkeypatch.setenv("TMUX", "/tmp/tmux-stub,1234,0")
    monkeypatch.setattr("sys.argv", ["drain-cycle", "--watch"])

    with pytest.raises(SystemExit):
        cli.main()

    captured = capsys.readouterr()
    # No TMUX warning on stderr (drain-cycle: picked ... lines are fine).
    stderr_lines = [l for l in captured.err.splitlines() if "tmux" in l.lower() and "warning" in l.lower()]
    assert stderr_lines == []
