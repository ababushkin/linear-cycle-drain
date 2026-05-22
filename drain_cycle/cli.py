"""``drain-cycle`` CLI entry point.

Zero-arg invocation per US-A: cwd is the target repo, all behaviour is
implicit from the current Linear cycle. The ``grade`` subcommand
(US-D / ABA-197) reads the run logs and prints a health read.

Loads ``.env`` from the drain-cycle repo root before any module reads
``os.environ`` — the CLI is run from arbitrary target-repo cwds, so the
default ``find_dotenv()`` walk would miss it. Shell-exported vars still
win (``load_dotenv`` does not override by default).
"""
from __future__ import annotations

import sys
from pathlib import Path

from dotenv import load_dotenv

from . import grade, orchestrator

_REPO_ENV = Path(__file__).resolve().parent.parent / ".env"


def main() -> None:
    load_dotenv(_REPO_ENV)
    argv = sys.argv[1:]
    if argv and argv[0] == "grade":
        sys.exit(grade.run(grade.default_runs_dir()))
    sys.exit(orchestrator.run())
