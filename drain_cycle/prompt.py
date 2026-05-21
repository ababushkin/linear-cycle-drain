"""Prompt builder for spawned ``claude -p`` sessions.

Walking-skeleton placeholder (Task 1 / ABA-198) — full template lands in
Task 4 / ABA-201.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any


def build(issue: dict[str, Any], worktree: Path) -> str:
    """Render the minimal prompt for the walking skeleton.

    The tail line is the load-bearing instruction: it tells the agent how
    the orchestrator knows the task is done.
    """
    title = issue.get("title", "")
    description = issue.get("description") or ""
    return (
        f"# {title}\n\n"
        f"{description}\n\n"
        "when complete, move this issue to Done via Linear MCP\n"
    )
