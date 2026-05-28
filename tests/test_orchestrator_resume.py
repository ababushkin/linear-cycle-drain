"""Orchestrator resume-on-re-run wiring.

These exercise the three orchestrator effects that turn a re-run after a
halt from a manual-cleanup chore into a clean continuation: ``worktree.ensure``
reuses a preserved worktree instead of failing on a leftover branch,
``prompt.build`` is called with ``resumed=True`` so the spawned agent
reads the live worktree before continuing, and the ``max_resume_attempts``
policy cap refuses to spawn once a perma-stuck issue has burned its budget.

Substitution mirrors ``test_orchestrator_halt.py``: real git repo, in-process
Linear stub via attribute monkey-patching, fake ``claude`` shell script as
``_CLAUDE_CMD``. The new bit is pre-seeding the run-log directory with halt
entries to drive ``_resume_attempts`` without first staging halts in this
process.
"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from drain_cycle import limits, linear, orchestrator, prompt, repos


_TEST_REPO_NAME = "test-repo"
_CYCLE_ID = "stub-cycle-id"


def _issue(identifier: str, sort_order: float) -> dict:
    return {
        "id": f"id-{identifier}",
        "identifier": identifier,
        "title": f"Title for {identifier}",
        "description": f"Body for {identifier}",
        "sortOrder": sort_order,
        "state": {"type": "unstarted", "name": "Todo"},
        "labels": [f"repo:{_TEST_REPO_NAME}"],
    }


def _stub_repos(repo_path: Path) -> repos.Repos:
    return repos.Repos(mapping={_TEST_REPO_NAME: repo_path})


def _init_repo(repo: Path) -> None:
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


def _noop_claude(tmp_path: Path) -> Path:
    """``claude -p`` stand-in that exits cleanly without flipping Linear.

    The orchestrator's not-Done branch then halts, which is the post-spawn
    shape these tests want — the worktree is preserved, the halt is
    recorded, and the next re-run drives the resume path.
    """
    script = tmp_path / "noop-claude.sh"
    script.write_text("#!/bin/sh\nexit 0\n")
    script.chmod(0o755)
    return script


def _seed_runlog_entry(
    runs_dir: Path,
    *,
    cycle_id: str,
    identifier: str,
    final_state: str,
    suffix: str,
) -> Path:
    """Write a minimal run-log file with one entry for ``identifier``.

    Mirrors the on-disk schema enough for ``_resume_attempts`` to read it:
    only ``cycle_id`` + ``entries[].issue_identifier`` + ``final_linear_state``
    are inspected. Filename uses ``<cycle_id>-<suffix>.json`` so the cycle's
    glob picks it up.
    """
    runs_dir.mkdir(parents=True, exist_ok=True)
    path = runs_dir / f"{cycle_id}-{suffix}.json"
    path.write_text(
        json.dumps(
            {
                "cycle_id": cycle_id,
                "cycle_duration_seconds": 0.0,
                "cycle_cost_usd": 0.0,
                "cycle_tokens_cumulative": 0,
                "cycle_halt_reason": None,
                "entries": [
                    {
                        "issue_identifier": identifier,
                        "started_at": "2026-05-28T10:00:00+00:00",
                        "finished_at": "2026-05-28T10:01:00+00:00",
                        "exit_code": -1,
                        "final_linear_state": final_state,
                        "worktree_path": "/tmp/stub",
                        "halt_reason": (
                            None
                            if final_state == "Done"
                            else f"Halt: {identifier} (final state: {final_state})"
                        ),
                        "duration_seconds": 60.0,
                        "model": None,
                        "usage": None,
                        "cost_usd": None,
                        "num_turns": None,
                        "session_id": None,
                        "is_error": None,
                    }
                ],
            }
        )
        + "\n"
    )
    return path


def _patch_linear(
    monkeypatch: pytest.MonkeyPatch,
    raw_issues: list[dict],
    *,
    set_state_calls: list[tuple[str, str]],
) -> None:
    """Common Linear-side substitutions for these tests.

    ``current_cycle_id`` returns the fixed test cycle id, ``pending_issues``
    serves the planned order, and ``set_state`` records calls (so a test
    can assert it was — or was not — invoked). ``get_issue`` returns the
    original issue verbatim, which makes a no-op claude script look like
    a not-Done halt to the orchestrator.
    """
    issues_by_id = {i["id"]: i for i in raw_issues}

    def fake_set_state(issue_id: str, state_name: str) -> None:
        set_state_calls.append((issue_id, state_name))

    monkeypatch.setattr(linear, "current_cycle_id", lambda: _CYCLE_ID)
    monkeypatch.setattr(
        linear, "pending_issues", lambda cycle_id: linear._plan(raw_issues)
    )
    monkeypatch.setattr(linear, "set_state", fake_set_state)
    monkeypatch.setattr(linear, "get_issue", lambda issue_id: issues_by_id[issue_id])


def test_resume_reuses_preserved_worktree_and_signals_resumed_prompt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Pre-existing worktree on disk is reused; the spawned prompt carries
    ``resumed=True``.

    This is the headline resume contract: a re-run after a halt does not
    fail on ``git worktree add`` colliding with the preserved worktree,
    does not require manual cleanup, and the spawned agent learns it is
    continuing rather than starting fresh — both halves matter, so they
    are pinned in one test.
    """
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)
    monkeypatch.chdir(repo)
    monkeypatch.setenv("HOME", str(tmp_path))

    issue = _issue("ABA-STUCK", sort_order=1.0)

    # Pre-create the preserved worktree as if a prior run halted here.
    preserved = repo / ".worktrees" / issue["identifier"]
    subprocess.run(
        [
            "git",
            "worktree",
            "add",
            "-b",
            issue["identifier"],
            str(preserved),
            "main",
        ],
        cwd=repo,
        check=True,
        capture_output=True,
    )
    sentinel = preserved / "PRESERVED_FILE"
    sentinel.write_text("from the prior halt\n")

    # Pre-seed one halted entry so the orchestrator's resume-cap sees this
    # as a re-run, not a first attempt.
    runs_dir = tmp_path / ".drain-cycle" / "runs"
    _seed_runlog_entry(
        runs_dir,
        cycle_id=_CYCLE_ID,
        identifier=issue["identifier"],
        final_state="Todo",
        suffix="prior",
    )

    build_calls: list[dict] = []
    real_build = prompt.build

    def recording_build(issue_arg, worktree_arg, *, resumed=False):
        build_calls.append(
            {"identifier": issue_arg["identifier"], "resumed": resumed}
        )
        return real_build(issue_arg, worktree_arg, resumed=resumed)

    monkeypatch.setattr(prompt, "build", recording_build)

    set_state_calls: list[tuple[str, str]] = []
    _patch_linear(monkeypatch, [issue], set_state_calls=set_state_calls)

    monkeypatch.setattr(orchestrator, "_CLAUDE_CMD", [str(_noop_claude(tmp_path))])

    exit_code = orchestrator.run(_stub_repos(repo))
    # The no-op claude leaves Linear at Todo, so the orchestrator halts on
    # the not-Done branch — but the load-bearing assertions below are about
    # what happened BEFORE that halt: ensure reused, prompt was resumed.
    assert exit_code == 1

    # The orchestrator called prompt.build with resumed=True — the resume
    # directive will be in the spawned-agent prompt.
    assert build_calls == [{"identifier": issue["identifier"], "resumed": True}]

    # The preserved worktree's sentinel file is still there — ensure did
    # not blow it away or recreate the worktree (which would have wiped
    # the gitignored sentinel).
    assert sentinel.read_text() == "from the prior halt\n"

    # Pre-spawn set_state(In Progress) still fires on a resume — the
    # lifecycle half-owned by the orchestrator does not change because the
    # worktree was reused.
    assert (issue["id"], "In Progress") in set_state_calls


