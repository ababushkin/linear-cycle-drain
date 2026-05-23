"""Drain-the-cycle iteration test for the orchestrator.

The walking skeleton covered one issue end-to-end. This test pins the
behaviour that *every* sorted Todo/Backlog issue is processed in order, each
in its own worktree, and that every worktree is removed when the spawned
session signals completion.

What we substitute and why:

* Linear: stubbed in-process. The orchestrator imports the linear module by
  attribute, so monkey-patching ``cycle_id`` / ``pending_issues`` / ``get_issue``
  on it is sufficient. We do **not** stub the GraphQL transport — the layer
  under test here is the orchestrator loop, not the wire format (the wire
  format is exercised separately).
* Spawned ``claude -p``: replaced with a real shell script via
  ``_CLAUDE_CMD``. The script writes the basename of its cwd (the issue
  identifier — that's how the orchestrator names worktrees) into a shared
  marker file. The stubbed ``get_issue`` reads that file to decide which
  issues are Done. This satisfies the spec's "no-op script that calls back
  into the stubbed Linear" — the callback path is the marker file rather
  than an in-process call, because the script runs in a separate process.
* Git: a real ``git init`` repo with one commit on ``main``. ``git worktree``
  is exercised for real; this is the cheapest way to be sure the orchestrator
  doesn't paper over a worktree problem.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from drain_cycle import linear, orchestrator, repos, runlog


def _issue(
    identifier: str,
    priority: int,
    sort_order: float,
    *,
    repo_name: str = "test-repo",
) -> dict:
    return {
        "id": f"id-{identifier}",
        "identifier": identifier,
        "title": f"Title for {identifier}",
        "description": f"Body for {identifier}",
        "priority": priority,
        "sortOrder": sort_order,
        "state": {"type": "unstarted", "name": "Todo"},
        "labels": [f"repo:{repo_name}"],
    }


def _init_repo(repo: Path) -> None:
    """Create a real git repo with one commit on ``main`` for worktree tests."""
    subprocess.run(["git", "init", "-b", "main"], cwd=repo, check=True, capture_output=True)
    subprocess.run(
        ["git", "config", "user.email", "test@example.com"], cwd=repo, check=True
    )
    subprocess.run(["git", "config", "user.name", "Test"], cwd=repo, check=True)
    (repo / "README.md").write_text("seed\n")
    subprocess.run(["git", "add", "."], cwd=repo, check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-m", "seed"], cwd=repo, check=True, capture_output=True
    )


def test_orchestrator_drains_every_issue_in_sorted_order(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)
    monkeypatch.chdir(repo)
    monkeypatch.setenv("HOME", str(tmp_path))

    # Three issues — chosen so a "naïve" iteration order (input order) and
    # the priority sort disagree, proving the loop consumes the *sorted*
    # list rather than the unsorted one.
    raw_issues = [
        _issue("ABA-X", priority=3, sort_order=2.0),  # Medium
        _issue("ABA-Y", priority=1, sort_order=1.0),  # Urgent — runs first
        _issue("ABA-Z", priority=2, sort_order=3.0),  # High
    ]
    sorted_issues = linear._sort_pending_issues(raw_issues)
    expected_order = [i["identifier"] for i in sorted_issues]
    assert expected_order == ["ABA-Y", "ABA-Z", "ABA-X"]

    issues_by_id = {i["id"]: i for i in raw_issues}
    done_marker = tmp_path / "done-identifiers.txt"

    def fake_current_cycle_id() -> str:
        return "stub-cycle"

    def fake_pending_issues(cycle_id: str) -> list[dict]:
        # Re-sort each call (mirrors the real client, which sorts the wire
        # response) and exclude anything the fake claude script has already
        # marked Done — so an accidental re-fetch can't re-feed the same
        # issue back into the loop.
        completed = _completed_identifiers(done_marker)
        return linear._sort_pending_issues(
            [i for i in raw_issues if i["identifier"] not in completed]
        )

    def fake_get_issue(issue_id: str) -> dict:
        issue = issues_by_id[issue_id]
        if issue["identifier"] in _completed_identifiers(done_marker):
            return {**issue, "state": {"type": "completed", "name": "Done"}}
        return issue

    monkeypatch.setattr(linear, "current_cycle_id", fake_current_cycle_id)
    monkeypatch.setattr(linear, "pending_issues", fake_pending_issues)
    monkeypatch.setattr(linear, "get_issue", fake_get_issue)
    # set_state is exercised by tests/test_orchestrator_set_state.py; here it's
    # a no-op so this test stays focused on iteration order + worktree cleanup.
    monkeypatch.setattr(linear, "set_state", lambda issue_id, state_name: None)

    fake_claude = _write_fake_claude_script(tmp_path, done_marker)
    monkeypatch.setattr(orchestrator, "_CLAUDE_CMD", [str(fake_claude)])

    exit_code = orchestrator.run(repos.Repos(mapping={"test-repo": repo}))

    assert exit_code == 0
    # The script appends the worktree-basename (== issue identifier) on each
    # run, so the file contents are the exact processing order.
    assert done_marker.read_text().splitlines() == expected_order
    # Every worktree was removed on success.
    worktrees_dir = repo / ".worktrees"
    assert not worktrees_dir.exists() or not any(worktrees_dir.iterdir())
    # ``git worktree list`` should show only the main checkout.
    listed = subprocess.run(
        ["git", "worktree", "list", "--porcelain"],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    assert listed.count("worktree ") == 1


def test_orchestrator_passes_resolved_model_to_worker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The model resolved from the issue's ``model:`` label reaches the
    spawned command as ``--model <resolved>`` — default Sonnet when absent,
    the labelled override otherwise."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)
    monkeypatch.chdir(repo)
    monkeypatch.setenv("HOME", str(tmp_path))

    default_issue = _issue("ABA-DEF", priority=1, sort_order=1.0)
    opus_issue = _issue("ABA-OPUS", priority=2, sort_order=2.0)
    opus_issue["labels"] = ["repo:test-repo", "model:opus"]
    raw_issues = [default_issue, opus_issue]

    issues_by_id = {i["id"]: i for i in raw_issues}
    done_marker = tmp_path / "done-identifiers.txt"
    argv_dir = tmp_path / "argv"
    argv_dir.mkdir()

    def fake_pending_issues(cycle_id: str) -> list[dict]:
        completed = _completed_identifiers(done_marker)
        return linear._sort_pending_issues(
            [i for i in raw_issues if i["identifier"] not in completed]
        )

    def fake_get_issue(issue_id: str) -> dict:
        issue = issues_by_id[issue_id]
        if issue["identifier"] in _completed_identifiers(done_marker):
            return {**issue, "state": {"type": "completed", "name": "Done"}}
        return issue

    monkeypatch.setattr(linear, "current_cycle_id", lambda: "stub-cycle")
    monkeypatch.setattr(linear, "pending_issues", fake_pending_issues)
    monkeypatch.setattr(linear, "get_issue", fake_get_issue)
    monkeypatch.setattr(linear, "set_state", lambda issue_id, state_name: None)

    fake_claude = _write_argv_capturing_claude_script(tmp_path, done_marker, argv_dir)
    monkeypatch.setattr(orchestrator, "_CLAUDE_CMD", [str(fake_claude)])

    exit_code = orchestrator.run(repos.Repos(mapping={"test-repo": repo}))
    assert exit_code == 0

    assert _model_arg(argv_dir / "ABA-DEF.txt") == "claude-sonnet-4-6"
    assert _model_arg(argv_dir / "ABA-OPUS.txt") == "claude-opus-4-7"


def test_orchestrator_links_project_config_into_worker_cwd(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The orchestrator symlinks gitignored project config into the worktree
    before spawning, so the worker's cwd can read ``.claude/settings.json`` —
    the precondition for project hooks to load. Asserts resolvability from the
    worker's cwd, not that hooks actually fire (no real ``claude`` runs)."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)
    # Gitignore the project config exactly as a real repo does, then seed it.
    # It is therefore absent from the worktree checkout until linked in.
    (repo / ".gitignore").write_text(".claude\n.worktrees\n")
    subprocess.run(["git", "add", ".gitignore"], cwd=repo, check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-m", "gitignore"], cwd=repo, check=True, capture_output=True
    )
    (repo / ".claude").mkdir()
    (repo / ".claude" / "settings.json").write_text("{}\n")
    monkeypatch.chdir(repo)
    monkeypatch.setenv("HOME", str(tmp_path))

    issue = _issue("ABA-CFG", priority=1, sort_order=1.0)
    raw_issues = [issue]
    issues_by_id = {i["id"]: i for i in raw_issues}
    done_marker = tmp_path / "done-identifiers.txt"
    probe_dir = tmp_path / "probe"
    probe_dir.mkdir()

    def fake_pending_issues(cycle_id: str) -> list[dict]:
        completed = _completed_identifiers(done_marker)
        return [i for i in raw_issues if i["identifier"] not in completed]

    def fake_get_issue(issue_id: str) -> dict:
        issue = issues_by_id[issue_id]
        if issue["identifier"] in _completed_identifiers(done_marker):
            return {**issue, "state": {"type": "completed", "name": "Done"}}
        return issue

    monkeypatch.setattr(linear, "current_cycle_id", lambda: "stub-cycle")
    monkeypatch.setattr(linear, "pending_issues", fake_pending_issues)
    monkeypatch.setattr(linear, "get_issue", fake_get_issue)
    monkeypatch.setattr(linear, "set_state", lambda issue_id, state_name: None)

    fake_claude = _write_config_probe_claude_script(tmp_path, done_marker, probe_dir)
    monkeypatch.setattr(orchestrator, "_CLAUDE_CMD", [str(fake_claude)])

    exit_code = orchestrator.run(repos.Repos(mapping={"test-repo": repo}))

    assert exit_code == 0
    # The probe ran inside the worktree and could read .claude/settings.json
    # through the symlink the orchestrator created before spawning it.
    assert (probe_dir / "ABA-CFG.txt").read_text().strip() == "present"


def _captured_argv_for_one_issue(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, identifier: str
) -> list[str]:
    """Drain a single-issue cycle through a real worktree + argv-capturing
    fake ``claude``, returning the spawned argv. ``HOME`` / cwd / Linear
    stubs are wired here so the debug-capture tests only assert on the argv;
    the caller sets ``DRAIN_CYCLE_DEBUG`` (or not) before calling."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)
    monkeypatch.chdir(repo)
    monkeypatch.setenv("HOME", str(tmp_path))

    issue = _issue(identifier, priority=1, sort_order=1.0)
    raw_issues = [issue]
    issues_by_id = {i["id"]: i for i in raw_issues}
    done_marker = tmp_path / "done-identifiers.txt"
    argv_dir = tmp_path / "argv"
    argv_dir.mkdir()

    def fake_pending_issues(cycle_id: str) -> list[dict]:
        completed = _completed_identifiers(done_marker)
        return [i for i in raw_issues if i["identifier"] not in completed]

    def fake_get_issue(issue_id: str) -> dict:
        issue = issues_by_id[issue_id]
        if issue["identifier"] in _completed_identifiers(done_marker):
            return {**issue, "state": {"type": "completed", "name": "Done"}}
        return issue

    monkeypatch.setattr(linear, "current_cycle_id", lambda: "stub-cycle")
    monkeypatch.setattr(linear, "pending_issues", fake_pending_issues)
    monkeypatch.setattr(linear, "get_issue", fake_get_issue)
    monkeypatch.setattr(linear, "set_state", lambda issue_id, state_name: None)

    fake_claude = _write_argv_capturing_claude_script(tmp_path, done_marker, argv_dir)
    monkeypatch.setattr(orchestrator, "_CLAUDE_CMD", [str(fake_claude)])

    assert orchestrator.run(repos.Repos(mapping={"test-repo": repo})) == 0
    return (argv_dir / f"{identifier}.txt").read_text().splitlines()


