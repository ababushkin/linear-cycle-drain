"""Drain a cycle by iterating over its sorted Todo/Backlog issues.

Halt-on-not-Done, the orchestrator-owned Todo→In-Progress transition, the
run-log artefact, and the inspectable-halt UX all live here. The halt-message
helper ``_halt_message`` is the single source of truth for the operator-facing
halt string — emitted both on stderr and into the run-log entry's
``halt_reason`` field.
"""
from __future__ import annotations

import os
import sys
from datetime import datetime, timezone
from pathlib import Path

from . import linear, model, progress, prompt, runlog, worker, worktree
from .limits import Limits, check_cycle
from .linear import DependencyCycleError
from .repos import RepoResolutionError, Repos

_DONE_STATE_TYPE = "completed"
_IN_PROGRESS_STATE_NAME = "In Progress"
_CLAUDE_CMD = ["claude", "-p", "--dangerously-skip-permissions"]
_DEBUG_ENV_VAR = "DRAIN_CYCLE_DEBUG"
"""Opt-in switch for per-issue ``--debug-file`` capture. Any non-empty value
turns it on; the worker then writes each session's startup diagnostics
(settings sources, plugins, MCP servers, hooks) beside the run log. Off by
default — the diagnostic exists for one-shot investigation, not steady state.
See ``docs/design-decisions.md`` §10."""
_UNRESOLVED_WORKTREE_DISPLAY = "<unresolved>"
"""Worktree-path placeholder for the pre-spawn resolution-halt path.
No path has been chosen yet — the issue couldn't be mapped to a target
repo — so the run-log entry and stderr halt line carry this marker
rather than a misleading fake path."""


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _halt_message(identifier: str, state_name: str, worktree_path: Path) -> str:
    """Single source of truth for the halt UX.

    The same string lands on stderr (the operator's grep anchor) and in
    the run-log entry's ``halt_reason`` field — so kill-condition tooling
    reads the same human-readable explanation the operator saw at halt
    time.
    """
    return f"Halt: {identifier} (final state: {state_name}) at {worktree_path}"


