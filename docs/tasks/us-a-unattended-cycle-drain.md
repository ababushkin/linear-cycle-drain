# Tasks: us-a-unattended-cycle-drain

Design doc: ../../README.md (§ "Design decisions" — the project uses README in place of a separate design doc; see ADRs-are-heavier-than-needed note in AGENTS.md)
Linear issue: [ABA-194 — US-A — Unattended cycle drain](https://linear.app/ababushkin/issue/ABA-194/us-a-unattended-cycle-drain)
Last updated: 2026-05-21

## Confirmed inputs

- **Orchestrator → Linear:** direct GraphQL against `https://api.linear.app/graphql` with `LINEAR_API_KEY` from env. No Python Linear SDK dependency.
- **Worktree base branch:** hard-coded `main` per README §3.

## Task list

Each task below is mirrored as a standalone Linear sub-issue of ABA-194 with full scope/test-plan/out-of-scope context and Linear-native `blockedBy` dependencies. The Linear ticket is the executable artefact; this file is the planning index.

### Task 1 — Walking skeleton: drain one trivial issue end-to-end
**Linear:** [ABA-198](https://linear.app/ababushkin/issue/ABA-198)
**Description:** Bootstrap `pyproject.toml`, package skeleton, and `drain-cycle` CLI entry point; resolve the current cycle via Linear GraphQL; fetch one Todo/Backlog issue; create `.worktrees/<issue-identifier>/` off `main` in cwd; spawn `claude -p --dangerously-skip-permissions` with a minimal prompt instructing the agent to complete the issue and mark it Done; wait for exit; re-fetch the issue; remove the worktree if Done; exit 0.
**Done when:** `drain-cycle`, invoked in a target repo whose current Linear cycle holds exactly one trivially-completable Todo/Backlog issue, exits 0 with the issue ending in Done (`In Progress → Done` visible in Linear history), the worktree directory removed, and no operator prompt at any point.
**Dependencies:** none.

### Task 2 — Sort cycle issues by priority then manual order
**Linear:** [ABA-199](https://linear.app/ababushkin/issue/ABA-199) (blockedBy: ABA-198)
**Description:** Linear client returns Todo/Backlog issues sorted by Linear `priority` (Urgent → No-priority), tiebroken by `sortOrder` (manual order) within the cycle. Note: `priority: 0` (No priority) sorts last, not first.
**Done when:** an automated unit test against a stub Linear response of ≥3 mixed-priority issues asserts the returned list matches the documented ordering.
**Dependencies:** Task 1 (ABA-198).

### Task 3 — Iterate the orchestrator across all qualifying issues
**Linear:** [ABA-200](https://linear.app/ababushkin/issue/ABA-200) (blockedBy: ABA-198, ABA-199)
**Description:** The orchestrator processes every Todo/Backlog issue in the sorted list, sequentially, until the list is exhausted — not just the first.
**Done when:** an integration test (stub Linear with 3+ issues, real worktree creation, no-op spawned-process substitute that marks each issue Done) drives the orchestrator to process every issue in order and remove every worktree.
**Dependencies:** Task 1 (ABA-198), Task 2 (ABA-199).

### Task 4 — Complete prompt template
**Linear:** [ABA-201](https://linear.app/ababushkin/issue/ABA-201) (blockedBy: ABA-198)
**Description:** The prompt sent to each spawned `claude -p` session contains, in order: the issue title, the issue description body, an execution-instructions preamble (working directory, expectations, base branch), and the mandatory tail "when complete, move this issue to Done via Linear MCP".
**Done when:** an automated test renders the prompt for a fixture issue and asserts each of the four required segments is present in the documented order.
**Dependencies:** Task 1 (ABA-198).

### Task 5 — Stop on not-Done after spawn
**Linear:** [ABA-202](https://linear.app/ababushkin/issue/ABA-202) (blockedBy: ABA-200)
**Description:** If a spawned session exits with the issue still not in Done state, the orchestrator stops processing further issues and exits non-zero. (Halt-message formatting and worktree preservation are deliberately left to US-B/ABA-195; this slice only guarantees US-A does not silently advance.)
**Done when:** `drain-cycle` against a cycle containing a deliberately-uncompletable test issue exits non-zero and leaves subsequent Todo/Backlog issues untouched in Linear; an integration test asserts both conditions.
**Dependencies:** Task 3 (ABA-200).

### Task 6 — Acceptance validation against ABA-194
**Linear:** [ABA-203](https://linear.app/ababushkin/issue/ABA-203) (blockedBy: ABA-198, ABA-199, ABA-200, ABA-201, ABA-202)
**Description:** Run one end-to-end smoke against a real Linear cycle containing 2+ trivially-completable issues, with no stubs anywhere in the loop, and check each of US-A's five acceptance bullets explicitly. (Tasks 1–5 verify slices; this verifies US-A as a whole.)
**Done when:** a comment on ABA-194 records the cycle ID, the run timestamp, and a per-bullet pass/waive table covering: (1) no operator prompts, (2) every issue reached Done with `In Progress → Done` visible in Linear history, (3) every worktree removed, (4) exit 0, (5) run recorded in US-C's artefact — explicitly waived with a `WAIVED-PENDING-US-C` marker if ABA-196 has not yet landed.
**Dependencies:** Tasks 1–5 (ABA-198, ABA-199, ABA-200, ABA-201, ABA-202).

## Open questions

None — Linear access mechanism and worktree base branch were resolved before this plan was finalised.
