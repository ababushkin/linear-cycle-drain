# drain-cycle

Unattended execution of a Linear cycle. One invocation, no prompts; each issue runs in an isolated worktree under a fresh `claude -p` session that updates Linear itself.

## Why this exists

Today, executing a Linear cycle is manual: launch Claude per issue, watch it run, approve permissions, update Linear, repeat. Execution shepherding consumes time that should go to *scoping the next cycle* and *validating delivered work*. `drain-cycle` removes that shepherding so attention shifts back to scoping and validation. The full goal / KRs / kill condition live in the Linear project description.

## Is this for you?

Read this section before installing — `drain-cycle` is deliberately not for everyone.

- **You need to be confident in your cycle planning.** `drain-cycle` executes the cycle you scoped — it does not reshape it. A poorly-scoped cycle drains faster than a manually-shepherded one — the multiplier works in both directions. Plan deliberately; this tool is the multiplier, not the safety net.
- **Accept the risk profile.** Every spawned session runs with `--dangerously-skip-permissions`. The agent can write files, run shell commands, hit the network, and update Linear without asking. The blast radius is documented in [`docs/design-decisions.md`](docs/design-decisions.md). If that makes you uncomfortable, that's the right instinct — stick with manual `claude` runs until it doesn't. Comfort with unattended execution is earned, not assumed.
- **Personal product, single operator.** One operator, one machine, one Linear workspace. Not designed for shared infra, production-touching cycles, or team workflows.

## Prerequisites

