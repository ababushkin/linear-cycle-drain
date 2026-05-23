# Design decisions

Design rationale for `drain-cycle`. Read this before making architectural changes — `AGENTS.md` points here.

Eight decisions are recorded so a future reader doesn't have to reverse-engineer them from the code. ADRs would be heavier than this tool needs.

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

## 5. Each issue declares its target repo via a `repo:<name>` Linear label

The orchestrator used to be single-repo by construction: `repo = Path.cwd()`, every worktree under `<cwd>/.worktrees/`. Cycles in this workspace span multiple repos by design — `linear-workflow.md` makes "Affected repos" part of the six initiative-readiness fields, and the Ops slot deliberately holds cross-repo maintenance issues. Each issue now carries a `repo:<name>` Linear label; `~/.drain-cycle/repos.yml` maps the name to an absolute path; the operator runs `drain-cycle` from anywhere.

**Alternatives considered.**

- *Description-body encoding* (e.g. a `Repo: drain-cycle` line in the markdown). The description is the most-edited surface, agents rewrite it routinely, and there's no schema enforcement. A label is one structured field with one value — Linear validates it, and a label rename triggers a clear "this label doesn't exist" error rather than silently drifting to a wrong repo.
- *Title prefix* (e.g. `[drain-cycle] Fix the foo`). Cleaner machine parse than the body, but every issue's title gains visual clutter for a parser-only concern. Labels don't pay that cost.
- *Inherit from the Linear project's "affected repos"*. Ambiguous for projects that legitimately touch multiple repos, and there is no obvious answer for the Ops container project, which is multi-repo by definition.

**Missing-repo halt behaviour.** Same machinery as every other pre-spawn halt: write a run-log entry, print the `Halt:` line to stderr, exit 1, leave subsequent issues untouched. The new wrinkle is that resolution failures happen before any Linear state is moved, so they skip the post-spawn revert path (`_revert_to_pre_halt_state`). This is the difference from the existing setup-failure halt: that one happens after `worktree.add` or the initial `linear.set_state` attempt; the resolution halt happens before either. Either way, no revert is needed because no state was moved.

**`repos.yml` config errors are even earlier.** A missing or malformed `repos.yml` halts the CLI at startup, before any Linear traffic and before the run-log file is created. There is no cycle yet to log against, so the failure surfaces only on stderr. This is enforced eagerly in `cli.main` so the orchestrator never sees a broken config.

**Why labels not file convention** (e.g. requiring the operator to put the issue identifier in a branch comment, or use a remote-name convention). Labels are the only signal that's enforceable in Linear's UI: cycle planners can see at a glance which repo an issue targets, and the multi-repo distinction is visible at the right surface (Linear) rather than buried in a config file.

**Out-of-v1 deliberately.** No env-var expansion inside `repos.yml`; no auto-clone if the path is missing; no parallelism across repos; no retroactive labelling of pre-cycle issues. All are operator-time concerns rather than tool-time concerns. Multi-team Linear support stays out of scope too — the tool is still hardcoded to the `Personal` team.

## 6. Installed as a `uv tool`, with the secret read from `~/.drain-cycle/.env`

`drain-cycle` is installed via `uv tool install`, which puts the executable on `$PATH` in an isolated environment. The Linear API key is read from `~/.drain-cycle/.env` (shell-exported vars still win), beside the `repos.yml` config and the `runs/` logs the tool already kept there.

**Alternatives considered.**

- *`pipx`*. Functionally equivalent for installing a Python CLI in isolation. Rejected because `uv` already anchors this repo's stack (`uv.lock`, the `mise.toml` Python pin) — adding `pipx` spends an innovation token on a second tool that does the same job.
- *Publish to PyPI*. Lets anyone `uv tool install drain-cycle` by name, but buys a release-and-versioning burden — tagging, changelogs, a name on the index — that a single-operator tool doesn't earn. `uv tool install git+https://…` already covers install-from-anywhere with no release step.

**The secret-loading change this forced.** The CLI used to load `.env` only from the repo root (`Path(__file__).parent.parent`). Once installed, the package lives in the isolated tool env, where that path has no `.env` — so the key has to live somewhere stable. The load order is now shell env → `~/.drain-cycle/.env` → repo-root `.env`, first hit wins (`load_dotenv` defaults to `override=False`). The repo-root entry survives only as a dev-checkout fallback (it works under `--editable`, where `__file__` still points into the checkout); the installed tool reads the key from `~/.drain-cycle/.env`.

**Out-of-v1 deliberately.** No PyPI release. No `drain-cycle init` command to scaffold `~/.drain-cycle/` — a missing `repos.yml` already halts with an actionable message that prints the expected shape, and `docs/repos.example.yml` is a copyable template, which is enough for a single operator.

