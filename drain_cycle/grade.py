"""Self-grade a series of drain-cycle runs (US-D / ABA-197).

Reads every ``~/.drain-cycle/runs/*.json`` produced by US-C / ABA-196 and
emits a human-readable health read:

1. Per-cycle section (Task 2 / ABA-219): cycle_id, attempted count,
   integer completion %, and halted entries as
   ``<identifier>: (<final_linear_state>, <exit_code>)``.
2. Across-cycles section (Task 3 / ABA-220): trend label over the last
   ``_TREND_WINDOW`` cycles + recurrent ``(final_linear_state,
   exit_code)`` tuples appearing in ≥2 of those cycles.
3. Verdict section (Task 4 / ABA-221): one of ``OK`` / ``WATCH`` /
   ``KILL`` against the most-recent cycle's completion %. KILL prints
   a reminder that the second clause of the project's kill condition
   ("not addressable within one cycle of fixes") is operator
   judgement, not machine-evaluated.

Run logs are grouped by ``cycle_id`` because one cycle can produce
multiple files when ``drain-cycle`` is re-run against the same cycle
after a halt (ABA-230). Chronological ordering uses the earliest
filename per cycle.
"""
from __future__ import annotations

import json
import sys
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import runlog

_TREND_WINDOW = 3
"""Number of most-recent cycles considered by the trend + recurrent-tuple
analysis (Task 3 / ABA-220). Pinned here; not configurable by the CLI."""

_OK_THRESHOLD = 80
_WATCH_THRESHOLD = 50
"""Verdict bands (Task 4 / ABA-221). Boundaries are inclusive on the
lower edge: ≥80 → OK, 50–79 → WATCH, <50 → KILL."""

_KILL_SECOND_CLAUSE = (
    "Reminder: the second clause of the kill condition — \"not addressable "
    "within one cycle of fixes\" — is operator judgement, not machine-evaluated."
)


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


def _trend_label(percents: list[int]) -> str:
    """Strict-monotonic over the window → improving/regressing; else flat.

    Single-cycle or empty windows are "flat" by definition (no direction
    can be inferred from one or zero points). Two-cycle windows still
    apply the strict-monotonic rule (50 → 60 is improving; 50 → 50 is
    flat because the inequality is strict).
    """
    if len(percents) < 2:
        return "flat"
    if all(a < b for a, b in zip(percents, percents[1:])):
        return "improving"
    if all(a > b for a, b in zip(percents, percents[1:])):
        return "regressing"
    return "flat"


def _recurrent_tuples(cycles: list[_Cycle]) -> list[tuple[tuple[str, int], int]]:
    """Tuples appearing in ≥2 of the supplied cycles, with cycle-count.

    Counted per cycle, not per entry: if a tuple appears three times
    inside one cycle and once in another, it counts as 2 cycles (not 4).
    Sorted by descending count then by the tuple itself for determinism.
    """
    counter: Counter[tuple[str, int]] = Counter()
    for cycle in cycles:
        seen_in_cycle: set[tuple[str, int]] = set()
        for entry in cycle.entries:
            key = (entry["final_linear_state"], entry["exit_code"])
            seen_in_cycle.add(key)
        counter.update(seen_in_cycle)
    recurrent = [(tup, count) for tup, count in counter.items() if count >= 2]
    recurrent.sort(key=lambda item: (-item[1], item[0]))
    return recurrent


def _render_across_cycles(cycles: list[_Cycle]) -> str:
    window = cycles[-_TREND_WINDOW:]
    percents = [c.completion_percent for c in window]
    lines = [
        f"window: last {len(window)} of {len(cycles)} cycle(s)"
        f" (max {_TREND_WINDOW})",
        f"trend: {_trend_label(percents)}",
    ]
    recurrent = _recurrent_tuples(window)
    if not recurrent:
        lines.append("recurrent tuples: none")
    else:
        lines.append("recurrent tuples:")
        for (state, exit_code), count in recurrent:
            lines.append(f"  ({state}, {exit_code}) x {count}")
    return "\n".join(lines)


def _render_verdict(cycles: list[_Cycle]) -> str:
    most_recent = cycles[-1]
    pct = most_recent.completion_percent
    if pct >= _OK_THRESHOLD:
        label = "OK"
    elif pct >= _WATCH_THRESHOLD:
        label = "WATCH"
    else:
        label = "KILL"
    lines = [f"verdict: {label} (most-recent cycle completion: {pct}%)"]
    if label == "KILL":
        lines.append(_KILL_SECOND_CLAUSE)
    return "\n".join(lines)


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

    print("== Across cycles ==")
    print()
    print(_render_across_cycles(cycles))
    print()

    print("== Verdict ==")
    print()
    print(_render_verdict(cycles))
    print()
    return 0
