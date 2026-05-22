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
      "cycle_id":  "<linear-cycle-uuid>",
      "time_spent": null,
      "entries":   [
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

``time_spent`` stays ``null`` for the tool's lifetime — the operator
self-reports ``scoping_hours`` / ``validation_hours`` / ``execution_hours``
at cycle close. The tool deliberately does not measure time (ABA-196
out-of-scope).

Persistence is incremental: ``append_entry`` re-serialises the whole dict
on every call, so a mid-run crash still leaves a well-formed file with
every issue attempted so far. Cycle scale (≤ 15 issues) makes the cost
irrelevant.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
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

    Writing the initial ``{cycle_id, time_spent: null, entries: []}`` shell
    in ``__post_init__`` means that even a zero-issue cycle (or a crash
    before the first ``append_entry``) leaves the artefact on disk, which
    is what US-D / kill-condition tooling needs to distinguish "drained
    nothing" from "never ran".
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
            "time_spent": None,
            "entries": self.entries,
        }
        self.path.write_text(json.dumps(payload, indent=2) + "\n")
