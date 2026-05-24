"""Active-run marker: write/clear/read ``~/.drain-cycle/active.json``.

The orchestrator writes the marker just before spawning each worker and
clears it once the worker returns (try/finally, every exit path). A
``drain-cycle status`` in a second terminal reads the marker to show what
the run is doing. The file lives above ``runs/`` so ``grade``'s
``runs/*.json`` glob never sees it.

Schema::

    {
      "pid":          <int>,
      "cycle_id":     "<uuid>",
      "run_log_path": "<path>",
      "issue": {
        "identifier":    "ABA-NNN",
        "title":         "<str>",
        "repo":          "<repo-name>",
        "worktree_path": "<path>",
      },
      "model":      "<model-id>",
      "started_at": "<iso-8601 UTC>",
      "index":      <int>,          # 1-based position in this cycle's issue list
      "total":      <int>,          # total issues in this cycle
      "progress": {
        "turns":               <int>,
        "cumulative_tokens":   <int>,
        "peak_context_tokens": <int>,
        "cost_usd":            <float | null>,
        "elapsed_seconds":     <float>,
        "last_event_at":       "<iso-8601 UTC>",
      },
    }
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any


def active_path() -> Path:
    """Resolved per call so tests can redirect via ``HOME``."""
    return Path.home() / ".drain-cycle" / "active.json"


def write(data: dict[str, Any]) -> None:
    """Atomically write the active marker via temp-file rename."""
    path = active_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.parent / (path.name + ".tmp")
    tmp.write_text(json.dumps(data, indent=2) + "\n")
    tmp.rename(path)


def clear() -> None:
    """Remove the active marker; no-op if absent."""
    try:
        active_path().unlink()
    except FileNotFoundError:
        pass


def read() -> dict[str, Any] | None:
    """Read and parse the marker; ``None`` if absent or unreadable."""
    try:
        return json.loads(active_path().read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return None


def is_pid_alive(pid: int) -> bool:
    """Return ``True`` if the process exists (not a zombie)."""
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        # pid exists but we lack permission to signal it — still alive.
        return True


def fmt_tokens(n: int) -> str:
    """Human-readable token count: ``8.1M``, ``180k``, ``42``."""
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 1_000:
        return f"{n // 1_000}k"
    return str(n)


def fmt_elapsed(seconds: float) -> str:
    """Human-readable elapsed time: ``1h23m``, ``14m``, ``42s``."""
    s = int(seconds)
    if s >= 3600:
        return f"{s // 3600}h{(s % 3600) // 60}m"
    if s >= 60:
        return f"{s // 60}m"
    return f"{s}s"


def format_progress_line(
    identifier: str,
    turns: int,
    cumulative_tokens: int,
    peak_context_tokens: int,
    cost_usd: float | None,
    elapsed_seconds: float,
) -> str:
    """Compact single-line progress for stderr.

    Example: ``ABA-205 · turn 42 · 8.1M tok (peak 180k) · $12.30 · 14m``
    """
    cost = f"${cost_usd:,.2f}" if cost_usd is not None else "$?.??"
    return (
        f"{identifier} · turn {turns}"
        f" · {fmt_tokens(cumulative_tokens)} tok (peak {fmt_tokens(peak_context_tokens)})"
        f" · {cost} · {fmt_elapsed(elapsed_seconds)}"
    )
