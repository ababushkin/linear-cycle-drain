"""``drain-cycle status`` — inspect an in-progress run from any terminal."""
from __future__ import annotations

from . import limits, progress


def run() -> int:
    """Read the active marker and print run status. Always exits 0."""
    marker = progress.read()
    if marker is None:
        print("No active drain-cycle run. Use `drain-cycle grade` to review completed runs.")
        return 0

    pid = marker.get("pid")
    if pid is not None and not progress.is_pid_alive(int(pid)):
        print(
            f"Stale marker: drain-cycle PID {pid} is no longer running.\n"
            f"Remove with: rm {progress.active_path()}"
        )
        return 0

    issue = marker.get("issue", {})
    prog = marker.get("progress", {})
    identifier = issue.get("identifier", "?")
    title = issue.get("title", "")
    repo = issue.get("repo", "")
    index = marker.get("index", "?")
    total = marker.get("total", "?")
    model_name = marker.get("model", "?")
    elapsed = float(prog.get("elapsed_seconds") or 0)
    turns = int(prog.get("turns") or 0)
    cumulative = int(prog.get("cumulative_tokens") or 0)
    peak = int(prog.get("peak_context_tokens") or 0)
    cost = prog.get("cost_usd")

    try:
        lim = limits.load()
    except limits.LimitsConfigError:
        lim = limits.Limits()

    tok_cap = _cap(cumulative, lim.per_issue_tokens, fmt=progress.fmt_tokens)
    cost_cap = _cap(cost, lim.per_issue_cost_usd, fmt=lambda x: f"${x:,.2f}")

    cost_display = f"${cost:,.2f}" if cost is not None else "—"

    print(f"drain-cycle: {identifier} [{index}/{total}] — {title}")
    print(f"  repo:    {repo}")
    print(f"  model:   {model_name}")
    print(f"  elapsed: {progress.fmt_elapsed(elapsed)}")
    print(f"  turns:   {turns}")
    print(
        f"  tokens:  {progress.fmt_tokens(cumulative)} cumulative"
        f"  {progress.fmt_tokens(peak)} peak{tok_cap}"
    )
    print(f"  cost:    {cost_display}{cost_cap}")
    return 0


def _cap(value: float | None, cap: float | None, *, fmt) -> str:
    """Return a cap annotation like `` [cap: 8M ⚠]`` or empty string."""
    if cap is None:
        return ""
    cap_str = fmt(cap)
    if value is not None and value >= cap:
        return f" [cap: {cap_str} ⚠]"
    return f" [cap: {cap_str}]"
