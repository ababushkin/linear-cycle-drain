# Integration test architecture

**Linear initiative:** [Test drain-cycle failure modes and resume without a real Linear cycle](https://linear.app/ababushkin/project/test-drain-cycle-failure-modes-and-resume-without-a-real-linear-cycle-910b954af13f)

The OKR (Goal + Key Results + appetite + kill condition) is on the Linear project. This doc is the design — what gets built, why, in what order — and is the input to `shape-planning-and-task-breakdown` when the initiative is sliced into issues.

## Context

Today `drain-cycle` only tests against a real Linear cycle. Anything past happy-path becomes expensive: failure modes need deliberately broken issues, resume needs an actual halt-and-restart, and usage tracking needs real Claude sessions. Unit tests cover individual modules but stop at the function boundary — they monkey-patch `linear._post` and swap `claude -p` for marker-touching shell scripts (`tests/test_orchestrator_iteration.py:22`, `tests/test_linear_pending_issues.py:65`).

We need **scenario-level integration tests** that drive the orchestrator end-to-end against a controllable Linear surface, covering:

- **Best-case drains** — dependency-ordered execution, runlog shape, state transitions
- **Per-issue failure paths** — every halt condition in `orchestrator._drain_one_issue` (repo resolution, worktree setup, `set_state` failure, breach kill, not-Done exit)
- **Cycle-level halts** — `DependencyCycleError`, cycle-cap breach propagation
- **Resume semantics** — halt mid-cycle, re-run, observe pickup of in-flight issues reverted to Todo
- **Session/usage tracking** — `_UsageAccumulator` totals, cycle aggregates in runlog, per-issue caps triggering breaches

The drain has no native retry (deliberate, per `docs/design-decisions.md`); "retry" here means **resume** — a re-invocation against the same cycle.

## System context

The seam choice in one picture: real `claude -p`, mocked Linear, glued by an MCP shim that translates child MCP calls into GraphQL against the same in-memory store the parent hits over HTTP.

```svg
<svg viewBox="0 0 860 360" xmlns="http://www.w3.org/2000/svg" role="img" aria-label="Integration test system context: real harness, orchestrator, and claude-p on the left; mocked MockLinear, linear_mcp_shim, and shared in-memory store on the right.">
  <defs>
    <marker id="arr" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" orient="auto-start-reverse">
      <path d="M0,0 L10,5 L0,10z" fill="currentColor"/>
    </marker>
    <marker id="arr-accent" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" orient="auto-start-reverse">
      <path d="M0,0 L10,5 L0,10z" fill="var(--accent)"/>
    </marker>
  </defs>

  <rect x="300" y="8" width="540" height="344" rx="14" fill="none" stroke="var(--muted)" stroke-width="1.5" stroke-dasharray="6 5"/>
  <text x="820" y="26" text-anchor="end" font="700 11px 'Inter', ui-sans-serif, sans-serif" fill="var(--muted)" letter-spacing="0.08em">MOCKED</text>

  <g class="p-node">
    <rect x="40" y="30" width="200" height="60" rx="10"/>
    <text x="140" y="56" text-anchor="middle" class="p-node-title">IntegrationHarness</text>
    <text x="140" y="74" text-anchor="middle" class="p-node-sub">pytest fixture</text>
  </g>
  <g class="p-node">
    <rect x="40" y="150" width="200" height="60" rx="10"/>
    <text x="140" y="176" text-anchor="middle" class="p-node-title">orchestrator.run()</text>
    <text x="140" y="194" text-anchor="middle" class="p-node-sub">parent · real</text>
  </g>
  <g class="p-node">
    <rect x="40" y="270" width="200" height="60" rx="10"/>
    <text x="140" y="296" text-anchor="middle" class="p-node-title">claude -p</text>
    <text x="140" y="314" text-anchor="middle" class="p-node-sub">child · real · per issue</text>
  </g>

  <g class="p-node">
    <rect x="340" y="30" width="200" height="60" rx="10"/>
    <text x="440" y="56" text-anchor="middle" class="p-node-title">MockLinear</text>
    <text x="440" y="74" text-anchor="middle" class="p-node-sub">FastAPI · GraphQL</text>
  </g>
  <g class="p-node">
    <rect x="340" y="270" width="200" height="60" rx="10"/>
    <text x="440" y="296" text-anchor="middle" class="p-node-title">linear_mcp_shim</text>
    <text x="440" y="314" text-anchor="middle" class="p-node-sub">stdio MCP server</text>
  </g>
  <g class="p-node p-node-emphasis">
    <rect x="620" y="150" width="200" height="60" rx="10"/>
    <text x="720" y="176" text-anchor="middle" class="p-node-title">in-memory store</text>
    <text x="720" y="194" text-anchor="middle" class="p-node-sub">cycle · issues · call log</text>
  </g>

  <path d="M240 56 L340 56" marker-end="url(#arr)" class="p-edge"/>
  <text x="290" y="48" text-anchor="middle" class="p-edge-label">populate</text>

  <path d="M140 90 L140 150" marker-end="url(#arr)" class="p-edge"/>
  <text x="160" y="125" class="p-edge-label">run()</text>

  <path d="M240 175 L340 70" marker-end="url(#arr)" class="p-edge"/>
  <text x="298" y="118" class="p-edge-label">GraphQL HTTP</text>

  <path d="M140 210 L140 270" marker-end="url(#arr)" class="p-edge"/>
  <text x="160" y="245" class="p-edge-label">spawn</text>

  <path d="M240 300 L340 300" marker-end="url(#arr-accent)" class="p-edge-async"/>
  <text x="290" y="292" text-anchor="middle" class="p-edge-label">MCP stdio</text>

  <path d="M440 270 L440 90" marker-end="url(#arr)" class="p-edge"/>
  <text x="460" y="185" class="p-edge-label">GraphQL POST</text>

  <path d="M540 70 L620 170" marker-end="url(#arr)" class="p-edge"/>
  <text x="565" y="115" class="p-edge-label">writes</text>

  <path d="M540 290 L620 195" marker-end="url(#arr)" class="p-edge"/>
  <text x="565" y="255" class="p-edge-label">writes</text>
</svg>
```

Solid = control / data flow over HTTP or subprocess. Dashed (accent) = MCP stdio between the child and the shim. The dashed boundary marks what's mocked: every byte that would otherwise hit `api.linear.app` is intercepted, but `claude -p` itself is the production binary every run. Both the parent's HTTP calls and the child's MCP calls land in one shared store, so scenarios assert against one unified call log.

## Workflow

1. **`/shape-initiative`** — done. Linear project created with the six-field OKR (linked above).
2. **`shape-planning-and-task-breakdown`** — convert the *Slices* section below into Linear issues under the new initiative; assign slices 1–4 to the current cycle, 5–12 to the backlog.
3. **`agent-skills:test-driven-development`** during build — each slice ships a failing scenario before the supporting infra.
4. **`agent-skills:code-review-and-quality`** per slice before commit, per `AGENTS.md`.

## Approach

**Parent HTTP mock + Linear-MCP shim + real `claude -p` + in-process driver.** The spawned session is real claude every time; only the Linear surface is mocked. Three pillars.

**Alternatives rejected** (draft for the ADR slice):

- **Scripted/fake `claude -p`.** Cheaper, faster, deterministic. Rejected because a scripted child stops being an integration test — it cannot validate the real session's usage stream, MCP tool calls, prompt-to-completion behaviour, or production OTel spans against actual data.
- **Hybrid: real claude only on happy-path; scripted child for failure determinism.** Two patterns to maintain; weaker default signal; gives up real-claude coverage on usage-tracking edge cases.
- **Function-injection / `LinearClient` protocol on the parent.** Cheaper than HTTP but skips the HTTP boundary and adds an abstraction layer that earns nothing else. The existing module-level functions in `linear.py` drive easily against an HTTP fake once `LINEAR_API_URL` is overridable.
- **VCR-style recorded fixtures.** Realistic but awkward for failure injection, expensive to maintain under schema drift.

Slice 1 produces a short ADR at `docs/adrs/0001-integration-test-architecture.md` recording the chosen approach, these rejected alternatives, and revisit conditions.

### Cost, runtime, and determinism

Real claude makes the suite costly, slow, and non-deterministic on numbers.

- **Cost / runtime budget**: ~$0.05–$0.20 per spawning scenario, ~15–30 seconds per scenario. Full suite of ~30 scenarios: ~$1–$3 and ~15 minutes. Run on demand, not as part of the default suite.
- **Default `pytest` run** covers only the pre-spawn halt scenarios (no claude spawned) — fast and deterministic, suitable for repeated local invocation while iterating.
- **Determinism strategy** — split assertions:
  - **Exact** — exit codes, final Linear state, halt reason class, runlog file presence, structural shape of runlog JSON, set of MCP tool calls observed.
  - **Predicate** — `tokens > 0`, `0 < cost_usd < per_issue_cost_usd`, `num_turns >= 1`, `model is not None`. No exact values.
  - **Tolerant snapshot** — `dirty-equals` matches keys and types, ignores values.
- **Cost guard**: scenarios that spawn claude carry `@pytest.mark.real_claude`. Default `pytest` invocation excludes the marker; the `real_claude` slice opts in via `pytest -m real_claude` and runs on demand. `ANTHROPIC_API_KEY` required.

### Pillar 1 — `MockLinear`: an in-process fake GraphQL server

A FastAPI/Starlette app on a random localhost port, started per-test via a pytest fixture. It speaks the five GraphQL operations `linear.py` uses (`current_cycle`, `pending_issues`, `get_issue`, `set_state`, `resolve_state` / WorkflowState lookup) plus an `IssueUpdate` mutation the scripted child hits to flip an issue to Done.

State lives in a typed in-memory store: `Cycle`, `Issue`, `Label`, `WorkflowState`. The store records every call (operation, variables, timestamp) so tests can assert call sequence and counts.

Fault injection is **per-operation, per-call-index**: `mock.fail_after(op="set_state", call=2, mode="http_500")`. Modes: `http_500`, `http_429`, `graphql_error`, `malformed_json`, `slow_response(seconds)`, `connection_reset`. The drain has no retry, so "fail" means "this single call breaks" — tests then verify the halt path.

**Production change required**: `linear.py:18` hard-codes `https://api.linear.app/graphql`. Replace with `os.environ.get("LINEAR_API_URL", "https://api.linear.app/graphql")`. No CLI flag — env var only.

### Pillar 2 — `linear_mcp_shim`: a Linear MCP server backed by the mock

A small Python stdio MCP server (built on the official `mcp` SDK) that exposes the two tools the drain-cycle prompt uses today: `save_issue` (state transitions) and `save_comment`. Each tool translates its call into a GraphQL POST against `MockLinear`, sharing the same backing store as the parent's HTTP surface.

The harness writes a custom `.mcp.json` into each test worktree registering the shim under the name `linear` (matching the existing tool prefix `mcp__claude_ai_Linear__*`). The orchestrator already symlinks `.mcp.json` from the configured source into the worktree (`orchestrator.py:257-259`); the harness arranges for the test source to be the shim-configured file.

The shim records every tool call (tool name, arguments, timestamp) on the shared store, so scenarios assert against both parent GraphQL calls and child MCP calls through one call log.

**Why an MCP shim and not just `LINEAR_API_URL` in the child env.** MCP is a separate protocol from raw GraphQL; the official Linear MCP server speaks MCP over stdio to claude and HTTPS to Linear. Setting `LINEAR_API_URL` in the child has no effect — there's no way to redirect the official server without forking it. A purpose-built shim is the smallest seam.

#### Happy-path sequence

The seam closes end-to-end. One issue, picked up and driven to Done:

```svg
<svg viewBox="0 0 860 500" xmlns="http://www.w3.org/2000/svg" role="img" aria-label="Sequence diagram of a single-issue happy-path drain showing messages between Harness, orchestrator, MockLinear, claude-p, and linear_mcp_shim.">
  <defs>
    <marker id="seq-arr" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" orient="auto-start-reverse">
      <path d="M0,0 L10,5 L0,10z" fill="currentColor"/>
    </marker>
    <marker id="seq-arr-accent" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" orient="auto-start-reverse">
      <path d="M0,0 L10,5 L0,10z" fill="var(--accent)"/>
    </marker>
  </defs>

  <g class="p-node">
    <rect x="20" y="20" width="120" height="44" rx="8"/>
    <text x="80" y="47" text-anchor="middle" class="p-node-title">Harness</text>
  </g>
  <g class="p-node">
    <rect x="180" y="20" width="140" height="44" rx="8"/>
    <text x="250" y="47" text-anchor="middle" class="p-node-title">orchestrator</text>
  </g>
  <g class="p-node">
    <rect x="360" y="20" width="140" height="44" rx="8"/>
    <text x="430" y="47" text-anchor="middle" class="p-node-title">MockLinear</text>
  </g>
  <g class="p-node">
    <rect x="540" y="20" width="120" height="44" rx="8"/>
    <text x="600" y="47" text-anchor="middle" class="p-node-title">claude -p</text>
  </g>
  <g class="p-node">
    <rect x="700" y="20" width="140" height="44" rx="8"/>
    <text x="770" y="47" text-anchor="middle" class="p-node-title">linear_mcp_shim</text>
  </g>

  <line x1="80"  y1="64" x2="80"  y2="475" stroke="var(--rule)" stroke-width="1.5" stroke-dasharray="4 4"/>
  <line x1="250" y1="64" x2="250" y2="475" stroke="var(--rule)" stroke-width="1.5" stroke-dasharray="4 4"/>
  <line x1="430" y1="64" x2="430" y2="475" stroke="var(--rule)" stroke-width="1.5" stroke-dasharray="4 4"/>
  <line x1="600" y1="64" x2="600" y2="475" stroke="var(--rule)" stroke-width="1.5" stroke-dasharray="4 4"/>
  <line x1="770" y1="64" x2="770" y2="475" stroke="var(--rule)" stroke-width="1.5" stroke-dasharray="4 4"/>

  <path d="M80 95 L430 95" marker-end="url(#seq-arr)" class="p-edge"/>
  <text x="255" y="88" text-anchor="middle" class="p-edge-label">populate(Cycle, Issue)</text>

  <path d="M80 130 L250 130" marker-end="url(#seq-arr)" class="p-edge"/>
  <text x="165" y="123" text-anchor="middle" class="p-edge-label">run(repos, limits)</text>

  <path d="M250 170 L430 170" marker-end="url(#seq-arr)" class="p-edge"/>
  <text x="340" y="163" text-anchor="middle" class="p-edge-label">pending_issues</text>
  <path d="M430 195 L250 195" marker-end="url(#seq-arr)" class="p-edge" stroke-dasharray="4 4"/>
  <text x="340" y="210" text-anchor="middle" class="p-edge-label">[Issue]</text>

  <path d="M250 240 L430 240" marker-end="url(#seq-arr)" class="p-edge"/>
  <text x="340" y="233" text-anchor="middle" class="p-edge-label">set_state(In-Progress)</text>

  <path d="M250 280 L600 280" marker-end="url(#seq-arr)" class="p-edge"/>
  <text x="425" y="273" text-anchor="middle" class="p-edge-label">spawn</text>

  <path d="M600 320 L770 320" marker-end="url(#seq-arr-accent)" class="p-edge-async"/>
  <text x="685" y="313" text-anchor="middle" class="p-edge-label">get_issue (MCP)</text>
  <path d="M770 345 L430 345" marker-end="url(#seq-arr)" class="p-edge"/>
  <text x="600" y="338" text-anchor="middle" class="p-edge-label">GraphQL get_issue</text>

  <path d="M600 385 L770 385" marker-end="url(#seq-arr-accent)" class="p-edge-async"/>
  <text x="685" y="378" text-anchor="middle" class="p-edge-label">save_issue(Done) · save_comment</text>
  <path d="M770 410 L430 410" marker-end="url(#seq-arr)" class="p-edge"/>
  <text x="600" y="403" text-anchor="middle" class="p-edge-label">GraphQL set_state(Done)</text>

  <path d="M250 455 L430 455" marker-end="url(#seq-arr)" class="p-edge"/>
  <text x="340" y="448" text-anchor="middle" class="p-edge-label">get_issue (verify Done)</text>
</svg>
```

The parent (left half) talks to `MockLinear` directly over HTTP. The child (right half) reaches the same store indirectly, via `linear_mcp_shim` over MCP stdio. Both write into the in-memory store, so the test's call log records the full conversation across both surfaces — the parent's `pending_issues` / `set_state` calls and the child's `save_issue` / `save_comment` calls interleave in one ordered list that scenarios assert against.

### How scenarios steer claude

Scenarios drive child behaviour through three knobs the harness sets per-scenario:

- **Issue body** — written into `MockLinear` and read by claude via `mcp__claude_ai_Linear__get_issue`. The body is the natural prompt seam (e.g. "create `<id>.txt` containing 'hello'; then mark this issue Done"). Library lives in `tests/integration/prompts/*.md`.
- **Per-issue limits** — `limits.yml` overrides (e.g. `per_issue_tokens=2000`, `per_issue_seconds=30`) used with verbose prompts to provoke breach scenarios deterministically.
- **Claude flags** — the harness may inject `--max-turns 1` for "exit without Done" cases (claude exits before completing the workflow), via the `_CLAUDE_CMD` patch point at `orchestrator.py:25`.

The drain-cycle prompt (`drain_cycle/prompt.py:39-42`) stays unchanged — same prompt the real drain uses.

### Pillar 3 — `IntegrationHarness`: a fixture that drives `orchestrator.run()` in-process

A pytest fixture that:

- Creates a temp `HOME` so `~/.drain-cycle/repos.yml`, `~/.drain-cycle/limits.yml`, and `~/.drain-cycle/runs/` redirect to a tmpdir (`monkeypatch.setenv("HOME", ...)` plus any path-resolution helpers in `cli.py` / `runlog.py`).
- Initialises one or more real git repos in tmpdir (existing pattern in `test_orchestrator_iteration.py`).
- Writes a `repos.yml` mapping label names to those repo paths.
- Writes a `.mcp.json` in each repo registering the Linear-MCP shim under `linear`.
- Starts `MockLinear` and populates it with the scenario's cycle, issues (with prompt body), and labels.
- Sets `LINEAR_API_URL`, `LINEAR_API_KEY=test`, `MOCK_LINEAR_URL=http://localhost:<port>`, `OTEL_SDK_DISABLED=true` env vars. Verifies `ANTHROPIC_API_KEY` is set for scenarios that spawn claude.
- Optionally appends extra flags to `_CLAUDE_CMD` (e.g. `--max-turns 1`) for scenarios that need to coerce claude's behaviour.
- Calls `orchestrator.run(repos, limits)` directly and returns a `Result`: exit code, runlog JSON, parent GraphQL call log, child MCP call log, final mock state.

Tests assert against `Result`. One scenario per `test_*` function. Helpers (`make_cycle`, `make_issue`, `expect_runlog`, `expect_calls`) keep scenarios short.

**Per-scenario contract.** Every scenario declares:

1. **Setup** — `Cycle`, `Issue[]`, `Label[]`, `WorkflowState[]`, repos.yml mapping, limits.yml overrides, transcript fixture name(s), any fault injection.
2. **Expected exit code** — orchestrator return.
3. **Expected runlog assertions** — a list of (a) structural shape (keys, types) matched via `dirty-equals`, (b) predicate assertions on numeric fields (`tokens > 0`, `cost_usd < limits.per_issue_cost_usd`), (c) exact assertions on deterministic fields (`final_linear_state`, `halt_reason`).
4. **Expected final mock state** — issue states the mock should end in (e.g. `{ "ABA-1": "Done", "ABA-2": "Todo" }` after a halt on issue 2).
5. **Expected call sequence** — ordered `(surface, operation, predicate-on-args)` tuples across both parent GraphQL and child MCP calls. Strict by default; `expect_calls(strict=False)` for tolerant cases where claude's tool-call order varies between runs.

A collection-time lint test fails any scenario missing (3), (4), or (5).

## Production code changes required

Minimal. Every test-only seam reuses existing structure.

1. **`drain_cycle/linear.py:18`** — `_GRAPHQL_URL` reads from `LINEAR_API_URL` env var, default unchanged. One-line change.
2. **`drain_cycle/runlog.py`** — verify the run-log directory honours `HOME` re-resolution; slice 1 greps for `Path.home`, `expanduser`, and `HOME`. If the path caches at module load, add an accessor (e.g. `_run_log_dir()`) so the harness re-resolves after `monkeypatch.setenv("HOME", ...)`.
3. **`drain_cycle/orchestrator.py:25`** — `_CLAUDE_CMD` is already module-level. Tests patch it.

No new abstraction layers, no `LinearClient` Protocol — the existing module-level functions drive easily against an HTTP fake once the URL is configurable.

## Test-infra correctness checks (one-time pre-slice work)

Risks the plan-review surfaced. Each lands before dependent slices.

- **Telemetry isolation** — drain-cycle wires httpx OTel auto-instrumentation in `telemetry.py`. The harness sets `OTEL_SDK_DISABLED=true` to keep tests from exporting spans. Confirmed in slice 2.
- **MCP-shim correctness** — the shim must implement the same tool schemas the official Linear MCP exposes for `save_issue` and `save_comment`, otherwise claude's tool calls land with wrong shapes. Slice 3 spike: capture the official schemas (from the Linear MCP docs or by running the real one briefly), implement matching ones in the shim, verify a single happy-path drain reaches Done end-to-end. **Kill-switch**: if the schemas drift between Linear MCP versions, pin the shim to a captured snapshot and add a periodic check.
- **Breach scenario reproducibility** — with real claude, breach scenarios depend on prompts that reliably exceed tight per-issue caps. Slice 8 spike: confirm a verbose prompt (e.g. "write a 2000-line poem") plus `per_issue_tokens=1000` reliably triggers token breach across 5 consecutive runs. **Kill-switch**: if not reliable, fall back to time-breach only (tight `per_issue_seconds` plus a "sleep 60s" prompt is robust because the breach loop polls wall-clock).

## Failure-mode coverage

The matrix below names every halt path in the production code and the scenario that covers it. KR 2's halt-coverage guard fails the suite when a new halt slug lands without a matching row.

| Halt path | Source | Scenario | Spawns claude? | Slice |
|---|---|---|---|---|
| Missing `repo:` label | `orchestrator.py` repo-resolve | `test_pre_spawn_halts::test_missing_repo_label` | no | 7 |
| `repo:` label → nonexistent path | `orchestrator.py` repo-resolve | `test_pre_spawn_halts::test_unknown_repo_path` | no | 7 |
| Worktree `add` fails | `orchestrator.py` worktree | `test_pre_spawn_halts::test_dirty_worktree` | no | 7 |
| `set_state` (Todo→In-Progress) HTTP 500 | `linear.py` set_state | `test_pre_spawn_halts::test_set_state_500` | no | 6 |
| Child exits without setting Done | `orchestrator._drain_one_issue` | `test_real_claude_halts::test_not_done_exit` | yes (`--max-turns 1`) | 8 |
| Child exits non-zero | `orchestrator._drain_one_issue` | `test_real_claude_halts::test_child_nonzero` | yes | 8 |
| Per-issue token cap exceeded | `worker.py` breach loop | `test_breach::test_token_cap` | yes (verbose prompt) | 9 |
| Per-issue time cap exceeded | `worker.py` breach loop | `test_breach::test_time_cap` | yes (`wait 60s` prompt) | 9 |
| Revert `set_state` (In-Progress→Todo) fails after halt | `orchestrator.py` revert | `test_real_claude_halts::test_revert_failure` | either | 8 |
| `DependencyCycleError` from `pending_issues` | `linear.py` pending_issues | `test_cycle_halts::test_dep_cycle` | no | 10 |
| Cycle-cap breach after N Done | `worker.py` cycle accumulator | `test_cycle_halts::test_cycle_cap` | yes | 10 |
| Linear 429 / malformed JSON / slow-beyond-timeout | `linear._post` | `test_linear_edges::*` | no | 12 |

Source paths are illustrative — the halt-coverage guard (`test_halt_coverage`) reads them from `mark_error` slugs in `drain_cycle/**.py` at suite time. A new halt slug with no matching row fails the suite loudly.

## Scenario catalogue (initial coverage)

Grouped by area; one test function each. Target end-state, not a single slice — slices below build the infra and add scenarios incrementally.

Scenarios that spawn `claude -p` carry `@pytest.mark.real_claude` (excluded from the default `pytest` run; opt-in via `-m real_claude`). Scenarios that halt pre-spawn run on every default invocation.

**Best case** *(real_claude)*
- 3-issue dependency-ordered drain reaches Done in order; runlog totals positive and sum across issues; child MCP calls include `save_issue` (transition to Done) and `save_comment` (summary) per issue
- Mixed Todo + Backlog states picked up; correct planning
- Single-issue drain
- Empty cycle exits cleanly *(no claude spawned)*

**Per-issue halts** (`orchestrator._drain_one_issue` paths)
- Missing `repo:` label → halt + reason *(no claude spawned)*
- `repo:` label resolves to nonexistent path → halt *(no claude spawned)*
- Worktree `add` fails (dirty tree) → halt *(no claude spawned)*
- `set_state` (Todo→In-Progress) fails with HTTP 500 → halt *(no claude spawned)*
- Child exits without setting Done *(real_claude; coerced via `--max-turns 1`)* → halt + reason matches `final_linear_state`
- Child exits non-zero *(real_claude; provoked by an invalid prompt or a tool that errors)* → halt
- Per-issue token cap exceeded mid-stream *(real_claude; verbose prompt + `per_issue_tokens=1000`)* → breach kill; runlog records breach scope/metric/limit/observed
- Per-issue time cap exceeded *(real_claude; prompt "wait 60s" + `per_issue_seconds=10`)* → time breach recorded
- Revert `set_state` (In-Progress→Todo) fails after a halt → halt-reason concatenates *(real_claude or pre-spawn)*

**Cycle-level halts**
- `DependencyCycleError` from `pending_issues` → halt before any issue attempted; `cycle_halt_reason` set; runlog otherwise empty *(no claude spawned)*
- After two Done issues, cycle-cap breach detected → third issue not attempted; halt reason names cumulative metric *(real_claude)*

**Resume** *(real_claude)*
- Run halts on issue 2; re-run with the same cycle picks up issue 2 (mock has it back at Todo via the revert) and issue 3; second runlog file separate from first; final mock state correct

**Session / usage tracking** *(real_claude; predicate assertions on numbers)*
- Single-issue drain produces `usage.cumulative.input_tokens > 0`, `usage.cumulative.output_tokens > 0`, `peak_context > 0`, `cost_usd > 0`, `num_turns >= 1`, `session_id` non-empty, `is_error == false`, `model` non-empty
- Multi-issue drain: cycle aggregates (`cycle_tokens_cumulative`, `cycle_cost_usd`, `cycle_duration_seconds`) equal the sum of per-issue values
- Worker dedup invariant (`worker.py:416`) verified indirectly: re-emit a non-deterministic prompt and confirm `usage.cumulative` is monotonically non-decreasing within a turn; explicit dedup behaviour stays unit-tested

**Linear edge cases** *(no claude spawned — parent halts before pre-spawn `set_state` succeeds)*
- 500, 429, GraphQL error, malformed JSON, slow-within-timeout (drain proceeds), slow-beyond-timeout (halts)

**Out of scope for MVP**
- Honeycomb/OTel span assertions (separate slice — needs a fake OTLP collector; production code wires spans cleanly per `drain_cycle/telemetry.py`)
- True MCP-server fake for children (scripted-child + direct GraphQL POST satisfies the contract)

**In MVP, bounded** — slice 4 ships one subprocess smoke test that spawns the real `drain-cycle` binary against the mock and asserts exit code plus runlog presence. This single test covers `cli.py`'s secrets-load order and process exit-code path. Larger subprocess scenarios stay out of scope.

**Note on the worker `_UsageAccumulator` dedup invariant** — the worker dedupes by `message.id` (`worker.py:416`). Real claude rarely emits duplicate ids in practice, so the dedup branch is exercised by existing unit tests, not by integration scenarios. Plan accordingly: keep the dedup unit test (`test_usage_accumulator_dedup.py` or equivalent); the integration suite asserts on monotonicity, not on dedup directly.

## Slices (verifiable build order)

Each slice ships in one session and ends with a passing scenario.

1. **Production seam + ADR** — `LINEAR_API_URL` env var in `linear.py`; unit test confirms override; default unchanged. Runlog `HOME` re-resolution verified (grep + accessor if needed). **Deliverable**: `docs/adrs/0001-integration-test-architecture.md` recording the chosen approach, rejected alternatives, and revisit conditions.
2. **`MockLinear` skeleton** — FastAPI app with `current_cycle` and `pending_issues` only; serves a hard-coded scenario; one unit test wires through `linear.py` and confirms the call lands on the mock. Harness sets `OTEL_SDK_DISABLED=true`.
3. **`linear_mcp_shim` + first real-claude drain** — stdio MCP server implementing `save_issue` and `save_comment` against the mock; harness writes a `.mcp.json` registering it; first **real-claude** happy-path drain of one issue reaches Done end-to-end. Schemas captured from the official Linear MCP. Marked `@pytest.mark.real_claude`.
4. **`IntegrationHarness` fixture + subprocess smoke** — tmpdir HOME, repos.yml, real git repo, `.mcp.json` writer, mock startup. Slice 3's scenario refactored onto the harness. **Plus**: one subprocess test spawns `drain-cycle` against the mock end-to-end and asserts exit code plus runlog file existence.
5. **Mock GraphQL completeness + drift guard** — add `get_issue`, `set_state`, WorkflowState resolution; happy-path drain of 3 ordered issues passes. **Plus**: `test_linear_surface_audit` fails when any `_post(operation=...)` call site in `linear.py` lacks a mock handler.
6. **Fault injection** — per-operation, per-call-index fault modes in the mock; scenario covers `set_state` HTTP 500 halt (pre-spawn, fast). Per-scenario contract enforced (runlog assertions, mock state, call sequence).
7. **Pre-spawn halt scenarios** — repo-resolution failures, worktree-add failure, set_state 500. Fast, no claude.
8. **Real-claude halt scenarios** — child exits without Done (via `--max-turns 1`); child non-zero exit (via invalid prompt). Marked `real_claude`.
9. **Breach scenarios** — time-breach first (tight `per_issue_seconds` plus "wait 60s" prompt — robust). Token-breach gated on the slice-8 reproducibility spike. Both `real_claude`.
10. **Cycle-level halt scenarios** — `DependencyCycleError` (no claude); cycle-cap breach after two Done issues (`real_claude`).
11. **Resume scenario** — drive a halt, re-invoke orchestrator on the same mock cycle, assert second runlog file and final state. `real_claude`.
12. **Usage-tracking + Linear edge-case scenarios** — usage predicates on a happy-path drain (`real_claude`); 429 / malformed / slow-timeout halts on the parent (no claude).

Slices 1–4 are the foundation; 5–12 are independent and ship in any order once 4 lands. Each slice opens a Linear issue under the initiative and closes per `AGENTS.md` (review → fix → commit + push → Linear summary comment → Done).

## Critical files

To modify:
- `drain_cycle/linear.py` — env-var override for GraphQL URL (slice 1)
- `drain_cycle/runlog.py` — verify `HOME` re-resolution; add small accessor only if cached at module load (slice 1)

To create:
- `docs/adrs/0001-integration-test-architecture.md` — chosen approach + rejected alternatives + revisit conditions (slice 1)
- `tests/integration/conftest.py` — harness fixture, `real_claude` marker config
- `tests/integration/mock_linear.py` — FastAPI GraphQL server + shared in-memory store + fault injection
- `tests/integration/linear_mcp_shim.py` — stdio MCP server backed by the shared store
- `tests/integration/prompts/*.md` — issue-body prompts (happy_path, exit_without_done, verbose, wait, etc.)
- `tests/integration/test_happy_path.py`, `test_pre_spawn_halts.py`, `test_real_claude_halts.py`, `test_breach.py`, `test_cycle_halts.py`, `test_resume.py`, `test_usage_tracking.py`, `test_linear_edges.py`, `test_linear_surface_audit.py`, `test_subprocess_smoke.py` — scenarios

To reuse from existing tests:
- Real git-repo setup pattern in `tests/test_orchestrator_iteration.py:22-90`
- Linear fixture style in `tests/test_linear_pending_issues.py:65-70`
- `_CLAUDE_CMD` patch point at `drain_cycle/orchestrator.py:25`

## Verification

End-to-end:
- `uv run pytest tests/integration/ -v` — non-`real_claude` scenarios green; no real Linear network calls; no real `LINEAR_API_KEY` leaking in. The default local invocation.
- `uv run pytest tests/integration/ -m real_claude -v` — full suite green against a real Anthropic API key. Run on demand; instructions live in `tests/integration/README.md`.
- `uv run pytest --cov=drain_cycle/orchestrator --cov=drain_cycle/worker --cov=drain_cycle/runlog tests/integration/` — coverage on orchestrator, worker, and runlog rises meaningfully.
- Each slice closes per `AGENTS.md`: review → fix → commit + push to main → Linear summary comment → Done.

**Halt-coverage assertion** (enforces KR 2):
- `test_halt_coverage` greps `mark_error` slugs from `drain_cycle/**.py`, collects the union of `exception.slug` attributes (or `halt_reason` strings) observed across the full integration run (including `real_claude`), and asserts equality. Fails locally when a new halt slug lands in production code without a covering scenario.

Negative checks during build:
- `grep -r "api.linear.app" tests/` returns nothing — no accidental real-Linear calls in fixtures.
- The mock's call log (parent GraphQL + child MCP) records every operation a scenario expects; an unexpected call fails the test loudly.
- `test_linear_surface_audit` fails when any `_post(operation=...)` call site in `linear.py` lacks a mock handler.
- Cost budget guard: the `real_claude` suite prints total Anthropic spend at the end of each run; manual review flags runs over $5 (catch a runaway loop scenario early).
