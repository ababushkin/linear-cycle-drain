"""Prompt builder for spawned ``claude -p`` sessions.

The prompt is the entire contract between the orchestrator and the spawned
agent — there's no system prompt, no multi-turn loop. The four-segment
ordering below is load-bearing: the agent reads top-down, so context
(title + body) comes before instructions (preamble + tail), and the tail
line is last so it stays in the trailing-tokens window the model attends
to most strongly.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any


_TAIL = (
    "before marking Done: run /code-review-and-quality on the working-tree "
    "changes, fix Critical/Required findings, commit + push, then post a "
    "review-summary comment on the issue and transition to Done."
)


def _resume_directive(identifier: str) -> str:
    """Resume preamble for a worktree carrying prior committed work.

    Inserted as the first line inside the preamble (after the ``---``
    separator, before "Execution instructions:") so the agent reads it
    ahead of the procedure but ``_TAIL`` still holds the last-line
    position the four-segment ordering reserves for it.
    """
    return (
        f"Resuming issue {identifier}: this worktree carries prior committed "
        "work from an earlier session that was halted. Run "
        "`git log --oneline main..HEAD` and `git status` first to read what "
        "is already done, then continue from that point — do not restart "
        "from scratch.\n\n"
    )


def build(issue: dict[str, Any], worktree: Path, *, resumed: bool = False) -> str:
    title = issue.get("title", "")
    description = issue.get("description") or ""
    identifier = issue.get("identifier", "")

    resume_segment = _resume_directive(identifier) if resumed else ""
    preamble = (
        "---\n\n"
        f"{resume_segment}"
        "Execution instructions:\n"
        f"- Working directory: {worktree}\n"
        "- Base branch: main\n"
        f"- Completion sequence for issue {identifier} (run in this order, "
        "before marking Done):\n"
        "  1. Run `/code-review-and-quality` against the working-tree changes.\n"
        "  2. Fix any Critical or Required findings. Lower-severity findings "
        "are at your discretion.\n"
        "  3. Commit and push to main.\n"
        "  4. Post a short review-summary comment on the Linear issue via "
        "`mcp__claude_ai_Linear__save_comment` (count of findings by severity, "
        "fixed vs deferred).\n"
        "  5. Transition issue to Done via `mcp__claude_ai_Linear__save_issue` "
        '(state: "Done").\n'
    )

    return (
        f"# {title}\n\n"
        f"{description}\n\n"
        f"{preamble}\n"
        f"{_TAIL}\n"
    )
