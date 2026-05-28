"""Tests for the ``worktree`` git wrapper.

The happy-path is exercised end-to-end by ``test_orchestrator_iteration``.
What these tests pin is the failure mode: git's stderr must be captured
and surfaced in the raised ``RuntimeError`` so the operator (and the
runlog ``halt_reason`` written by the orchestrator) sees what actually
went wrong, not just a non-zero exit code.
"""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from drain_cycle import worktree


def _init_repo(repo: Path) -> None:
    subprocess.run(["git", "init", "-b", "main"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "t@x"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "T"], cwd=repo, check=True)
    (repo / "README.md").write_text("seed\n")
    subprocess.run(["git", "add", "."], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "seed"], cwd=repo, check=True, capture_output=True)


def _commit_gitignore(repo: Path, *patterns: str) -> None:
    (repo / ".gitignore").write_text("\n".join(patterns) + "\n")
    subprocess.run(["git", "add", ".gitignore"], cwd=repo, check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-m", "gitignore"], cwd=repo, check=True, capture_output=True
    )


def test_add_failure_raises_with_git_stderr_in_message(tmp_path: Path) -> None:
    """``git worktree add`` fails when the branch name is already taken.
    The raised RuntimeError must carry git's stderr so the operator can
    diagnose without re-running by hand."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)

    # First add succeeds, taking the branch "ABA-X".
    worktree.add(repo, "ABA-X")
    # Second add fails — same branch.
    with pytest.raises(RuntimeError) as excinfo:
        worktree.add(repo, "ABA-X")

    msg = str(excinfo.value)
    # The message names the operation and includes git's actual complaint
    # (typically "already exists" or "already checked out").
    assert "worktree add" in msg
    assert "ABA-X" in msg


def test_remove_failure_raises_with_git_stderr_in_message(tmp_path: Path) -> None:
    """``git worktree remove`` on a non-existent path fails. The raised
    RuntimeError must carry git's stderr."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)

    bogus = repo / ".worktrees" / "never-existed"
    with pytest.raises(RuntimeError) as excinfo:
        worktree.remove(repo, bogus)

    msg = str(excinfo.value)
    assert "worktree remove" in msg
    # Either git's own "is not a working tree" message or the path itself.
    assert str(bogus) in msg or "working tree" in msg.lower()


def test_link_project_config_symlinks_gitignored_claude(tmp_path: Path) -> None:
    """A gitignored ``.claude/`` (absent from the worktree checkout) is
    symlinked back to the repo's real dir, so the worker reads through it."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)
    _commit_gitignore(repo, ".claude", ".worktrees")
    (repo / ".claude").mkdir()
    (repo / ".claude" / "settings.json").write_text('{"hooks": {}}\n')

    wt = worktree.add(repo, "ABA-A")
    created = worktree.link_project_config(repo, wt, [".claude"])

    assert created == [wt / ".claude"]
    assert (wt / ".claude").is_symlink()
    assert Path(os.readlink(wt / ".claude")) == repo.resolve() / ".claude"
    assert (wt / ".claude" / "settings.json").read_text() == '{"hooks": {}}\n'


def test_link_project_config_noop_when_no_claude(tmp_path: Path) -> None:
    """A repo without the named config produces no link and no error."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)
    _commit_gitignore(repo, ".worktrees")

    wt = worktree.add(repo, "ABA-A")
    created = worktree.link_project_config(repo, wt, [".claude", ".mcp.json"])

    assert created == []
    assert not os.path.lexists(wt / ".claude")
    assert not os.path.lexists(wt / ".mcp.json")


def test_link_project_config_skips_existing_tracked_entry(tmp_path: Path) -> None:
    """A tracked ``.claude/`` git already checked out is left as a real dir,
    never clobbered by a symlink."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)
    (repo / ".claude").mkdir()
    (repo / ".claude" / "settings.json").write_text("tracked\n")
    subprocess.run(["git", "add", ".claude"], cwd=repo, check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-m", "track claude"], cwd=repo, check=True, capture_output=True
    )

    wt = worktree.add(repo, "ABA-A")
    created = worktree.link_project_config(repo, wt, [".claude"])

    assert created == []
    assert not (wt / ".claude").is_symlink()
    assert (wt / ".claude" / "settings.json").read_text() == "tracked\n"


def test_link_project_config_includes_mcp_and_entire_when_present(
    tmp_path: Path,
) -> None:
    """Every present, gitignored name is linked; absent ones are skipped."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)
    _commit_gitignore(repo, ".claude", ".mcp.json", ".entire", ".worktrees")
    (repo / ".mcp.json").write_text("{}\n")
    (repo / ".entire").mkdir()
    (repo / ".entire" / "state").write_text("x\n")

    wt = worktree.add(repo, "ABA-A")
    created = worktree.link_project_config(
        repo, wt, [".claude", ".mcp.json", ".entire"]
    )

    assert set(created) == {wt / ".mcp.json", wt / ".entire"}
    assert (wt / ".mcp.json").is_symlink()
    assert (wt / ".entire").is_symlink()
    assert not os.path.lexists(wt / ".claude")


