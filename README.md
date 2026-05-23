# drain-cycle

Unattended execution of a Linear cycle. One invocation, no prompts; each issue runs in an isolated worktree under a fresh `claude -p` session that updates Linear itself.

## Why this exists

Today, executing a Linear cycle is manual: launch Claude per issue, watch it run, approve permissions, update Linear, repeat. Execution shepherding consumes time that should go to *scoping the next cycle* and *validating delivered work*. `drain-cycle` removes that shepherding so attention shifts back to scoping and validation. The full goal / KRs / kill condition live in the Linear project description.

## Is this for you?

Read this section before installing — `drain-cycle` is deliberately not for everyone.

- **You need to be confident in your cycle planning.** `drain-cycle` executes the cycle you scoped — it does not reshape it. A poorly-scoped cycle drains faster than a manually-shepherded one, but the result is poorly-scoped work shipped faster. Plan deliberately; this tool is the multiplier, not the safety net.
- **Accept the risk profile.** Every spawned session runs with `--dangerously-skip-permissions`. The agent can write files, run shell commands, hit the network, and update Linear without asking. The blast radius is documented in [`docs/design-decisions.md`](docs/design-decisions.md). If that makes you uncomfortable, that's the right instinct — stick with manual `claude` runs until it doesn't. Comfort with unattended execution is earned, not assumed.
- **Personal product, single operator.** One operator, one machine, one Linear workspace. Not designed for shared infra, production-touching cycles, or team workflows.

## Prerequisites