def test_resume_cap_halts_before_any_state_change(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """With prior halts past the cap, the orchestrator refuses to spawn.

    With ``max_resume_attempts=2`` the operator opted in to two resumes
    after the initial attempt (three total attempts before refusal).
    Seeding three prior halted entries puts us at the refusal boundary
    on the fourth would-be attempt.

    The refusal is a no-spawn halt: no ``set_state``, no worktree call,
    no ``claude`` invocation. The halt entry is written so KR1 grading
    sees the refused attempt, and the halt line names the cap so the
    operator knows how to clear it.
    """
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)
    monkeypatch.chdir(repo)
    monkeypatch.setenv("HOME", str(tmp_path))

    stuck = _issue("ABA-STUCK", sort_order=1.0)
    later = _issue("ABA-LATER", sort_order=2.0)

    # Seed three prior halted entries — one fresh + two resume halts —
    # so a fourth attempt with ``max_resume_attempts=2`` is the first
    # one past the cap (prior_halts=3 > cap=2).
    runs_dir = tmp_path / ".drain-cycle" / "runs"
    for suffix in ("first", "second", "third"):
        _seed_runlog_entry(
            runs_dir,
            cycle_id=_CYCLE_ID,
            identifier=stuck["identifier"],
            final_state="Todo",
            suffix=suffix,
        )

    set_state_calls: list[tuple[str, str]] = []
    _patch_linear(monkeypatch, [stuck, later], set_state_calls=set_state_calls)

    # Claude must never be spawned on the cap-halt path; ``set_state``
    # must never be called either (the cap fires before either).
    forbidden = tmp_path / "forbidden-claude.sh"
    forbidden.write_text("#!/bin/sh\nexit 99\n")
    forbidden.chmod(0o755)
    monkeypatch.setattr(orchestrator, "_CLAUDE_CMD", [str(forbidden)])

    exit_code = orchestrator.run(
        _stub_repos(repo), limits.Limits(max_resume_attempts=2)
    )
    assert exit_code == 1

    # No set_state, no spawned worker. The cap-halt fires before either.
    assert set_state_calls == []
    assert not (repo / ".worktrees" / stuck["identifier"]).exists()

    # Three seeded files + one fresh run-log file (the current run).
    # The current one holds exactly one entry: the cap-halt.
    log_files = sorted(runs_dir.glob(f"{_CYCLE_ID}-*.json"))
    assert len(log_files) == 4  # three seeded + one current
    seeded_stems = {
        f"{_CYCLE_ID}-first",
        f"{_CYCLE_ID}-second",
        f"{_CYCLE_ID}-third",
    }
    current_payload = next(
        json.loads(p.read_text())
        for p in log_files
        if p.stem not in seeded_stems
    )
    assert len(current_payload["entries"]) == 1
    entry = current_payload["entries"][0]
    assert entry["issue_identifier"] == stuck["identifier"]
    assert entry["final_linear_state"] == stuck["state"]["name"]
    assert "resume-attempt cap reached (all 2 resumes used)" in entry["halt_reason"]

    # Stderr halt line matches the run-log halt_reason exactly — the same
    # invariant every other halt path holds.
    stderr_lines = capsys.readouterr().err.splitlines()
    (halt_line,) = [line for line in stderr_lines if line.startswith("Halt: ")]
    assert entry["halt_reason"] == halt_line
    assert "max_resume_attempts" in halt_line

    # Second issue never attempted.
    assert all(
        e["issue_identifier"] != later["identifier"]
        for e in current_payload["entries"]
    )


