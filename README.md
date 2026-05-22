# drain-cycle

Unattended execution of a Linear cycle. One invocation, no prompts; each issue runs in an isolated worktree under a fresh `claude -p` session that updates Linear itself.

## Why this exists

Today, executing a Linear cycle is manual: launch Claude per issue, watch it run, approve permissions, update Linear, repeat. This time that should go to *scoping the next cycle* and *validating delivered work* is consumed by execution shepherding. `drain-cycle` removes that shepherding for the common path so attention shifts back to scoping and validation. The full goal / KRs / kill condition live in the Linear project description.

## Is this for you?

Read this section before installing — `drain-cycle` is deliberately not for everyone.

- **You need to be confident in your cycle planning.** `drain-cycle` executes the cycle you scoped — it does not reshape it. A poorly-scoped cycle drains faster than a manually-shepherded one, but the result is poorly-scoped work shipped faster. Plan deliberately; this tool is the multiplier, not the safety net.
- **Accept the risk profile.** Every spawned session runs with `--dangerously-skip-permissions`. The agent can write files, run shell commands, hit the network, and update Linear without asking. The blast radius is documented in [`docs/design-decisions.md`](docs/design-decisions.md). If that makes you uncomfortable, that's the right instinct — stick with manual `claude` runs until it doesn't. Comfort with unattended execution is earned, not assumed.
- **Personal product, single operator.** One operator, one machine, one Linear workspace. Not designed for shared infra, production-touching cycles, or team workflows.

## Prerequisites

- **Python 3.11+** — pinned to 3.12 via `mise.toml`. Install [`mise`](https://mise.jdx.dev/) first.
- **`git` CLI** on `$PATH`.
- **`claude` CLI** on `$PATH` — see the [Claude Code install docs](https://docs.claude.com/en/docs/claude-code/setup).
- **Linear API key** with read/write on your Personal team. Generate one at <https://linear.app/settings/api>.

## Install

```bash
git clone https://github.com/ababushkin/drain-cycle
cd drain-cycle
mise install                          # picks up mise.toml → Python 3.12
pip install -e .                      # installs the `drain-cycle` CLI
echo 'LINEAR_API_KEY=lin_api_…' > .env  # loaded automatically at CLI start
```

`.env` lives at the drain-cycle repo root and is gitignored, if you'd rather export `LINEAR_API_KEY` in your shell rc, that still works and takes precedence over `.env`.

Verify the install:

```bash
drain-cycle --help     # any invocation that doesn't crash on import works
```

## Usage

```
cd /path/to/target-repo
drain-cycle
```

Drains the current cycle's Todo/Backlog issues (in priority order) until either the cycle is empty (exit 0) or an issue halts (exit 1).

### What a run looks like

Below is a representative trace — invented for illustration; a recorded demo will replace it later. Every line is something `drain-cycle` actually prints today.

```bash
$ cd ~/src/my-project
$ drain-cycle

# drain-cycle reads the current Linear cycle, fetches its Todo/Backlog issues,
# and works through them one at a time. The first issue it picks up:

drain-cycle: picked ABA-241: Persist halt_reason in run-log entries

# It creates a fresh git worktree under .worktrees/ABA-241/ branched off main,
# flips the Linear issue to In Progress, then spawns a `claude -p` session
# inside the worktree. The Claude session's own output streams to your
# terminal here — same as if you ran it by hand.

  (… claude -p session does the work: reads the issue, edits files,
       runs tests, commits, pushes, marks the issue Done in Linear …)

# When the session exits, drain-cycle re-reads Linear. The issue is now in
# the Done column — so the worktree gets cleaned up and the next issue starts.

drain-cycle: ABA-241 done; worktree removed.
drain-cycle: picked ABA-242: Spec-shaped halt message on stderr

  (… second session runs …)

drain-cycle: ABA-242 done; worktree removed.
drain-cycle: picked ABA-243: Compute cycle_duration_seconds

  (… third session runs, but doesn't finish the work …)

# The session exited but the Linear issue is still In Progress — meaning the
# agent didn't mark it Done. drain-cycle stops the run immediately so you can
# investigate. The worktree is left in place; the run log captures the same
# halt line you see on screen.

Halt: ABA-243 (final state: In Progress) at /Users/you/src/my-project/.worktrees/ABA-243
$ echo $?
1
```

After the run, the per-cycle JSON log is at `~/.drain-cycle/runs/<cycle-id>.json`. On halt, the worktree at `.worktrees/<issue-identifier>/` is preserved for inspection; on success it's removed.

## Recommended companion skills

`drain-cycle` assumes the spawned `claude -p` sessions have access to whatever skills you've installed globally. The pairing it was designed for:

- [**`ababushkin/pde-skills`**](https://github.com/ababushkin/pde-skills) — planning + engineering skill pack. Use it to *shape* the cycle (initiative, KRs, slices) before draining it. `drain-cycle` only multiplies execution; the quality of the cycle is upstream of this tool.
- [**`addyosmani/agent-skills`**](https://github.com/addyosmani/agent-skills) — Addy Osmani's pack covers complementary build / test / review skills the spawned sessions lean on.

Both are skill packs for Claude Code — install them globally and the spawned `claude -p` sessions will pick them up automatically.

## Logs & grading

Every invocation writes one JSON file: `~/.drain-cycle/runs/<cycle-id>.json`. One entry per attempted issue, including timestamps, exit code, final Linear state, worktree path, and `halt_reason` on the halting entry. You can use this to get some stats on how cleanly your executions are performing — see [`drain_cycle/runlog.py`](drain_cycle/runlog.py) for the schema.

## Design

Design decisions, alternatives considered, and deliberate out-of-scope choices live in [`docs/design-decisions.md`](docs/design-decisions.md).