- **[`uv`](https://docs.astral.sh/uv/) on `$PATH`** — the installer; it also fetches the pinned Python for the tool's isolated env, so you don't install Python yourself.
- **`git` CLI** on `$PATH`.
- **`claude` CLI** on `$PATH` — see the [Claude Code install docs](https://docs.claude.com/en/docs/claude-code/setup).
- **Linear API key** with read/write on your Personal team. Generate one at <https://linear.app/settings/api>.
- **A `repo:<name>` Linear label on every cycle issue.** `drain-cycle` resolves the target repo per issue from this label — an unlabelled issue halts the run before any worktree is created. Create the labels at the team level in Linear (Settings → Labels) using the exact names your `repos.yml` keys use.
- **An optional `model:<alias>` Linear label.** Workers default to `claude-sonnet-4-6` — the cost-efficient default for unattended drains. Add `model:opus` (or `model:haiku`, or a full model id like `model:claude-opus-4-7`) to an issue that warrants a different model; that issue's worker runs on it. Missing, unknown, or conflicting labels fall back to the default — model resolution never halts a run.
- **A `~/.drain-cycle/repos.yml` config file** mapping each `<name>` to the absolute path of the repo on disk (see Install below).
- **Each target repo must gitignore `.worktrees/`.** `drain-cycle` creates `.worktrees/<issue-identifier>/` inside the target repo per spawned session; without an ignore rule the spawned `git add` calls can sweep those paths into commits. A one-line `.gitignore` entry per repo is enough.

## Install

Install with `uv` so the `drain-cycle` executable lands on `$PATH` in an isolated environment — no venv to activate, runs from any directory:

```bash
uv tool install .                                          # from a local checkout
uv tool install git+https://github.com/ababushkin/drain-cycle  # or straight from the repo
```

`uv` installs the executable under `~/.local/bin`. If that isn't on your `$PATH`, run `uv tool update-shell` once. The CLI resolves each issue's target repo from its `repo:<name>` label, so the directory you run it from doesn't matter.

Then set up `~/.drain-cycle/`, which holds everything the tool reads and writes (config, secret, run logs):

```bash
mkdir -p ~/.drain-cycle
echo 'LINEAR_API_KEY=lin_api_…' > ~/.drain-cycle/.env
```

The key is read from the first source that defines it: a shell-exported `LINEAR_API_KEY` wins, then `~/.drain-cycle/.env`, then a `.env` at the repo root (a dev-checkout fallback the installed tool never sees). Export it in your shell rc instead of the file if you prefer.

Create `~/.drain-cycle/repos.yml` mapping each label name to the repo's absolute path on disk:

```yaml
repos:
  drain-cycle:  /Users/you/src/drain-cycle
  pde-skills:   /Users/you/src/pde-skills
  stock-review: /Users/you/src/stock-review
```

A `~`-prefixed path inside the file is expanded against `$HOME`. A missing or malformed `repos.yml` halts the CLI before any Linear traffic — exit 1, message on stderr, no run-log entry written. See [`docs/repos.example.yml`](docs/repos.example.yml) for a copyable template.

Verify the install from anywhere:

```bash
cd /tmp && drain-cycle --help     # any invocation that doesn't crash on import works
```

### Developing on drain-cycle

Working on the tool itself? Pin the dev Python and install editable so source edits take effect without reinstalling:

```bash
mise install                       # picks up mise.toml → Python 3.12
uv tool install --editable .       # symlinks the checkout
uv tool install --reinstall .      # or rebuild from scratch after a change
```

## Usage

```
drain-cycle
```

Run from anywhere — `drain-cycle` resolves each issue's target repo from its `repo:<name>` label. Drains the current cycle's Todo/Backlog issues (in priority order) until either the cycle is empty (exit 0) or an issue halts (exit 1).

### What a run looks like

```bash
$ drain-cycle

# drain-cycle reads the current Linear cycle, fetches its Todo/Backlog issues,
# and works through them one at a time. Each issue's target repo is resolved
# from its `repo:<name>` label against ~/.drain-cycle/repos.yml.

drain-cycle: picked ABA-241: Persist halt_reason in run-log entries

# ABA-241 is labelled `repo:drain-cycle`, which `repos.yml` maps to
# ~/src/drain-cycle. The orchestrator creates a fresh git worktree under
# ~/src/drain-cycle/.worktrees/ABA-241/ branched off main, flips the Linear
# issue to In Progress, then spawns a `claude -p` session inside the worktree.

  (… claude -p session does the work: reads the issue, edits files,
       runs tests, commits, pushes, marks the issue Done in Linear …)

drain-cycle: ABA-241 done; worktree removed.
drain-cycle: picked PDE-12: Add the new triage skill

# PDE-12 is labelled `repo:pde-skills`, so this run lands in a worktree
# under ~/src/pde-skills/.worktrees/PDE-12/ — different repo, same machinery.

  (… second session runs …)

drain-cycle: PDE-12 done; worktree removed.
drain-cycle: picked ABA-243: Compute cycle_duration_seconds

  (… third session runs, but doesn't finish the work …)

# The session exited but the Linear issue is still In Progress — meaning the
# agent didn't mark it Done. drain-cycle stops the run immediately so you can
# investigate. The worktree is left in place; the run log captures the same
# halt line you see on screen.

Halt: ABA-243 (final state: In Progress) at /Users/you/src/drain-cycle/.worktrees/ABA-243
$ echo $?
1
```

After the run, one JSON log per invocation is at `~/.drain-cycle/runs/<cycle-id>-<run-timestamp>.json`. On halt, the worktree at `<target-repo>/.worktrees/<issue-identifier>/` is preserved for inspection; on success it's removed.

## Recommended companion skills

`drain-cycle` assumes the spawned `claude -p` sessions have access to whatever skills you've installed globally. The pairing it was designed for:

- [**`ababushkin/pde-skills`**](https://github.com/ababushkin/pde-skills) — planning + engineering skill pack. Use it to *shape* the cycle (initiative, KRs, slices) before draining it. `drain-cycle` only multiplies execution; the quality of the cycle is upstream of this tool.
- [**`addyosmani/agent-skills`**](https://github.com/addyosmani/agent-skills) — Addy Osmani's pack covers complementary build / test / review skills the spawned sessions lean on.

Both are skill packs for Claude Code — install them globally and the spawned `claude -p` sessions will pick them up automatically.

## Logs & grading

Every invocation writes one JSON file: `~/.drain-cycle/runs/<cycle-id>-<run-timestamp>.json`. One entry per attempted issue, including timestamps, exit code, final Linear state, worktree path, and `halt_reason` on the halting entry. Use it to gauge how cleanly your runs complete — see [`drain_cycle/runlog.py`](drain_cycle/runlog.py) for the schema.

## Design

Design decisions, alternatives considered, and deliberate out-of-scope choices live in [`docs/design-decisions.md`](docs/design-decisions.md).
