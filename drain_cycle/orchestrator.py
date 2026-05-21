"""Walking-skeleton orchestrator: one issue, happy path only.

Multi-issue loop (Task 3 / ABA-200), halt-on-not-Done polish (Task 5 /
ABA-202), and run-log artefact (US-C / ABA-196) all land in later slices.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from . import linear, prompt, worktree

_DONE_STATE_TYPE = "completed"
_CLAUDE_CMD = ["claude", "-p", "--dangerously-skip-permissions"]


def run() -> int:
    repo = Path.cwd()
    cycle_id = linear.current_cycle_id()
    issue = linear.first_pending_issue(cycle_id)
    if issue is None:
        print(f"Cycle {cycle_id} has no Todo/Backlog issues — nothing to do.")
        return 0

    identifier = issue["identifier"]
    print(f"drain-cycle: picked {identifier}: {issue['title']}", file=sys.stderr)

    worktree_path = worktree.add(repo, identifier)
    agent_prompt = prompt.build(issue, worktree_path)

    subprocess.run(
        [*_CLAUDE_CMD, agent_prompt],
        cwd=worktree_path,
        check=False,
    )

    refreshed = linear.get_issue(issue["id"])
    state_type = refreshed["state"]["type"]
    if state_type == _DONE_STATE_TYPE:
        worktree.remove(repo, worktree_path)
        print(f"drain-cycle: {identifier} done; worktree removed.", file=sys.stderr)
        return 0

    # Halt UX (formatted message, deliberate preservation) is US-B / ABA-195.
    # Walking skeleton only guarantees: do not silently advance, leave worktree on disk.
    print(
        f"drain-cycle: {identifier} ended in state {refreshed['state']['name']!r}; "
        f"worktree preserved at {worktree_path}.",
        file=sys.stderr,
    )
    return 1
