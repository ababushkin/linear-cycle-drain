# Design decisions

Design rationale for `drain-cycle`. Read this before making architectural changes — `AGENTS.md` points here.

Four decisions are recorded so a future reader doesn't have to reverse-engineer them from the code. ADRs would be heavier than this tool needs.

## 1. The spawned agent updates Linear itself

The orchestrator does **not** poll Linear and write status. The spawned `claude -p` session is told, in its prompt, to move its issue to Done on completion. The orchestrator only reads Linear after the session exits, to decide whether to advance or halt.

**Alternative considered.** Orchestrator-owned status: the parent polls Linear, transitions states, owns the lifecycle. This is more conventional and easier to reason about.

**Why the agent-self-update path.** The orchestrator can only observe *process exit*, not *task success*. A Claude session may exit 0 having done nothing useful, or exit non-zero having actually shipped — exit code is a poor proxy for "the issue is Done." Letting the agent assert Done in Linear forces it to make an explicit, observable claim about its own outcome, which is exactly the artefact we need to grade KR1 and trigger the kill condition. If this pattern proves unreliable, that's the initiative's kill condition firing — not a bug to paper over.

## 2. `--dangerously-skip-permissions` is accepted

Every spawned session runs with `--dangerously-skip-permissions`. The agent can run any tool on any path inside its worktree, including shell commands, file writes, and network calls, with no operator approval.

**Blast radius.** Bounded to the per-issue worktree under `.worktrees/<issue-identifier>/` inside the target repo, plus the operator's Linear account (the agent can transition issues), plus whatever the spawned shell can reach (env vars, secrets in `~/.config`, network egress). Not bounded to the worktree at the filesystem level — a determined or confused agent can `cd` out.

**Why accepted.** The single-operator personal-product context: target repos are mine, the Linear workspace is mine, the machine is mine. The point of the tool is removing prompts; gating them defeats the purpose. The mitigation is **scope discipline at cycle planning** — don't drain a cycle whose issues touch credentials, production systems, or shared infrastructure. This is operator responsibility, not a tool guarantee.

## 3. Fresh worktree per issue, not a shared workspace

Each issue gets `.worktrees/<issue-identifier>/` branched off `main`, used once, then either removed (on Done) or preserved (on halt).

**Alternative considered.** Shared workspace where all issues run in the target repo's main checkout in sequence. Simpler, faster, no worktree plumbing.

**Why worktree-per-issue.** Issues drift. An agent that misunderstands its task can leave the workspace in a broken state — half-applied edits, uncommitted files, branch in the wrong place — that contaminates every subsequent issue's starting point. The worktree gives each issue a clean, identical starting point regardless of what the previous one did, and preservation-on-halt (US-B) means inspectable debug state. The cost is filesystem space and a few seconds of branch setup per issue. Cheap.

## 4. Run-log is one file per invocation, not one file per cycle

Each `drain-cycle` invocation writes its own run-log file at `~/.drain-cycle/runs/<cycle-id>-<run-timestamp>.json`. The per-file schema is unchanged — `{cycle_id, cycle_duration_seconds, entries: [...]}` — and the `cycle_id` carried inside each file is how downstream readers group runs of the same cycle together.

**Alternatives considered.** (A) Multi-run schema in one file: `{cycle_id, runs: [...]}`. Faithful, but every reader has to learn the new shape and the on-disk backup file needs migrating. (B) Open-and-extend: load the existing file and append to a single flat `entries` list. Loses run boundaries, and `cycle_duration_seconds` (computed as `max(finished_at) - min(started_at)`) spans the inter-run gap and becomes misleading. (D) Refuse to clobber: fail-fast if the file exists. Breaks the unattended re-run flow the tool exists for (fix X, re-run, drain the rest) until a resume mode is built.

**Why per-run files.** The bug being fixed (ABA-230) is that a second invocation against the same cycle silently overwrites the first run's data. Per-run files make the write path single-writer-write-once — no read-then-write race, no schema diff, no migration. US-D (ABA-197), which already plans to glob `runs/*.json` and merge across cycles, gets a one-line addition (group by `cycle_id`) instead of a new shape to read. Each file's `cycle_duration_seconds` represents one invocation's hands-off time; US-D sums them when reporting cycle-level KR2.

**Cost.** A re-run cycle accumulates one file per invocation. At cycle scale (≤ 15 issues, rarely more than 2–3 invocations to drain) this is negligible. No retention policy is shipped; trim by hand if it ever matters.

## Out of scope (deliberate)

- **Parallelism.** Issues run one at a time. The Linear cycle is the unit; intra-cycle parallelism adds resource contention and serialises poorly with the agent-self-update pattern (two agents racing to mark different issues Done is fine, but two agents racing on overlapping files is not).
- **Retry.** A halted issue is not retried automatically. The operator inspects the worktree and decides — fix, redo manually, or descope.
- **Cross-cycle scheduling.** One cycle per invocation. Chaining cycles is an operator concern.
