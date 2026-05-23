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
