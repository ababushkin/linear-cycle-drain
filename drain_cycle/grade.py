"""Self-grade a series of drain-cycle runs (US-D / ABA-197).

Reads every ``~/.drain-cycle/runs/*.json`` produced by US-C / ABA-196 and
emits a human-readable health read:

1. Per-cycle section (Task 2 / ABA-219): cycle_id, attempted count,
   integer completion %, and halted entries as
   ``<identifier>: (<final_linear_state>, <exit_code>)``.
2. Across-cycles section (Task 3 / ABA-220, pending): trend + recurrent
   failure-mode tuples.
3. Verdict section (Task 4 / ABA-221, pending): OK / WATCH / KILL.

Run logs are grouped by ``cycle_id`` because one cycle can produce
multiple files when ``drain-cycle`` is re-run against the same cycle
after a halt (ABA-230). Chronological ordering uses the earliest
filename per cycle.
"""
from __future__ import annotations

import json
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import runlog


def default_runs_dir() -> Path:
    return runlog.runs_dir()


@dataclass
class _Cycle:
    cycle_id: str
    entries: list[dict[str, Any]] = field(default_factory=list)
    earliest_filename: str = ""

    @property
    def attempted(self) -> int:
        return len(self.entries)

    @property
    def completion_percent(self) -> int:
        if not self.entries:
            return 0
        done = sum(1 for e in self.entries if e["final_linear_state"] == "Done")
        return round(done * 100 / self.attempted)

    @property
    def halted(self) -> list[dict[str, Any]]:
        return [e for e in self.entries if e["final_linear_state"] != "Done"]


def _collect_cycles(runs_dir: Path) -> list[_Cycle]:
    """Group run-log files by cycle_id, chronological by earliest filename.

    A cycle can span multiple files (ABA-230 — re-running drain-cycle on
    the same cycle writes a new file). Tie-break by filename is the rule
    pinned in ABA-219's scope; in practice cycles never tie because each
    cycle has its own UUID, but the rule is held for determinism if two
    cycles ever did share a min-filename timestamp.
    """
    files = sorted(runs_dir.glob("*.json"))
    cycles: dict[str, _Cycle] = {}
    for path in files:
        payload = json.loads(path.read_text())
        cycle_id = payload["cycle_id"]
        cycle = cycles.setdefault(cycle_id, _Cycle(cycle_id=cycle_id))
        cycle.entries.extend(payload.get("entries", []))
        if not cycle.earliest_filename or path.name < cycle.earliest_filename:
            cycle.earliest_filename = path.name
    return sorted(cycles.values(), key=lambda c: (c.earliest_filename, c.cycle_id))


def _render_per_cycle(cycle: _Cycle) -> str:
    lines = [
        f"cycle_id: {cycle.cycle_id}",
        f"  attempted: {cycle.attempted}",
        f"  completion: {cycle.completion_percent}%",
    ]
    if cycle.halted:
        lines.append("  halted:")
        for entry in cycle.halted:
            lines.append(
                f"    {entry['issue_identifier']}: "
                f"({entry['final_linear_state']}, {entry['exit_code']})"
            )
    return "\n".join(lines)


def run(runs_dir: Path) -> int:
    cycles = _collect_cycles(runs_dir) if runs_dir.is_dir() else []
    if not cycles:
        print(
            f"drain-cycle grade: no run logs found at {runs_dir}",
            file=sys.stderr,
        )
        return 1

    print("== Per-cycle ==")
    print()
    for cycle in cycles:
        print(_render_per_cycle(cycle))
        print()
    return 0
