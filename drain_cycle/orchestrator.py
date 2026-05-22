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


def _revert_to_pre_halt_state(
    issue_id: str, *, target_state_name: str, pre_revert_state_name: str
) -> tuple[str, str | None]:
    """Restore Linear state on halt; return ``(state_to_report, error_msg)``.

    The orchestrator transitions issues Todo→In Progress before spawning the
    agent (ABA-209). When the run halts, that In-Progress flag leaves the
    issue outside ``_PENDING_STATE_TYPES`` so a re-run silently skips it
    (ABA-229). This helper reverses the transition and re-fetches to confirm.

    On revert success, returns the refreshed state name and ``None``.
    On revert failure, returns ``pre_revert_state_name`` (the state the
    issue is actually still in, so the operator can find it) plus the
    exception message — non-fatal per AC4. A failed refresh after a
    successful revert falls back to ``target_state_name``, since we trust
    the mutation landed even if the read-back didn't.
    """
    try:
        linear.set_state(issue_id, target_state_name)
    except Exception as exc:
        return pre_revert_state_name, str(exc)
    try:
        refreshed = linear.get_issue(issue_id)
    except Exception:
        return target_state_name, None
    return refreshed["state"]["name"], None


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
            original_state_name = issue["state"]["name"]
            effective_state, revert_error = _revert_to_pre_halt_state(
                issue["id"],
                target_state_name=original_state_name,
                pre_revert_state_name=_IN_PROGRESS_STATE_NAME,
            )
            halt_reason = (
                f"{_halt_message(identifier, effective_state, worktree_path)}"
                f" — claude -p exceeded {_ISSUE_TIMEOUT_SECONDS:.0f}s timeout"
            )
            if revert_error is not None:
                halt_reason += (
                    f"; revert to {original_state_name!r} failed: {revert_error}"
                )
            log.append_entry(
                issue_identifier=identifier,
                started_at=started_at,
                finished_at=_now_iso(),
                exit_code=-1,
                final_linear_state=effective_state,
                worktree_path=str(worktree_path),
                halt_reason=halt_reason,
            )
            print(halt_reason, file=sys.stderr)
            return 1
        finished_at = _now_iso()

        refreshed = linear.get_issue(issue["id"])
        post_spawn_state = refreshed["state"]["name"]
        is_done = refreshed["state"]["type"] == _DONE_STATE_TYPE

        if is_done:
            # Append unconditionally for every attempted issue (ABA-216).
            log.append_entry(
                issue_identifier=identifier,
                started_at=started_at,
                finished_at=finished_at,
                exit_code=result.returncode,
                final_linear_state=post_spawn_state,
                worktree_path=str(worktree_path),
                halt_reason=None,
            )
            worktree.remove(repo, worktree_path)
            print(f"drain-cycle: {identifier} done; worktree removed.", file=sys.stderr)
            continue

        # Not-Done halt: revert to the pre-halt state so a re-run picks
        # this issue back up instead of silently skipping it (ABA-229).
        original_state_name = issue["state"]["name"]
        effective_state, revert_error = _revert_to_pre_halt_state(
            issue["id"],
            target_state_name=original_state_name,
            pre_revert_state_name=post_spawn_state,
        )
        halt_reason = _halt_message(identifier, effective_state, worktree_path)
        if revert_error is not None:
            halt_reason += (
                f" — revert to {original_state_name!r} failed: {revert_error}"
            )
        # halt_reason carries the same string also printed to stderr below
        # so the on-disk and terminal surfaces cannot drift (ABA-213).
        log.append_entry(
            issue_identifier=identifier,
            started_at=started_at,
            finished_at=finished_at,
            exit_code=result.returncode,
            final_linear_state=effective_state,
            worktree_path=str(worktree_path),
            halt_reason=halt_reason,
        )
        print(halt_reason, file=sys.stderr)
        return 1

    return 0
