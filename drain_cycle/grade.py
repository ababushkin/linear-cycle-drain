"""Self-grade a series of drain-cycle runs (US-D / ABA-197).

Reads every ``~/.drain-cycle/runs/*.json`` produced by US-C / ABA-196 and
emits a human-readable health read. This walking skeleton (Task 1 /
ABA-217) wires up CLI dispatch, directory enumeration, JSON parse, and
both exit paths (happy + empty/missing). Subsequent sub-issues fill in
the per-cycle, across-cycles, and verdict sections.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

from . import runlog


def default_runs_dir() -> Path:
    return runlog.runs_dir()


def run(runs_dir: Path) -> int:
    files = sorted(runs_dir.glob("*.json")) if runs_dir.is_dir() else []
    if not files:
        print(
            f"drain-cycle grade: no run logs found at {runs_dir}",
            file=sys.stderr,
        )
        return 1

    for path in files:
        payload = json.loads(path.read_text())
        print(payload["cycle_id"])
    return 0
