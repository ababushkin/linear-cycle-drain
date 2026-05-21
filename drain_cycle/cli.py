"""``drain-cycle`` CLI entry point.

Zero-arg invocation per US-A: cwd is the target repo, all behaviour is
implicit from the current Linear cycle.
"""
from __future__ import annotations

import sys

from .orchestrator import run


def main() -> None:
    sys.exit(run())
