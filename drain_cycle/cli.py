"""``drain-cycle`` CLI entry point.

Zero-arg invocation per US-A: drain the current Linear cycle. Each
issue's target repo is resolved from a ``repo:<name>`` label against
``~/.drain-cycle/repos.yml`` (ABA-232); the operator runs ``drain-cycle``
from anywhere, not from inside a target repo. The ``grade`` subcommand
(US-D / ABA-197) reads the run logs and prints a health read.

Loads ``.env`` from the drain-cycle repo root before any module reads
``os.environ`` — the CLI is run from arbitrary cwds, so the default
``find_dotenv()`` walk would miss it. Shell-exported vars still win
(``load_dotenv`` does not override by default).

``repos.yml`` is validated eagerly at startup so a broken config halts
exit 1 on stderr before any Linear traffic or run-log file is written
— there is no cycle yet to log against.
"""
from __future__ import annotations

import sys
from pathlib import Path

from dotenv import load_dotenv

from . import grade, orchestrator, repos

_REPO_ENV = Path(__file__).resolve().parent.parent / ".env"

_USAGE = (
    "usage: drain-cycle              drain the current Linear cycle\n"
    "       drain-cycle grade        print health read from run logs\n"
    "       drain-cycle --help"
)


def main() -> None:
    load_dotenv(_REPO_ENV)
    argv = sys.argv[1:]
    if not argv:
        try:
            loaded_repos = repos.load()
        except repos.RepoConfigError as exc:
            print(f"drain-cycle: {exc}", file=sys.stderr)
            sys.exit(1)
        sys.exit(orchestrator.run(loaded_repos))
    if argv == ["grade"]:
        sys.exit(grade.run(grade.default_runs_dir()))
    if argv in (["-h"], ["--help"]):
        print(_USAGE)
        sys.exit(0)
    print(f"drain-cycle: unknown invocation: {' '.join(argv)}", file=sys.stderr)
    print(_USAGE, file=sys.stderr)
    sys.exit(2)
