"""Per-issue and per-cycle resource guardrails.

A spawned ``claude -p`` worker can run away — burning tokens, wall-clock,
and quota with no ceiling but the outer per-issue timeout. This module
defines the thresholds that bound that spend and the ``Breach`` value that
names which one was crossed.

**Two layers, belt and suspenders.**

* *Native belt* — ``per_issue_cost_usd`` is handed to ``claude`` as
  ``--max-budget-usd`` so the session self-terminates on cost without the
  orchestrator having to watch it.
* *Orchestrator suspenders* — ``per_issue_tokens`` and ``per_issue_seconds``
  are enforced by the worker against the live event stream: when either is
  crossed the worker SIGKILLs the session's process group (see
  ``worker.py``). The ``cycle_*`` caps are enforced by the orchestrator
  between issues via ``check_cycle`` — death-by-aggregate, where every issue
  is individually under its cap but their sum drains the quota, is exactly
  what the per-issue caps cannot catch.

Token count is the primary guardrail (a subscription user pays in tokens,
not dollars); cost rides alongside it.

**On/off per guardrail.** Any threshold may be ``None`` — that guardrail is
off. The defaults below are all live; an operator turns one off by setting
its key to ``null`` in ``~/.drain-cycle/limits.yml``.

**Load precedence.** Baked-in defaults, overlaid by ``limits.yml`` if it
exists: a key absent from the file keeps its default, an explicit ``null``
turns the guardrail off, a positive number overrides it. A present-but-
malformed file raises ``LimitsConfigError`` rather than silently falling
back to defaults — a typo that quietly disabled a cap the operator believed
was active is worse than a loud startup halt. The defaults are deliberately
generous starting points; recalibrate against real run-log spend.
"""
from __future__ import annotations

from dataclasses import dataclass, fields
from pathlib import Path
from typing import Any

import yaml

_CONFIG_DISPLAY = "~/.drain-cycle/limits.yml"


class LimitsConfigError(RuntimeError):
    """``limits.yml`` exists but is malformed."""


@dataclass(frozen=True)
class Breach:
    """A crossed guardrail, recorded into the halting run-log entry.

    ``scope`` is ``"per-issue"`` or ``"cycle"``; ``metric`` is ``"token"``,
    ``"time"``, or ``"cost"``. ``limit`` is the cap that was set and
    ``observed`` the value at the moment of the breach. ``describe`` renders
    the operator-facing line — the worker uses it for a per-issue kill and
    the orchestrator for a cycle stop, so the wording cannot drift between
    the two surfaces.
    """

    scope: str
    metric: str
    limit: float
    observed: float

    def describe(self) -> str:
        return f"{self.scope} {self.metric} cap exceeded: {self._detail()}"

    def _detail(self) -> str:
        if self.metric == "cost":
            return f"${self.observed:,.2f} ≥ ${self.limit:,.2f}"
        if self.metric == "time":
            return f"{self.observed:,.0f}s ≥ {self.limit:,.0f}s"
        return f"{self.observed:,.0f} ≥ {self.limit:,.0f} tokens"


@dataclass(frozen=True)
class Limits:
    """Resource caps. ``None`` on any field turns that guardrail off.

    Defaults: per-issue 8M tokens · 20 min · $15; cycle 30M tokens ·
    90 min · $60.
    """

    per_issue_tokens: int | None = 8_000_000
    per_issue_seconds: float | None = 20 * 60
    per_issue_cost_usd: float | None = 15.0
    cycle_tokens: int | None = 30_000_000
    cycle_seconds: float | None = 90 * 60
    cycle_cost_usd: float | None = 60.0


def default_config_path() -> Path:
    """Resolved per call so tests can redirect via ``monkeypatch.setenv("HOME", ...)``."""
    return Path.home() / ".drain-cycle" / "limits.yml"


def load(path: Path | None = None) -> Limits:
    """Read ``limits.yml`` over the baked-in defaults.

    A missing or empty file yields the defaults unchanged — ``limits.yml``
    is optional. A present file maps limit names to positive numbers (or
    ``null`` to disable that guardrail); unknown keys, non-numeric values,
    booleans, and non-positive numbers each raise ``LimitsConfigError`` with
    a message naming the on-disk problem.
    """
    defaults = Limits()
    if path is None:
        path = default_config_path()
    if not path.exists():
        return defaults
    try:
        data = yaml.safe_load(path.read_text())
    except yaml.YAMLError as exc:
        raise LimitsConfigError(f"{_CONFIG_DISPLAY} is not valid YAML: {exc}") from None
    if data is None:
        return defaults
    if not isinstance(data, dict):
        raise LimitsConfigError(
            f"{_CONFIG_DISPLAY} must be a mapping of limit names to numbers"
        )

    valid = [f.name for f in fields(Limits)]
    unknown = set(data) - set(valid)
    if unknown:
        raise LimitsConfigError(
            f"{_CONFIG_DISPLAY} has unknown keys: {', '.join(sorted(unknown))}; "
            f"valid keys: {', '.join(valid)}"
        )

    values: dict[str, Any] = {}
    for name in valid:
        if name not in data:
            values[name] = getattr(defaults, name)
            continue
        values[name] = _coerce(name, data[name])
    return Limits(**values)


def _coerce(name: str, raw: Any) -> float | None:
    """Validate one ``limits.yml`` value: ``None`` (off) or a positive number."""
    if raw is None:
        return None
    # ``bool`` is a subclass of ``int``; ``per_issue_tokens: true`` would
    # otherwise slip through as ``1`` and silently arm an absurd cap.
    if isinstance(raw, bool) or not isinstance(raw, (int, float)) or raw <= 0:
        raise LimitsConfigError(
            f"{_CONFIG_DISPLAY} entry {name!r}: must be a positive number "
            f"or null (got {raw!r})"
        )
    return raw


def check_cycle(
    limits: Limits, *, tokens: int, cost_usd: float, seconds: float
) -> Breach | None:
    """Return the first cycle-wide cap the running totals breach, else ``None``.

    Tokens are checked first (the primary guardrail), then cost, then
    wall-clock. A guardrail set to ``None`` is skipped.
    """
    if limits.cycle_tokens is not None and tokens >= limits.cycle_tokens:
        return Breach("cycle", "token", limits.cycle_tokens, tokens)
    if limits.cycle_cost_usd is not None and cost_usd >= limits.cycle_cost_usd:
        return Breach("cycle", "cost", limits.cycle_cost_usd, cost_usd)
    if limits.cycle_seconds is not None and seconds >= limits.cycle_seconds:
        return Breach("cycle", "time", limits.cycle_seconds, seconds)
    return None