- **[`uv`](https://docs.astral.sh/uv/) on `$PATH`** — the installer; it also fetches the pinned Python for the tool's isolated env, so you don't install Python yourself.
- **`git` CLI** on `$PATH`.
- **`claude` CLI** on `$PATH` — see the [Claude Code install docs](https://docs.claude.com/en/docs/claude-code/setup).
- **Linear API key** with read/write on your Personal team. Generate one at <https://linear.app/settings/api>.
- **A `repo:<name>` label on every cycle issue** — either a flat `repo:<name>` label or a child of a `repo` label group whose leaf name matches a `repos.yml` key. `drain-cycle` resolves the target repo from this label — an unlabelled issue halts the run before any worktree is created. Create the labels at the team level in Linear (Settings → Labels) using the exact names your `repos.yml` keys use.
- **An optional `model:<alias>` Linear label.** Workers default to `claude-sonnet-4-6` — the cost-efficient default for unattended drains. Add `model:opus` (or `model:haiku`, or a full model id like `model:claude-opus-4-7`) to an issue that warrants a different model; that issue's worker runs on it. Missing, unknown, or conflicting labels fall back to the default — model resolution never halts a run.
- **A `~/.drain-cycle/repos.yml` config file** mapping each `<name>` to the absolute path of the repo on disk (see Install below).
- **Add `.worktrees/` to each target repo's `.gitignore`.** `drain-cycle` creates `.worktrees/<issue-identifier>/` inside the target repo per spawned session; without an ignore rule the spawned `git add` calls can sweep those paths into commits. A one-line entry per repo is enough.

## Install

Install with `uv` so the `drain-cycle` executable lands on `$PATH` in an isolated environment — no venv to activate:

```bash
uv tool install .                                              # from a local checkout
uv tool install git+https://github.com/ababushkin/drain-cycle  # or straight from the repo
```

`uv` installs the executable under `~/.local/bin`. If that isn't on your `$PATH`, run `uv tool update-shell` once. The CLI resolves each issue's target repo from its `repo:<name>` label, so the run directory is irrelevant.

Then set up `~/.drain-cycle/`, which holds everything the tool reads and writes (config, secret, run logs):

```bash
mkdir -p ~/.drain-cycle
echo 'LINEAR_API_KEY=lin_api_…' > ~/.drain-cycle/.env
```

The CLI reads the key from the first source that defines it: a shell-exported `LINEAR_API_KEY` wins, then `~/.drain-cycle/.env`, then a `.env` at the repo root (a dev-checkout fallback the installed tool never sees). Export it in your shell rc instead of the file if you prefer. The same precedence applies to the optional `HONEYCOMB_API_KEY` (see [Telemetry](#telemetry-optional)).

Create `~/.drain-cycle/repos.yml` mapping each label name to the repo's absolute path on disk:

```yaml
repos:
  drain-cycle:  /Users/you/src/drain-cycle
  pde-skills:   /Users/you/src/pde-skills
  stock-review: /Users/you/src/stock-review
```

Paths starting with `~` expand against `$HOME`. A missing or malformed `repos.yml` halts the CLI before any Linear traffic — exit 1, message on stderr, no run-log entry written. See [`docs/repos.example.yml`](docs/repos.example.yml) for a copyable template.

By default each worker symlinks the repo's `.claude/` and `.mcp.json` into its worktree, so it loads the same project-scoped settings, hooks, agents, and skills as an interactive session at the repo root (see [Debug capture](#debug-capture) and [`docs/design-decisions.md`](docs/design-decisions.md) §10). To link a different set — for example to add a tool-specific directory like `.entire/` — add an optional `worktree_config_paths` list:

```yaml
worktree_config_paths:
  - .claude
  - .mcp.json
  - .entire        # opt in if this repo uses the entire.io tool
```

Omit the key to keep the `[.claude, .mcp.json]` default. Each entry is a path relative to the repo root; absent entries are skipped, and a tracked entry git already checked out is never overwritten. **Each entry must be gitignored in the target repo** — the symlink lives inside the worktree, so a non-ignored entry would be staged by the worker's `git add` and would block `git worktree remove` at teardown. The two defaults are gitignored in a typical repo; check any path you add.

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

Run from anywhere — `drain-cycle` resolves each issue's target repo from its `repo:<name>` label. Drains the current cycle's Todo/Backlog issues in manual (drag) order, respecting blocks/blocked-by, until either the cycle is empty (exit 0) or an issue halts (exit 1).

Issues blocked by an unresolved blocker outside the drain set are deferred (skipped with a stderr message, left Todo). A dependency cycle among pending issues halts the entire run immediately (exit 1) with a `Halt:` line naming the involved issues.

### Inspecting a live run

While a run is in progress, open a second terminal and run:

```bash
drain-cycle status
```

Example output:

```
drain-cycle: ABA-205 [2/5] — Fix the runaway token bug
  repo:    drain-cycle
  model:   claude-sonnet-4-6
  elapsed: 14m
  turns:   42
  tokens:  8.1M cumulative  180k peak [cap: 8M ⚠]
  cost:    $12.30 [cap: $15.00]
```

The orchestrator also writes a compact progress line to stderr on every new turn:

```
ABA-205 · turn 42 · 8.1M tok (peak 180k) · $12.30 · 14m
```

`drain-cycle status` reads `~/.drain-cycle/active.json` (written before each spawn, removed after the worker returns). With no run active it says so; with a crashed run (pid gone) it reports a stale marker rather than a live run.

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

Spawned sessions use whatever skills you've installed globally. The pairing it was designed for:

- [**`ababushkin/pde-skills`**](https://github.com/ababushkin/pde-skills) — planning + engineering skill pack. Use it to *shape* the cycle (initiative, KRs, slices) before draining it. `drain-cycle` only multiplies execution; the quality of the cycle is upstream of this tool.
- [**`addyosmani/agent-skills`**](https://github.com/addyosmani/agent-skills) — Addy Osmani's pack covers complementary build / test / review skills the spawned sessions lean on.

Both are skill packs for Claude Code — install them globally and the spawned `claude -p` sessions will pick them up automatically.

## Limits

Each worker is bounded by resource guardrails so a runaway session — or a cycle of merely expensive ones — can't drain your whole quota unattended. There are two layers:

- **Native belt** — the per-issue cost cap is handed to `claude` as `--max-budget-usd`, so the session self-terminates on spend.
- **Orchestrator suspenders** — the per-issue token and time caps are enforced by killing the worker's whole process group the moment either is crossed; the cycle-wide caps (tokens, cost, wall-clock) stop the run between issues when their running total is breached. A per-issue breach lands in the halting entry's `halt_reason`; a cycle breach is recorded in the run log's top-level `cycle_halt_reason`.

Token count is the primary guardrail (a subscription user pays in tokens, not dollars); cost rides alongside it.

Defaults are baked in — **per-issue 8M tokens · 20 min · $15; cycle 30M tokens · 90 min · $60** — and live with no config. To change them, drop a `~/.drain-cycle/limits.yml` (optional) overriding only the caps you care about:

```yaml
per_issue_tokens: 4000000   # tighten the per-issue token cap
cycle_cost_usd: null        # disable the cycle cost cap entirely
```

A key you omit keeps its default; a number overrides it; `null` turns that guardrail off. Each value must be a positive number or `null` — a malformed file halts startup rather than silently reverting to defaults. See [`docs/limits.example.yml`](docs/limits.example.yml) for the full annotated template. The defaults are deliberately generous starting points; recalibrate against your real run-log spend.

## Logs & grading

Every invocation writes one JSON file: `~/.drain-cycle/runs/<cycle-id>-<run-timestamp>.json`. One entry per attempted issue, including timestamps, exit code, final Linear state, worktree path, per-issue token usage and cost, and `halt_reason` on the halting entry. The file also carries per-cycle totals (`cycle_tokens_cumulative`, `cycle_cost_usd`, `cycle_duration_seconds`) and a `cycle_halt_reason` when a cycle-wide cap stopped the run. Use it to gauge how cleanly your runs complete and what they cost — see [`drain_cycle/runlog.py`](drain_cycle/runlog.py) for the schema.

### Debug capture

Workers spawn in an isolated worktree. drain-cycle symlinks the repo's project-scoped config (`.claude/`, `.mcp.json`, and any extra paths you configure — see Install) into each worktree, so a worker loads the same settings, hooks, agents, and skills as an interactive session at the repo root, including project-scoped hooks like entire.io's checkpointing. [`docs/design-decisions.md`](docs/design-decisions.md) §10 documents the mechanism.

To confirm that parity — to see exactly which settings sources, plugins, MCP servers, and hooks a worker initialised — run with debug capture on:

```bash
DRAIN_CYCLE_DEBUG=1 drain-cycle
```

Each spawned session then gets `claude`'s `--debug-file`, writing one `<cycle-id>-<run-timestamp>-<issue>.debug.log` per issue beside the run log in `~/.drain-cycle/runs/`. It is off by default. Debug output goes to the file, so it doesn't disturb the token-usage stream the run log records. These captures are verbose and aren't pruned automatically (like the run logs themselves); delete them by hand when you're done investigating.

## Telemetry (optional)

drain-cycle can emit OpenTelemetry traces to [Honeycomb](https://www.honeycomb.io/) so you can see where a drain spends its time and tokens. It is **off unless `HONEYCOMB_API_KEY` is set** — with no key, tracing is a no-op and the tool takes no runtime network dependency.

```bash
echo 'HONEYCOMB_API_KEY=hcaik_…' >> ~/.drain-cycle/.env
```

Each invocation then emits one trace: a `drain.cycle` root span, a `drain.issue` span per attempted issue, and under those the spawned-session (`drain.worker.session`, carrying token/cost/turn usage), worktree (`drain.worktree.add`/`.remove`), and Linear (`linear.*`, wrapping the auto-instrumented `httpx` calls) spans. Traces land in a Honeycomb dataset named `drain-cycle`. Optional overrides: `HONEYCOMB_API_ENDPOINT` (default `https://api.honeycomb.io`; set the EU host for an EU team) and `OTEL_SERVICE_NAME` (default `drain-cycle`, which is also the dataset). See [`docs/design-decisions.md`](docs/design-decisions.md) §13.

## Design

Design decisions, alternatives considered, and deliberate out-of-scope choices live in [`docs/design-decisions.md`](docs/design-decisions.md).