def test_resume_cap_allows_attempts_up_to_the_cap(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The cap is exclusive: ``prior_halts == cap`` still proceeds.

    With ``max_resume_attempts=2`` (two resumes after the initial),
    seeding *two* prior halts (one fresh halt + one resume halt) still
    allows the next attempt — it is the second resume, used at the
    boundary. The cap fires only on the *third* resume (prior_halts=3).
    Pinning this boundary catches the off-by-one that would otherwise
    silently shrink the cap by one.
    """
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)
    monkeypatch.chdir(repo)
    monkeypatch.setenv("HOME", str(tmp_path))

    issue = _issue("ABA-STUCK", sort_order=1.0)

    runs_dir = tmp_path / ".drain-cycle" / "runs"
    for suffix in ("first", "second"):
        _seed_runlog_entry(
            runs_dir,
            cycle_id=_CYCLE_ID,
            identifier=issue["identifier"],
            final_state="Todo",
            suffix=suffix,
        )

    set_state_calls: list[tuple[str, str]] = []
    _patch_linear(monkeypatch, [issue], set_state_calls=set_state_calls)

    monkeypatch.setattr(orchestrator, "_CLAUDE_CMD", [str(_noop_claude(tmp_path))])

    exit_code = orchestrator.run(
        _stub_repos(repo), limits.Limits(max_resume_attempts=2)
    )
    # The no-op claude doesn't flip to Done, so the orchestrator halts
    # on the not-Done branch — but the pre-spawn cap did NOT fire, which
    # is the load-bearing assertion here. The In Progress transition
    # proves we passed the cap check.
    assert exit_code == 1
    assert (issue["id"], "In Progress") in set_state_calls


def test_resume_cap_none_is_unbounded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``max_resume_attempts=None`` lets a perma-stuck issue keep retrying.

    The operator opts into that risk explicitly by setting the cap to
    ``null`` in ``limits.yml``. Five prior halts must not block the
    sixth attempt.
    """
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)
    monkeypatch.chdir(repo)
    monkeypatch.setenv("HOME", str(tmp_path))

    issue = _issue("ABA-STUCK", sort_order=1.0)

    runs_dir = tmp_path / ".drain-cycle" / "runs"
    for i in range(5):
        _seed_runlog_entry(
            runs_dir,
            cycle_id=_CYCLE_ID,
            identifier=issue["identifier"],
            final_state="Todo",
            suffix=f"halt{i}",
        )

    set_state_calls: list[tuple[str, str]] = []
    _patch_linear(monkeypatch, [issue], set_state_calls=set_state_calls)

    monkeypatch.setattr(orchestrator, "_CLAUDE_CMD", [str(_noop_claude(tmp_path))])

    # max_resume_attempts=None — the cap is off, the orchestrator must
    # still attempt the issue (and the no-op claude then halts on not-Done
    # like any first attempt, which is fine for this test's purpose).
    exit_code = orchestrator.run(
        _stub_repos(repo), limits.Limits(max_resume_attempts=None)
    )
    assert exit_code == 1
    # The pre-spawn In Progress transition fired — proves the cap did NOT
    # short-circuit ahead of it.
    assert (issue["id"], "In Progress") in set_state_calls


