"""Per-cycle run-log artefact.

Each ``drain-cycle`` invocation produces a single JSON file at
``~/.drain-cycle/runs/<cycle-id>-<run-timestamp>.json`` capturing one
entry per attempted issue. The filename embeds the run-start timestamp
(UTC, ``%Y%m%dT%H%M%S%fZ``) so re-running ``drain-cycle`` against the
same cycle writes a new file instead of clobbering the prior one.
Downstream consumers glob the directory and group by ``cycle_id``
(carried inside each file).

* KR1 (completion %) is computed from ``entries[].final_linear_state``
  merged across every file sharing the same ``cycle_id``.
* Cycle-drain self-grading reads every file across cycles.
* The kill condition (KR1 < 50%) merges per-cycle then thresholds.

Schema:

::

    {
      "cycle_id":               "<linear-cycle-uuid>",
      "cycle_duration_seconds": <float>,
      "cycle_cost_usd":         <float>,
      "cycle_tokens_cumulative": <int>,
      "cycle_halt_reason":      null | "<cycle-budget halt line>",
      "entries":                [
        {
          "issue_identifier":   "ABA-NNN",
          "started_at":         "<iso-8601 UTC>",
          "finished_at":        "<iso-8601 UTC>",
          "exit_code":          <int>,
          "final_linear_state": "Done" | "Todo" | ...,
          "worktree_path":      "<absolute path>",
          "halt_reason":        null | "<orchestrator stderr halt line>",
          "duration_seconds":   <float>,
          "model":              null | "<resolved model id>",
          "usage":              null | {
            "input_tokens":               <int>,
            "output_tokens":              <int>,
            "cache_creation_input_tokens": <int>,
            "cache_read_input_tokens":    <int>,
            "cumulative":                 <int>,
            "peak_context":               <int>,
          },
          "cost_usd":           null | <float>,
          "num_turns":          null | <int>,
          "session_id":         null | "<uuid>",
          "is_error":           null | <bool>,
        },
        ...
      ],
    }

``halt_reason`` is ``null`` on Done entries unless worktree teardown
failed after the session completed — in that case the Done entry carries
the teardown error string and the drain continues. On the orchestrator's
halt entry it is the exact string also written to stderr — both surfaces
are produced by the same ``_halt_message`` helper in ``orchestrator.py``
so the on-disk and terminal values cannot drift. Non-last entries are
``null`` by construction: the orchestrator returns on first halt, so
anything before the halt is a Done entry.

The worker-usage fields (``model`` through ``is_error``) are populated
from the spawned session's stream-json events — see ``worker.py``.
Entries written before any session runs (pre-spawn resolution and
setup-failure halts) carry ``null`` for ``model`` / ``usage`` /
``cost_usd`` / ``num_turns`` / ``session_id`` / ``is_error``; every entry
keeps the same key set. ``usage.cumulative`` is the billed-token total
across turns; ``usage.peak_context`` is the largest single-turn context.
These fields are additive — ``grade.py`` reads only ``cycle_id`` and
``entries[].final_linear_state`` / ``exit_code``, so existing run logs
without them grade unchanged.

``cycle_cost_usd`` and ``cycle_tokens_cumulative`` are per-invocation
totals over ``entries`` (entries with ``null`` cost or usage contribute
zero), recomputed on every persist alongside ``cycle_duration_seconds``.

``cycle_halt_reason`` is ``null`` unless the orchestrator stopped the run
because a *cycle-wide* cap (tokens / cost / wall-clock) was breached — the
death-by-aggregate case where every issue stayed under its own per-issue
caps but their sum crossed the cycle budget. The breaching issue's own
entry is recorded normally (it ran to Done); this top-level field is the
record of why no further issue was attempted, carrying the same ``Halt:``
string printed to stderr. A per-issue breach, by contrast, lands in the
halting entry's ``halt_reason`` like any other halt.

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
from datetime import datetime, timezone
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
    artefact on disk, which is what kill-condition tooling needs to
    distinguish "drained nothing" from "never ran".

    The filename embeds a UTC run-start timestamp with microsecond
    resolution, so two ``RunLog`` instances on the same ``cycle_id``
    (re-running ``drain-cycle`` after a halt) write to two separate
    files instead of one clobbering the other.
    """

    cycle_id: str
    path: Path = field(init=False)
    entries: list[dict[str, Any]] = field(default_factory=list, init=False)
    cycle_halt_reason: str | None = field(default=None, init=False)

    def __post_init__(self) -> None:
        directory = runs_dir()
        directory.mkdir(parents=True, exist_ok=True)
        run_started_at = datetime.now(tz=timezone.utc)
        timestamp = run_started_at.strftime("%Y%m%dT%H%M%S%fZ")
        self.path = directory / f"{self.cycle_id}-{timestamp}.json"
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
        halt_reason: str | None = None,
        duration_seconds: float | None = None,
        model: str | None = None,
        usage: dict[str, int] | None = None,
        cost_usd: float | None = None,
        num_turns: int | None = None,
        session_id: str | None = None,
        is_error: bool | None = None,
    ) -> None:
        if duration_seconds is None:
            duration_seconds = (
                datetime.fromisoformat(finished_at)
                - datetime.fromisoformat(started_at)
            ).total_seconds()
        self.entries.append(
            {
                "issue_identifier": issue_identifier,
                "started_at": started_at,
                "finished_at": finished_at,
                "exit_code": exit_code,
                "final_linear_state": final_linear_state,
                "worktree_path": worktree_path,
                "halt_reason": halt_reason,
                "duration_seconds": duration_seconds,
                "model": model,
                "usage": usage,
                "cost_usd": cost_usd,
                "num_turns": num_turns,
                "session_id": session_id,
                "is_error": is_error,
            }
        )
        self._persist()

    def debug_path(self, issue_identifier: str) -> Path:
        """Path for one issue's opt-in ``--debug-file`` capture, beside the log.

        Named ``<run-log-stem>-<issue-identifier>.debug.log`` so each issue's
        startup diagnostics sit next to the run log they belong to and the
        run-start timestamp keeps re-runs from clobbering a prior capture.
        Only written when debug capture is enabled — see ``orchestrator.run``
        and ``docs/design-decisions.md`` §10.
        """
        return self.path.with_name(f"{self.path.stem}-{issue_identifier}.debug.log")

    def watch_path(self, issue_identifier: str) -> Path:
        """Path for one issue's watch log, beside the run log.

        Named ``<run-log-stem>-<issue-identifier>.watch.log`` so each issue's
        activity log sits next to the run log it belongs to. Written when
        ``--watch`` is active; see ``orchestrator.run`` and ``cli.py``.
        """
        return self.path.with_name(f"{self.path.stem}-{issue_identifier}.watch.log")

    def set_cycle_halt(self, reason: str) -> None:
        """Record why a cycle-wide cap stopped the run, and persist.

        Called by the orchestrator after the breaching issue's own entry is
        already written, so the file carries both the issue's normal entry
        and the cycle-level reason no further issue was attempted.
        """
        self.cycle_halt_reason = reason
        self._persist()

    def cycle_duration_seconds(self) -> float:
        if not self.entries:
            return 0.0
        starts = [datetime.fromisoformat(e["started_at"]) for e in self.entries]
        finishes = [datetime.fromisoformat(e["finished_at"]) for e in self.entries]
        return (max(finishes) - min(starts)).total_seconds()

    def cycle_cost_usd(self) -> float:
        return sum(
            e["cost_usd"] for e in self.entries if e.get("cost_usd") is not None
        )

    def cycle_tokens_cumulative(self) -> int:
        return sum(
            e["usage"]["cumulative"]
            for e in self.entries
            if e.get("usage") is not None
        )

    def _persist(self) -> None:
        payload = {
            "cycle_id": self.cycle_id,
            "cycle_duration_seconds": self.cycle_duration_seconds(),
            "cycle_cost_usd": self.cycle_cost_usd(),
            "cycle_tokens_cumulative": self.cycle_tokens_cumulative(),
            "cycle_halt_reason": self.cycle_halt_reason,
            "entries": self.entries,
        }
        self.path.write_text(json.dumps(payload, indent=2) + "\n")
