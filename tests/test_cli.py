"""CLI dispatch tests for ``drain-cycle``.

Pins the four dispatch paths in ``cli.main``: zero-arg → orchestrator,
``grade`` → grade.run, ``-h`` / ``--help`` → usage + exit 0, anything
else → usage on stderr + exit 2. The behaviour guarded against is the
old fall-through where a misspelled subcommand silently triggered a
real cycle drain.

ABA-232 added a fifth path: zero-arg must first eagerly load
``repos.yml`` and exit 1 (without invoking the orchestrator, without
writing any run-log) when the config is broken. That test sits below.
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest

from drain_cycle import cli, grade, orchestrator, repos

_SECRET = "DRAIN_CYCLE_TEST_SECRET"


def _write_env(directory: Path, value: str) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / ".env"
    path.write_text(f"{_SECRET}={value}\n")
    return path


def test_load_secrets_reads_home_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ABA-233: an installed tool with no repo-root .env still finds the
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


def _stub_no_op_orchestrator(monkeypatch: pytest.MonkeyPatch) -> list[bool]:
    called: list[bool] = []

    def fake_run(loaded_repos) -> int:
        called.append(True)
        return 0

    monkeypatch.setattr(orchestrator, "run", fake_run)
    monkeypatch.setattr(cli, "load_dotenv", lambda *_a, **_kw: False)
    monkeypatch.setattr(repos, "load", lambda *_a, **_kw: repos.Repos(mapping={}))
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
    def boom(*_a, **_kw) -> int:
        raise AssertionError("orchestrator.run() must not be called on unknown args")

    monkeypatch.setattr(orchestrator, "run", boom)


def forbid_grade(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(_runs_dir) -> int:
        raise AssertionError("grade.run() must not be called on unknown args")

    monkeypatch.setattr(grade, "run", boom)


def test_zero_arg_invocation_eagerly_validates_repos_yml(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """ABA-232: a broken/missing ``repos.yml`` halts the zero-arg
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