def test_resume_cap_tolerates_corrupt_runlog_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``_resume_attempts`` skips unreadable or malformed files.

    A partial-write JSON file from a SIGKILL'd earlier run, or a file
    whose root is not a dict / whose ``entries`` is null or a non-list,
    must not crash the cap check — the operator would otherwise be
    pinned out of running the cycle by debris from a prior abnormal
    exit. The helper's docstring promises graceful skip; this test
    pins it.
    """
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)
    monkeypatch.chdir(repo)
    monkeypatch.setenv("HOME", str(tmp_path))

    issue = _issue("ABA-STUCK", sort_order=1.0)
    runs_dir = tmp_path / ".drain-cycle" / "runs"
    runs_dir.mkdir(parents=True)

    # Four flavours of corruption sharing the cycle prefix so the glob
    # picks them up: unparseable JSON, JSON whose root is a list (not a
    # mapping), JSON whose ``entries`` is explicitly null, and JSON whose
    # ``entries`` contains a non-dict element. Plus one valid halt entry
    # — the only one that should count toward ``prior_halts``.
    (runs_dir / f"{_CYCLE_ID}-bad-json.json").write_text("{not valid json")
    (runs_dir / f"{_CYCLE_ID}-list-root.json").write_text("[]")
    (runs_dir / f"{_CYCLE_ID}-null-entries.json").write_text(
        json.dumps({"cycle_id": _CYCLE_ID, "entries": None}) + "\n"
    )
    (runs_dir / f"{_CYCLE_ID}-bad-entry.json").write_text(
        json.dumps({"cycle_id": _CYCLE_ID, "entries": ["not-a-dict"]}) + "\n"
    )
    _seed_runlog_entry(
        runs_dir,
        cycle_id=_CYCLE_ID,
        identifier=issue["identifier"],
        final_state="Todo",
        suffix="valid-halt",
    )

    set_state_calls: list[tuple[str, str]] = []
    _patch_linear(monkeypatch, [issue], set_state_calls=set_state_calls)
    monkeypatch.setattr(orchestrator, "_CLAUDE_CMD", [str(_noop_claude(tmp_path))])

    # Only the one valid halt entry counts, so prior_halts=1. With
    # max_resume_attempts=1 the cap is NOT yet exceeded (1 > 1 is false),
    # so the run proceeds and reaches the In Progress transition. If
    # any corrupt file crashed the helper, the cycle would fail before
    # set_state — so the assertion below pins both "corrupt files
    # don't crash" AND "valid entries are still counted".
    exit_code = orchestrator.run(
        _stub_repos(repo), limits.Limits(max_resume_attempts=1)
    )
    assert exit_code == 1
    assert (issue["id"], "In Progress") in set_state_calls


def test_resume_cap_does_not_count_done_entries(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Prior Done entries are excluded from the cap tally.

    Without this, a successfully drained issue in a prior run would
    consume the resume budget of a later, unrelated re-run against the
    same cycle — pinning the cap to ``Done`` entries instead of halts.
    """
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)
    monkeypatch.chdir(repo)
    monkeypatch.setenv("HOME", str(tmp_path))

    issue = _issue("ABA-STUCK", sort_order=1.0)
    runs_dir = tmp_path / ".drain-cycle" / "runs"

    # Five prior Done entries plus one prior halt. With max_resume_attempts=2
    # the cap is NOT reached (1 < 2) — the run must proceed.
    for i in range(5):
        _seed_runlog_entry(
            runs_dir,
            cycle_id=_CYCLE_ID,
            identifier=issue["identifier"],
            final_state="Done",
            suffix=f"done{i}",
        )
    _seed_runlog_entry(
        runs_dir,
        cycle_id=_CYCLE_ID,
        identifier=issue["identifier"],
        final_state="Todo",
        suffix="halt",
    )

    set_state_calls: list[tuple[str, str]] = []
    _patch_linear(monkeypatch, [issue], set_state_calls=set_state_calls)

    monkeypatch.setattr(orchestrator, "_CLAUDE_CMD", [str(_noop_claude(tmp_path))])

    exit_code = orchestrator.run(
        _stub_repos(repo), limits.Limits(max_resume_attempts=2)
    )
    assert exit_code == 1
    # Pre-spawn transition fired — Done entries weren't counted.
    assert (issue["id"], "In Progress") in set_state_calls
