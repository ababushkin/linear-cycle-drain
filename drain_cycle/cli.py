"""``drain-cycle`` CLI entry point.

Zero-arg invocation per US-A: cwd is the target repo, all behaviour is
implicit from the current Linear cycle.

Loads ``.env`` from the drain-cycle repo root before any module reads
``os.environ`` — the CLI is run from arbitrary target-repo cwds, so the
default ``find_dotenv()`` walk would miss it. Shell-exported vars still
win (``load_dotenv`` does not override by default).
"""
from __future__ import annotations

import sys
from pathlib import Path

from dotenv import load_dotenv

from .orchestrator import run

_REPO_ENV = Path(__file__).resolve().parent.parent / ".env"


def main() -> None:
    load_dotenv(_REPO_ENV)
    sys.exit(run())
