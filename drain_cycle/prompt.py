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


_TAIL = "when complete, move this issue to Done via Linear MCP"


def build(issue: dict[str, Any], worktree: Path) -> str:
    title = issue.get("title", "")
    description = issue.get("description") or ""
    identifier = issue.get("identifier", "")

    preamble = (
        "---\n\n"
        "Execution instructions:\n"
        f"- Working directory: {worktree}\n"
        "- Base branch: main\n"
        f"- When done, transition issue {identifier} to Done in Linear via "
        "the Linear MCP server (`mcp__claude_ai_Linear__save_issue` with "
        'state: "Done").\n'
    )

    return (
        f"# {title}\n\n"
        f"{description}\n\n"
        f"{preamble}\n"
        f"{_TAIL}\n"
    )
