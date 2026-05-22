# Tasks: us-c-artefacts-that-grade-the-krs

Design doc: ../../README.md (§ "Design decisions" — the project uses README in place of a separate design doc; see ADRs-are-heavier-than-needed note in AGENTS.md)
Linear issue: [ABA-196 — US-C — Artefacts that grade the KRs](https://linear.app/ababushkin/issue/ABA-196/us-c-artefacts-that-grade-the-krs)
Last updated: 2026-05-22

## Confirmed inputs

- **Log location:** `~/.drain-cycle/runs/<cycle-id>.json` per ABA-196 acceptance and README usage section.
- **Owner:** orchestrator-side, not agent-side. Mirrors the ABA-209 decision that the orchestrator owns `Todo → In Progress` while the agent owns `… → Done`.
- **Time tracking:** explicitly excluded. `time_spent` is initialised to `null` and the operator self-reports `scoping_hours` / `validation_hours` / `execution_hours` at cycle close.

## Task list

Each task below is mirrored as a standalone Linear sub-issue of ABA-196 with full scope/test-plan/out-of-scope context and Linear-native `blockedBy` dependencies. The Linear ticket is the executable artefact; this file is the planning index.

### Task 1 — Walking skeleton: runlog module + orchestrator wiring (happy path)
**Linear:** [ABA-215](https://linear.app/ababushkin/issue/ABA-215)
**Description:** Add `drain_cycle/runlog.py` with a `RunLog` type that initialises `~/.drain-cycle/runs/<cycle-id>.json` with `{cycle_id, time_spent: null, entries: []}` and persists incrementally on `append_entry(...)`. Wire `orchestrator.run()` to capture `started_at` / `finished_at` around each spawn (and `exit_code` from `subprocess.run` returncode), and append one entry per attempted issue with the six required fields. Append site is placed above the Done/halt branching so Task 2 needs no orchestrator restructuring.
**Done when:** a unit test (`tests/test_runlog.py`, temp `HOME`, two `append_entry` calls) asserts the JSON file shape and append order; an integration test (`tests/test_orchestrator_runlog.py`, stubbed Linear + real worktree + fake claude shell script + monkeypatched `HOME`, ≥2 successful issues) asserts the file exists at the resolved path with `cycle_id` matching the stubbed cycle, `time_spent` null, `entries` in pick order with every entry carrying all six required fields, every `final_linear_state == "Done"`, ISO-parseable `started_at`/`finished_at` with `started_at <= finished_at`, and `exit_code == 0`.
**Dependencies:** none.

### Task 2 — Halt path writes a run-log entry before exit-non-zero
**Linear:** [ABA-216](https://linear.app/ababushkin/issue/ABA-216) (blockedBy: ABA-215)
**Description:** Pin the contract that the orchestrator's halt branch (ABA-202) also writes a log entry before `return 1`. If Task 1's append site is above the branching this is purely test coverage; if not, restructure the orchestrator so the append is unconditional for every attempted issue.
**Done when:** an integration test in `test_orchestrator_halt.py` style (stubbed Linear, fake claude exiting 0 without flipping state, two issues queued, monkeypatched `HOME`) asserts the log file contains exactly one entry — for the halted (first) issue, with all six required fields and `final_linear_state` matching the non-Done state — and the second issue is absent from `entries` and absent from disk.
**Dependencies:** Task 1 (ABA-215).

### Task 3 — Acceptance validation against ABA-196 (success smoke + forced-failure smoke)
**Linear:** [ABA-218](https://linear.app/ababushkin/issue/ABA-218) (blockedBy: ABA-215, ABA-216)
**Description:** Two real, unstubbed runs against the active Linear cycle, mirroring ABA-203's ABA-194 acceptance pattern. (a) Success smoke against 2+ trivially-completable issues; (b) forced-failure smoke against one deliberately-uncompletable issue. After both, post a per-bullet pass/waive comment on ABA-196, and update the ABA-194 acceptance comment to flip bullet 5 from `WAIVED-PENDING-US-C` to PASS.
**Done when:** a comment on ABA-196 records the success-smoke and forced-failure-smoke cycle IDs, run timestamps, log paths, and a per-bullet pass/waive table covering: (1) JSON well-formed; (2) entries in pick order with one per attempted issue; (3) each entry has all six required fields; (4) `cycle_id` matches Linear; (5) `time_spent` is null; (6) `final_linear_state` sequence matches Linear history per issue. On all-PASS: ABA-196 → Done and the ABA-194 acceptance table is re-posted with bullet 5 flipped to PASS.
**Dependencies:** Tasks 1 (ABA-215), 2 (ABA-216).

## Open questions

None blocking. Re-invocation behaviour (overwrite vs append on a same-cycle re-run) is the only ambiguity; default is "overwrite per run". If US-D / ABA-197's grading later needs run history, a separate ticket can introduce a `.history` sub-array.
