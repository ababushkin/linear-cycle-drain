"""Drain a cycle by iterating over its sorted Todo/Backlog issues.

Task 3 / ABA-200 turns the walking skeleton into a real loop. Halt-on-not-Done
polish (Task 5 / ABA-202) and the run-log artefact (US-C / ABA-196) land in
later slices; the non-Done branch in this file is deliberately minimal — it
guarantees we do not silently advance, and leaves formatting to Task 5.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from . import linear, prompt, worktree

_DONE_STATE_TYPE = "completed"
_IN_PROGRESS_STATE_NAME = "In Progress"
_CLAUDE_CMD = ["claude", "-p", "--dangerously-skip-permissions"]


def run() -> int:
    repo = Path.cwd()
    cycle_id = linear.current_cycle_id()
    issues = linear.pending_issues(cycle_id)
    if not issues:
        print(f"Cycle {cycle_id} has no Todo/Backlog issues — nothing to do.")
        return 0

    for issue in issues:
        identifier = issue["identifier"]
        print(f"drain-cycle: picked {identifier}: {issue['title']}", file=sys.stderr)

        worktree_path = worktree.add(repo, identifier)
        # Orchestrator owns the Todo→In Progress half so the lifecycle doesn't
        # depend on the spawned agent's compliance (ABA-209). The agent still
        # owns the …→Done half via Linear MCP — see prompt.py tail.
        linear.set_state(issue["id"], _IN_PROGRESS_STATE_NAME)
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
            continue

        # Halt UX (formatted message, deliberate preservation) is US-B / ABA-195.
        # This slice only guarantees: do not silently advance, leave worktree on disk.
        print(
            f"drain-cycle: {identifier} ended in state {refreshed['state']['name']!r}; "
            f"worktree preserved at {worktree_path}.",
            file=sys.stderr,
        )
        return 1

    return 0
