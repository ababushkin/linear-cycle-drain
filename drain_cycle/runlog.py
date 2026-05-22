"""Per-cycle run-log artefact (US-C / ABA-196).

Each ``drain-cycle`` invocation produces a single JSON file at
``~/.drain-cycle/runs/<cycle-id>.json`` capturing one entry per attempted
issue. The file is the input every downstream consumer reads:

* KR1 (completion %) is computed from ``entries[].final_linear_state``.
* US-D (cycle-drain self-grading) reads the same file across cycles.
* The kill condition (KR1 < 50%) is a one-file grep.

Schema:

::

    {
      "cycle_id":               "<linear-cycle-uuid>",
      "cycle_duration_seconds": <float>,
      "entries":                [
        {
          "issue_identifier":   "ABA-NNN",
          "started_at":         "<iso-8601 UTC>",
          "finished_at":        "<iso-8601 UTC>",
          "exit_code":          <int>,
          "final_linear_state": "Done" | "Todo" | ...,
          "worktree_path":      "<absolute path>",
        },
        ...
      ],
    }

``cycle_duration_seconds`` is computed automatically on every persist as
``max(finished_at) - min(started_at)`` across ``entries`` (``0.0`` when
empty). This is the KR2 grading proxy — "how long was the agent doing
execution for me unattended" stands in for "operator hands-off time" —
and replaces the originally specified ``time_spent`` block, which
required manual self-report at cycle close and was never going to be
filled in.

Persistence is incremental: ``append_entry`` re-serialises the whole dict
on every call, so a mid-run crash still leaves a well-formed file with
every issue attempted so far. Cycle scale (≤ 15 issues) makes the cost
irrelevant.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any


def runs_dir() -> Path:
    """Return the directory where per-cycle run logs are written.

    Resolved on every call (not module-import time) so tests can redirect
    via ``monkeypatch.setenv("HOME", ...)`` after the module is imported.
    """
    return Path.home() / ".drain-cycle" / "runs"


@dataclass
class RunLog:
    """Per-cycle run-log file. Construct once at orchestrator start.

    Writing the initial ``{cycle_id, cycle_duration_seconds: 0.0,
    entries: []}`` shell in ``__post_init__`` means that even a zero-issue
    cycle (or a crash before the first ``append_entry``) leaves the
    artefact on disk, which is what US-D / kill-condition tooling needs
    to distinguish "drained nothing" from "never ran".
    """

    cycle_id: str
    path: Path = field(init=False)
    entries: list[dict[str, Any]] = field(default_factory=list, init=False)

    def __post_init__(self) -> None:
        directory = runs_dir()
        directory.mkdir(parents=True, exist_ok=True)
        self.path = directory / f"{self.cycle_id}.json"
        self._persist()

    def append_entry(
        self,
        *,
        issue_identifier: str,
        started_at: str,
        finished_at: str,
        exit_code: int,
        final_linear_state: str,
        worktree_path: str,
    ) -> None:
        self.entries.append(
            {
                "issue_identifier": issue_identifier,
                "started_at": started_at,
                "finished_at": finished_at,
                "exit_code": exit_code,
                "final_linear_state": final_linear_state,
                "worktree_path": worktree_path,
            }
        )
        self._persist()

    def _persist(self) -> None:
        payload = {
            "cycle_id": self.cycle_id,
            "cycle_duration_seconds": self._cycle_duration_seconds(),
            "entries": self.entries,
        }
        self.path.write_text(json.dumps(payload, indent=2) + "\n")

    def _cycle_duration_seconds(self) -> float:
        if not self.entries:
            return 0.0
        starts = [datetime.fromisoformat(e["started_at"]) for e in self.entries]
        finishes = [datetime.fromisoformat(e["finished_at"]) for e in self.entries]
        return (max(finishes) - min(starts)).total_seconds()