def _revert_to_pre_halt_state(
    issue_id: str, *, target_state_name: str, pre_revert_state_name: str
) -> tuple[str, str | None]:
    """Restore Linear state on halt; return ``(state_to_report, error_msg)``.

    The orchestrator transitions issues Todo→In Progress before spawning the
    agent. When the run halts, that In-Progress flag leaves the issue outside
    ``_PENDING_STATE_TYPES`` so a re-run silently skips it. This helper
    reverses the transition and re-fetches to confirm.

    On revert success, returns the refreshed state name and ``None``.
    On revert failure, returns ``pre_revert_state_name`` (the state the
    issue is actually still in, so the operator can find it) plus the
    exception message — non-fatal. A failed refresh after a
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


def _worker_log_fields(result: worker.WorkerResult) -> dict[str, object]:
    """Map a ``WorkerResult`` onto the run-log entry's usage fields.

    Shared by all three worker-backed ``append_entry`` calls (timeout
    halt, Done, not-Done halt) so the recorded usage shape can't drift
    between branches.
    """
    return {
        "duration_seconds": result.duration_seconds,
        "model": result.model,
        "usage": result.usage,
        "cost_usd": result.cost_usd,
        "num_turns": result.num_turns,
        "session_id": result.session_id,
        "is_error": result.is_error,
    }


def _debug_enabled() -> bool:
    """Whether per-issue ``--debug-file`` capture is switched on.

    Read from the environment (``os.environ`` already carries any
    ``~/.drain-cycle/.env`` value loaded at CLI startup), so the operator
    turns it on for one investigative run with ``DRAIN_CYCLE_DEBUG=1
    drain-cycle`` and leaves it off otherwise.
    """
    return bool(os.environ.get(_DEBUG_ENV_VAR))


def run(repos: Repos, limits: Limits | None = None) -> int:
    if limits is None:
        limits = Limits()
    debug = _debug_enabled()
    cycle_id = linear.current_cycle_id()
    log = runlog.RunLog(cycle_id=cycle_id)
    try:
        plan = linear.pending_issues(cycle_id)
    except DependencyCycleError as exc:
        halt_reason = f"Halt: {exc}"
        log.set_cycle_halt(halt_reason)
        print(halt_reason, file=sys.stderr)
        return 1

    if not plan.order and not plan.deferred:
        print(f"Cycle {cycle_id} has no Todo/Backlog issues — nothing to do.")
        return 0

    for deferred in plan.deferred:
        issue = deferred["issue"]
        blocker_id = deferred["blocker_identifier"]
        blocker_state = deferred["blocker_state_type"]
        print(
            f"drain-cycle: deferred {issue['identifier']}"
            f" — blocked by {blocker_id} ({blocker_state})",
            file=sys.stderr,
        )

    if not plan.order:
        return 0

    total = len(plan.order)
    for index, issue in enumerate(plan.order):
        identifier = issue["identifier"]
        print(f"drain-cycle: picked {identifier}: {issue['title']}", file=sys.stderr)

        started_at = _now_iso()
        try:
            target_repo = repos.resolve(issue)
        except RepoResolutionError as exc:
            # Pre-spawn resolution halt: no Linear state was moved, so no
            # revert is attempted. The worktree path is the ``<unresolved>``
            # placeholder since no repo was chosen.
            state_name = issue["state"]["name"]
            halt_reason = (
                f"{_halt_message(identifier, state_name, Path(_UNRESOLVED_WORKTREE_DISPLAY))}"
                f" — {exc}"
            )
            log.append_entry(
                issue_identifier=identifier,
                started_at=started_at,
                finished_at=_now_iso(),
                exit_code=-1,
                final_linear_state=state_name,
                worktree_path=_UNRESOLVED_WORKTREE_DISPLAY,
                halt_reason=halt_reason,
            )
            print(halt_reason, file=sys.stderr)
            return 1

        try:
            worktree_path = worktree.add(target_repo, identifier)
            # A worktree checks out only tracked files, so gitignored
            # project config (.claude/, .mcp.json) is absent. Symlink it in
            # so the worker loads the same settings/hooks/agents/skills/MCP
            # as an interactive session at the repo root.
            worktree.link_project_config(
                target_repo, worktree_path, repos.worktree_config_paths
            )
            # Orchestrator owns the Todo→In Progress half so the lifecycle
            # doesn't depend on the spawned agent's compliance. The agent
            # still owns the …→Done half via Linear MCP — see prompt.py tail.
            linear.set_state(issue["id"], _IN_PROGRESS_STATE_NAME)
        except Exception as exc:
            # Convert any pre-spawn failure into a recorded halt rather than
            # a traceback: write a run-log entry with the planned worktree
            # path, print the halt message, exit non-zero. Subsequent issues
            # are not attempted — same contract as a spawn-time halt.
            planned_path = target_repo / worktree.WORKTREE_DIR / identifier
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
        worker_model = model.resolve(issue)

        debug_file = log.debug_path(identifier) if debug else None
        if debug_file is not None:
            print(
                f"drain-cycle: {identifier} debug capture → {debug_file}",
                file=sys.stderr,
            )

        marker: dict = {
            "pid": os.getpid(),
            "cycle_id": cycle_id,
            "run_log_path": str(log.path),
            "issue": {
                "identifier": identifier,
                "title": issue["title"],
                "repo": target_repo.name,
                "worktree_path": str(worktree_path),
            },
            "model": worker_model,
            "started_at": started_at,
            "index": index + 1,
            "total": total,
            "progress": {},
        }
        progress.write(marker)

        def _make_on_progress(m: dict, ident: str):
            def _cb(
                turns: int,
                cumulative_tokens: int,
                peak_context_tokens: int,
                cost_usd: float | None,
                elapsed_seconds: float,
            ) -> None:
                m["progress"] = {
                    "turns": turns,
                    "cumulative_tokens": cumulative_tokens,
                    "peak_context_tokens": peak_context_tokens,
                    "cost_usd": cost_usd,
                    "elapsed_seconds": elapsed_seconds,
                    "last_event_at": _now_iso(),
                }
                progress.write(m)
                print(
                    progress.format_progress_line(
                        ident,
                        turns,
                        cumulative_tokens,
                        peak_context_tokens,
                        cost_usd,
                        elapsed_seconds,
                    ),
                    file=sys.stderr,
                )
            return _cb

        try:
            result = worker.run_issue(
                claude_cmd=_CLAUDE_CMD,
                model=worker_model,
                prompt=agent_prompt,
                cwd=worktree_path,
                token_limit=limits.per_issue_tokens,
                time_limit_seconds=limits.per_issue_seconds,
                cost_limit_usd=limits.per_issue_cost_usd,
                debug_file=debug_file,
                on_progress=_make_on_progress(marker, identifier),
            )
        finally:
            progress.clear()
        finished_at = _now_iso()

        if result.breach is not None:
            # The worker crossed a per-issue cap (tokens or time) and was
            # process-group killed (grandchildren reaped). Same revert + halt
            # contract as a not-Done exit, with the recorded usage of the
            # killed session and the breached cap named in the halt reason.
            original_state_name = issue["state"]["name"]
            effective_state, revert_error = _revert_to_pre_halt_state(
                issue["id"],
                target_state_name=original_state_name,
                pre_revert_state_name=_IN_PROGRESS_STATE_NAME,
            )
            halt_reason = (
                f"{_halt_message(identifier, effective_state, worktree_path)}"
                f" — {result.breach.describe()}"
            )
            if revert_error is not None:
                halt_reason += (
                    f"; revert to {original_state_name!r} failed: {revert_error}"
                )
            log.append_entry(
                issue_identifier=identifier,
                started_at=started_at,
                finished_at=finished_at,
                exit_code=result.exit_code,
                final_linear_state=effective_state,
                worktree_path=str(worktree_path),
                halt_reason=halt_reason,
                **_worker_log_fields(result),
            )
            print(halt_reason, file=sys.stderr)
            return 1

        refreshed = linear.get_issue(issue["id"])
        post_spawn_state = refreshed["state"]["name"]
        is_done = refreshed["state"]["type"] == _DONE_STATE_TYPE

        if is_done:
            remove_error: str | None = None
            try:
                worktree.remove(target_repo, worktree_path)
            except RuntimeError as exc:
                remove_error = str(exc)
                print(
                    f"drain-cycle: {identifier}: worktree teardown failed: {exc}",
                    file=sys.stderr,
                )
            # Append unconditionally for every attempted issue.
            log.append_entry(
                issue_identifier=identifier,
                started_at=started_at,
                finished_at=finished_at,
                exit_code=result.exit_code,
                final_linear_state=post_spawn_state,
                worktree_path=str(worktree_path),
                halt_reason=remove_error,
                **_worker_log_fields(result),
            )
            if remove_error is None:
                print(f"drain-cycle: {identifier} done; worktree removed.", file=sys.stderr)

            # Cycle-wide circuit breaker: every issue may stay under its own
            # per-issue caps while their sum drains the quota. Check the
            # running totals (which now include this Done issue) before
            # spawning the next one; on breach, stop the cycle.
            cycle_breach = check_cycle(
                limits,
                tokens=log.cycle_tokens_cumulative(),
                cost_usd=log.cycle_cost_usd(),
                seconds=log.cycle_duration_seconds(),
            )
            if cycle_breach is not None:
                halt_reason = f"Halt: {cycle_breach.describe()}"
                log.set_cycle_halt(halt_reason)
                print(halt_reason, file=sys.stderr)
                return 1
            continue

        # Not-Done halt: revert to the pre-halt state so a re-run picks
        # this issue back up instead of silently skipping it.
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
        # so the on-disk and terminal surfaces cannot drift.
        log.append_entry(
            issue_identifier=identifier,
            started_at=started_at,
            finished_at=finished_at,
            exit_code=result.exit_code,
            final_linear_state=effective_state,
            worktree_path=str(worktree_path),
            halt_reason=halt_reason,
            **_worker_log_fields(result),
        )
        print(halt_reason, file=sys.stderr)
        return 1

    return 0
