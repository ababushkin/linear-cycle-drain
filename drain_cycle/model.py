"""Per-issue worker-model resolution.

Workers default to a cost-efficient model. A spawned ``claude -p`` session
inherits whatever model the operator has globally pinned, which can be the
most expensive one — so unattended cycles default to Sonnet here and let an
individual issue opt up (or down) via a ``model:<alias>`` Linear label,
mirroring the ``repo:<name>`` mechanism in ``repos.py``.

Resolution is deliberately lenient and never raises: a worker always has a
safe model to run, so any ambiguity falls back to the default rather than
halting an unattended cycle. The model actually used is recorded in the run
log, so a mis-labelled issue surfaces after the fact instead of stalling.

* No ``model:`` label → ``DEFAULT_MODEL``.
* Exactly one label → its value, mapped through ``_ALIASES`` if known,
  otherwise passed through verbatim (``claude --model`` validates it).
* More than one label → ``DEFAULT_MODEL`` (ambiguous intent; the cheap,
  safe default wins).
"""
from __future__ import annotations

from typing import Any

_MODEL_LABEL_PREFIX = "model:"

DEFAULT_MODEL = "claude-sonnet-4-6"

_ALIASES = {
    "sonnet": "claude-sonnet-4-6",
    "opus": "claude-opus-4-7",
    "haiku": "claude-haiku-4-5-20251001",
}


def resolve(issue: dict[str, Any]) -> str:
    """Return the model the worker for ``issue`` should run on."""
    values = [
        label.removeprefix(_MODEL_LABEL_PREFIX)
        for label in issue.get("labels", [])
        if label.startswith(_MODEL_LABEL_PREFIX)
    ]
    if len(values) != 1:
        return DEFAULT_MODEL
    requested = values[0]
    return _ALIASES.get(requested, requested)