def test_linked_config_is_gitignored_in_worktree(tmp_path: Path) -> None:
    """The linked entry is gitignored in the worktree (it shares the repo's
    tracked .gitignore), so a worker's ``git add`` never stages it."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)
    _commit_gitignore(repo, ".claude", ".worktrees")
    (repo / ".claude").mkdir()
    (repo / ".claude" / "settings.json").write_text("{}\n")

    wt = worktree.add(repo, "ABA-A")
    worktree.link_project_config(repo, wt, [".claude"])

    status = subprocess.run(
        ["git", "status", "--porcelain"],
        cwd=wt,
        check=True,
        capture_output=True,
        text=True,
    )
    assert ".claude" not in status.stdout


def test_ensure_fresh_creates_worktree_and_marks_resumed_false(tmp_path: Path) -> None:
    """``ensure`` on a previously unseen identifier delegates to ``add``,
    creating the worktree+branch and returning ``resumed=False``."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)

    handle = worktree.ensure(repo, "ABA-FRESH")

    assert handle.resumed is False
    assert handle.path == repo / worktree.WORKTREE_DIR / "ABA-FRESH"
    assert handle.path.is_dir()
    # The branch git just created points at HEAD on main.
    branches = subprocess.run(
        ["git", "branch", "--list", "ABA-FRESH"],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    assert "ABA-FRESH" in branches


def test_ensure_reuse_preserves_dirty_state_and_runs_no_git_command(
    tmp_path: Path,
) -> None:
    """A second ``ensure`` for an already-registered worktree returns the
    same path with ``resumed=True``, and changes the worktree's git state
    in no observable way: a committed file, a staged file, and an
    untracked file all survive untouched. This pins the "no-op on git
    state" guarantee that the dirty-tree acceptance criterion depends on.
    """
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)

    first = worktree.ensure(repo, "ABA-RESUME")
    assert first.resumed is False

    # Make the worktree dirty in three ways: a committed file on the
    # issue branch, a staged-but-uncommitted file, and an untracked file.
    (first.path / "committed.txt").write_text("committed\n")
    subprocess.run(
        ["git", "add", "committed.txt"],
        cwd=first.path,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "commit", "-m", "prior commit"],
        cwd=first.path,
        check=True,
        capture_output=True,
    )
    (first.path / "staged.txt").write_text("staged\n")
    subprocess.run(
        ["git", "add", "staged.txt"],
        cwd=first.path,
        check=True,
        capture_output=True,
    )
    (first.path / "untracked.txt").write_text("untracked\n")

    status_before = subprocess.run(
        ["git", "status", "--porcelain"],
        cwd=first.path,
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    log_before = subprocess.run(
        ["git", "log", "--oneline"],
        cwd=first.path,
        check=True,
        capture_output=True,
        text=True,
    ).stdout

    second = worktree.ensure(repo, "ABA-RESUME")

    assert second.resumed is True
    assert second.path == first.path
    # Status + log are byte-identical: ``ensure`` ran no mutating git command.
    status_after = subprocess.run(
        ["git", "status", "--porcelain"],
        cwd=first.path,
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    log_after = subprocess.run(
        ["git", "log", "--oneline"],
        cwd=first.path,
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    assert status_after == status_before
    assert log_after == log_before
    # All three dirty-state artefacts still exist.
    assert (first.path / "committed.txt").read_text() == "committed\n"
    assert (first.path / "staged.txt").read_text() == "staged\n"
    assert (first.path / "untracked.txt").read_text() == "untracked\n"


def test_ensure_leftover_branch_without_worktree_falls_through_to_add(
    tmp_path: Path,
) -> None:
    """If the operator deletes the worktree directory but leaves the
    branch behind, ``ensure`` does not auto-recover — it falls through
    to ``add``, which raises ``RuntimeError`` (the orchestrator's halt
    path)."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)

    handle = worktree.ensure(repo, "ABA-LEFT")
    # Simulate the partial-cleanup state: worktree gone, branch kept.
    subprocess.run(
        ["git", "worktree", "remove", str(handle.path)],
        cwd=repo,
        check=True,
        capture_output=True,
    )
    branches = subprocess.run(
        ["git", "branch", "--list", "ABA-LEFT"],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    assert "ABA-LEFT" in branches  # branch survived the partial cleanup

    with pytest.raises(RuntimeError) as excinfo:
        worktree.ensure(repo, "ABA-LEFT")
    msg = str(excinfo.value)
    assert "worktree add" in msg
    assert "ABA-LEFT" in msg


def test_ensure_link_project_config_idempotent_on_reuse(tmp_path: Path) -> None:
    """A second ``link_project_config`` on a resumed worktree returns
    an empty list (every name already lexists) and leaves the existing
    symlink intact — confirming the orchestrator can call it
    unconditionally after both fresh and reused ensures."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)
    _commit_gitignore(repo, ".claude", ".worktrees")
    (repo / ".claude").mkdir()
    (repo / ".claude" / "settings.json").write_text('{"hooks": {}}\n')

    handle = worktree.ensure(repo, "ABA-CFG")
    first_links = worktree.link_project_config(repo, handle.path, [".claude"])
    assert first_links == [handle.path / ".claude"]
    assert (handle.path / ".claude").is_symlink()

    # Second call on the reused worktree returns nothing and the link
    # is still pointing at the same target.
    reused = worktree.ensure(repo, "ABA-CFG")
    assert reused.resumed is True
    second_links = worktree.link_project_config(repo, reused.path, [".claude"])
    assert second_links == []
    assert (handle.path / ".claude").is_symlink()
    assert Path(os.readlink(handle.path / ".claude")) == repo.resolve() / ".claude"


def test_remove_preserves_symlink_target(tmp_path: Path) -> None:
    """Removing the worktree deletes the symlink, not the repo's real dir."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)
    _commit_gitignore(repo, ".claude", ".worktrees")
    (repo / ".claude").mkdir()
    (repo / ".claude" / "settings.json").write_text("keep me\n")

    wt = worktree.add(repo, "ABA-A")
    worktree.link_project_config(repo, wt, [".claude"])
    worktree.remove(repo, wt)

    assert (repo / ".claude").is_dir()
    assert (repo / ".claude" / "settings.json").read_text() == "keep me\n"