## 7. Workers default to Sonnet; a `model:` label overrides per issue

A spawned `claude -p` worker inherits whatever model the operator has globally pinned. In the diagnosed quota-burn run all five workers ran on `claude-opus-4-7` (the operator's global pin), and Opus was the single largest cost multiplier of the ~108M-token spend. Workers now default to `claude-sonnet-4-6`, passed explicitly via `--model`; an individual issue opts up (or down) with a `model:<alias>` Linear label, mirroring the `repo:<name>` mechanism. Known aliases (`sonnet`/`opus`/`haiku`) map to full ids; an unrecognised value is passed to `claude --model` verbatim.

**Why lenient, not strict.** Unlike `repo:` resolution — where a missing label is a hard halt because there is no safe default target — model resolution always has a safe fallback. So it never raises: no label, an unknown alias, or conflicting `model:` labels all fall back to the default rather than halting an unattended cycle over a label typo. The model actually used is recorded in the run log, so a mis-labelled issue surfaces after the fact instead of stalling the run.

**Alternatives considered.** (A) Keep inheriting the global pin — rejected, it is exactly what caused the burn and gives the operator no per-issue control. (B) A single global `--model` flag with no per-issue override — simpler, but a cycle legitimately mixes cheap mechanical issues with a few that warrant Opus; per-issue is the right grain. (C) Raise on ambiguous labels like `repo:` does — rejected, halting a whole unattended cycle over a duplicate label is worse than silently taking the cheap, safe default.

## 8. Workers stream stream-json; usage is parsed off the wire and the worker leads its own process group

The spawned session is launched with `claude -p --verbose --output-format stream-json` via `subprocess.Popen(..., stdout=PIPE, stderr=STDOUT, text=True, bufsize=1, start_new_session=True)`, read line by line in a reader thread. The per-issue run-log entry gains `model`, a `usage` block (the four token components + `cumulative` + `peak_context`), `cost_usd`, `num_turns`, `session_id`, `is_error`, and an explicit `duration_seconds`; the file gains top-level `cycle_cost_usd` and `cycle_tokens_cumulative`. All of this lives in `drain_cycle/worker.py`; the orchestrator calls `worker.run_issue(...)` and records the result.

**The problem.** A run that burned ~108M tokens left the operator with no on-disk record of *which issue* spent what — usage had to be reconstructed from `~/.claude/projects/*.jsonl`. Spend is the metric the cost guardrail (a downstream slice) acts on, so it has to be captured at the source.

**Why parse the stream rather than the JSONL transcript.** The transcript files are keyed by session and live outside the worktree; correlating them back to an issue after the fact is exactly the manual step this removes. The event stream is emitted by the process we already spawn, in real time, and carries everything we need.

**Token accounting — dedup by `message.id`.** The same `assistant` message is emitted *once per content block* (thinking, text, tool_use), each copy repeating the identical `message.usage`. Summing per event double-counts a turn, so usage is keyed by `message.id` and counted once. `cumulative` sums all four token components across turns — the real billed total, dominated in long tool-use loops by cache reads re-paid every turn (this is what the 108M figure was). `result.usage` is deliberately *not* used for the totals: it is only the final turn's snapshot, and it is absent entirely when a session is killed before finishing. The terminal `result` event is authoritative only for `cost_usd` (`total_cost_usd`), `num_turns`, `session_id`, `is_error`; those are `null` on a killed session.

**Why `start_new_session=True` + `os.killpg`.** The old `subprocess.run(timeout=)` killed only the direct child on timeout, orphaning grandchildren — MCP servers, sub-agents — that kept consuming. Making the worker a process-group leader and SIGKILLing the whole group on the time cap reaps them. SIGKILL with no SIGTERM grace is deliberate: a session past its deadline has no clean-shutdown work worth waiting for, and SIGKILL is the only signal a wedged grandchild cannot ignore. Killing the group also closes the stdout pipe those grandchildren inherited, which is what lets the reader thread reach EOF instead of blocking.

**Additive schema.** `grade.py` reads only `cycle_id` and `entries[].final_linear_state` / `exit_code`, so the new fields don't touch grading and pre-existing run logs grade unchanged. Entries written before any session runs (resolution and setup-failure halts) carry `null` for the worker fields but keep the same key set.

- **Parallelism.** Issues run one at a time. The Linear cycle is the unit; intra-cycle parallelism adds resource contention and serialises poorly with the agent-self-update pattern (two agents racing to mark different issues Done is fine, but two agents racing on overlapping files is not).
- **Retry.** A halted issue is not retried automatically. The operator inspects the worktree and decides — fix, redo manually, or descope.
- **Cross-cycle scheduling.** One cycle per invocation. Chaining cycles is an operator concern.
