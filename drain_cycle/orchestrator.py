"""Drain a cycle by iterating over its sorted Todo/Backlog issues.

Task 3 / ABA-200 turns the walking skeleton into a real loop. Halt-on-not-Done
(ABA-202), the orchestrator-owned Todo→In-Progress transition (ABA-209), the
run-log artefact (US-C / ABA-215), and the inspectable-halt UX (US-B / ABA-212
+ ABA-213) all live here. The halt-message helper ``_halt_message`` is the
single source of truth for the operator-facing halt string — emitted both on
stderr and into the run-log entry's ``halt_reason`` field.
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
_ISSUE_TIMEOUT_SECONDS = 3600.0
"""Outer cap on one spawned ``claude -p`` session. A session that exceeds
this converts to a halt — same exit-1 + run-log entry + Halt: stderr line
as any other halt — so a hung agent advances the cycle to operator
attention instead of stalling indefinitely. Sized for one long unattended
issue; raise locally if a single issue legitimately takes longer."""


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _halt_message(identifier: str, state_name: str, worktree_path: Path) -> str:
    """Single source of truth for the halt UX (ABA-212 / ABA-213).

    The same string lands on stderr (the operator's grep anchor) and in
    the run-log entry's ``halt_reason`` field — so US-D / kill-condition
    tooling reads the same human-readable explanation the operator saw
    at halt time.
    """
    return f"Halt: {identifier} (final state: {state_name}) at {worktree_path}"


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

        started_at = _now_iso()
        try:
            worktree_path = worktree.add(repo, identifier)
            # Orchestrator owns the Todo→In Progress half so the lifecycle
            # doesn't depend on the spawned agent's compliance (ABA-209). The
            # agent still owns the …→Done half via Linear MCP — see prompt.py
            # tail.
            linear.set_state(issue["id"], _IN_PROGRESS_STATE_NAME)
        except Exception as exc:
            # Convert any pre-spawn failure into a recorded halt rather than
            # a traceback: write a run-log entry with the planned worktree
            # path, print the halt message, exit non-zero. Subsequent issues
            # are not attempted — same contract as a spawn-time halt.
            planned_path = repo / worktree.WORKTREE_DIR / identifier
            state_name = issue["state"]["name"]
            halt_reason = (
                f"{_halt_message(identifier, state_name, planned_path)}"
                f" — setup failed: {exc}"
            )
            log.append_entry(
                issue_identifier=identifier,
                started_at=started_at,
                finished_at=_now_iso(),
                exit_code=-1,
                final_linear_state=state_name,
                worktree_path=str(planned_path),
                halt_reason=halt_reason,
            )
            print(halt_reason, file=sys.stderr)
            return 1

        agent_prompt = prompt.build(issue, worktree_path)

        try:
            result = subprocess.run(
                [*_CLAUDE_CMD, agent_prompt],
                cwd=worktree_path,
                check=False,
                timeout=_ISSUE_TIMEOUT_SECONDS,
            )
        except subprocess.TimeoutExpired:
            state_name = issue["state"]["name"]
            halt_reason = (
                f"{_halt_message(identifier, state_name, worktree_path)}"
                f" — claude -p exceeded {_ISSUE_TIMEOUT_SECONDS:.0f}s timeout"
            )
            log.append_entry(
                issue_identifier=identifier,
                started_at=started_at,
                finished_at=_now_iso(),
                exit_code=-1,
                final_linear_state=state_name,
                worktree_path=str(worktree_path),
                halt_reason=halt_reason,
            )
            print(halt_reason, file=sys.stderr)
            return 1
        finished_at = _now_iso()

        refreshed = linear.get_issue(issue["id"])
        state_name = refreshed["state"]["name"]
        is_done = refreshed["state"]["type"] == _DONE_STATE_TYPE
        halt_reason = (
            None if is_done else _halt_message(identifier, state_name, worktree_path)
        )
        # Append unconditionally for every attempted issue (ABA-216); the
        # halt branch's `halt_reason` carries the same string also printed
        # to stderr below so the on-disk and terminal surfaces cannot
        # drift (ABA-213).
        log.append_entry(
            issue_identifier=identifier,
            started_at=started_at,
            finished_at=finished_at,
            exit_code=result.returncode,
            final_linear_state=state_name,
            worktree_path=str(worktree_path),
            halt_reason=halt_reason,
        )

        if is_done:
            worktree.remove(repo, worktree_path)
            print(f"drain-cycle: {identifier} done; worktree removed.", file=sys.stderr)
            continue

        print(halt_reason, file=sys.stderr)
        return 1

    return 0
