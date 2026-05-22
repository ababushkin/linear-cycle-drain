"""Drain a cycle by iterating over its sorted Todo/Backlog issues.

Task 3 / ABA-200 turns the walking skeleton into a real loop. Halt-on-not-Done
polish (Task 5 / ABA-202), the orchestrator-owned Todo→In-Progress transition
(Task 7 / ABA-209), and the run-log artefact (US-C / ABA-215) land in later
slices; the non-Done branch in this file is deliberately minimal — it
guarantees we do not silently advance, and leaves formatting to US-B.
"""
from __future__ import annotations

import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

from . import linear, prompt, runlog, worktree

_DONE_STATE_TYPE = "completed"
_IN_PROGRESS_STATE_NAME = "In Progress"
_CLAUDE_CMD = ["claude", "-p", "--dangerously-skip-permissions"]


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def run() -> int:
    repo = Path.cwd()
    cycle_id = linear.current_cycle_id()
    log = runlog.RunLog(cycle_id=cycle_id)
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

        started_at = _now_iso()
        result = subprocess.run(
            [*_CLAUDE_CMD, agent_prompt],
            cwd=worktree_path,
            check=False,
        )
        finished_at = _now_iso()

        refreshed = linear.get_issue(issue["id"])
        # Append before the Done/halt branching so the halt path (ABA-216)
        # also lands an entry without restructuring the orchestrator.
        log.append_entry(
            issue_identifier=identifier,
            started_at=started_at,
            finished_at=finished_at,
            exit_code=result.returncode,
            final_linear_state=refreshed["state"]["name"],
            worktree_path=str(worktree_path),
        )

        state_type = refreshed["state"]["type"]
        if state_type == _DONE_STATE_TYPE:
            worktree.remove(repo, worktree_path)
            print(f"drain-cycle: {identifier} done; worktree removed.", file=sys.stderr)
            continue

        # Spec'd halt line (US-B / ABA-212): the `Halt:` token is the unique
        # anchor an operator can grep stderr for; identifier + state name +
        # absolute worktree path are all that's needed to `cd` and inspect.
        print(
            f"Halt: {identifier} (final state: {refreshed['state']['name']}) "
            f"at {worktree_path}",
            file=sys.stderr,
        )
        return 1

    return 0