def test_orchestrator_debug_capture_passes_debug_file_beside_runlog(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With ``DRAIN_CYCLE_DEBUG`` set, the spawned session gets a
    ``--debug-file`` whose path is a sibling of the run log, named per
    issue and timestamped with the run."""
    monkeypatch.setenv(orchestrator._DEBUG_ENV_VAR, "1")

    argv = _captured_argv_for_one_issue(tmp_path, monkeypatch, "ABA-DBG")

    assert "--debug-file" in argv
    debug_arg = Path(argv[argv.index("--debug-file") + 1])
    assert debug_arg.parent == runlog.runs_dir()
    assert debug_arg.name.startswith("stub-cycle-")
    assert debug_arg.name.endswith("-ABA-DBG.debug.log")


def test_orchestrator_omits_debug_file_when_capture_off(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Default (no ``DRAIN_CYCLE_DEBUG``): no ``--debug-file`` reaches the
    spawned session."""
    monkeypatch.delenv(orchestrator._DEBUG_ENV_VAR, raising=False)

    argv = _captured_argv_for_one_issue(tmp_path, monkeypatch, "ABA-NODBG")

    assert "--debug-file" not in argv


def _model_arg(argv_file: Path) -> str:
    """Return the token following ``--model`` in a captured argv file."""
    argv = argv_file.read_text().splitlines()
    return argv[argv.index("--model") + 1]


def _write_argv_capturing_claude_script(
    tmp_path: Path, done_marker: Path, argv_dir: Path
) -> Path:
    """A ``claude -p`` stand-in that records its argv (one token per line)
    to ``argv_dir/<identifier>.txt`` and marks the issue Done."""
    script = tmp_path / "fake-claude-argv.sh"
    script.write_text(
        "#!/bin/sh\n"
        'id="$(basename "$PWD")"\n'
        f': > "{argv_dir}/$id.txt"\n'
        f'for a in "$@"; do printf "%s\\n" "$a" >> "{argv_dir}/$id.txt"; done\n'
        f'printf "%s\\n" "$id" >> "{done_marker}"\n'
    )
    script.chmod(0o755)
    return script


def _write_config_probe_claude_script(
    tmp_path: Path, done_marker: Path, probe_dir: Path
) -> Path:
    """A ``claude -p`` stand-in that records whether ``.claude/settings.json``
    is readable from its cwd (``present``/``absent``) to
    ``probe_dir/<identifier>.txt``, then marks the issue Done."""
    script = tmp_path / "fake-claude-probe.sh"
    script.write_text(
        "#!/bin/sh\n"
        'id="$(basename "$PWD")"\n'
        "if [ -r .claude/settings.json ]; then\n"
        f'  printf "present\\n" > "{probe_dir}/$id.txt"\n'
        "else\n"
        f'  printf "absent\\n" > "{probe_dir}/$id.txt"\n'
        "fi\n"
        f'printf "%s\\n" "$id" >> "{done_marker}"\n'
    )
    script.chmod(0o755)
    return script


def _completed_identifiers(marker: Path) -> set[str]:
    if not marker.exists():
        return set()
    return {line for line in marker.read_text().splitlines() if line}


def _write_fake_claude_script(tmp_path: Path, done_marker: Path) -> Path:
    """A no-op stand-in for ``claude -p``.

    Records the basename of its cwd (the issue identifier — orchestrator
    names worktrees ``.worktrees/<identifier>/``) into ``done_marker``.
    The stubbed ``get_issue`` reads this file to learn which issues the
    'agent' has completed.
    """
    script = tmp_path / "fake-claude.sh"
    script.write_text(
        "#!/bin/sh\n"
        f'printf "%s\\n" "$(basename "$PWD")" >> "{done_marker}"\n'
    )
    script.chmod(0o755)
    return script
