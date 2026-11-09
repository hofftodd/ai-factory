# Validation 000 — Local Agent Coding Orchestrator

Status: **Draft** · Validates: [spec.md](spec.md)

"Done" for v1 = every check below passes in CI (unit/integration) or in the
scripted end-to-end run (E2E). Model-dependent behaviour is tested against a
**fake LLM endpoint** (scripted OpenAI-compatible server) and a **fake
executor** so tests are deterministic; one optional smoke test uses real nodes.

## V1 — Projects & config
- V1.1 Adding a project by repo URL clones it and lists it with phase `specify`. (FR-1.1)
- V1.2 Role bindings and gates persist and round-trip via API. (FR-1.2)

## V2 — Phase 1
- V2.1 Planner dialogue produces `spec.md`, `plan.md`, `validation.md`
  committed under `specs/<feature>/`. (FR-2.1, FR-2.3)
- V2.2 Phase cannot move to `execute` without an approval record. (FR-2.4)

## V3 — Decomposition
- V3.1 Every generated task has all required fields. (FR-3.2)
- V3.2 A task whose context pack exceeds the size limit is split, never
  queued. (FR-3.3)
- V3.3 Task graph is acyclic; scheduler only starts tasks with deps `done`. (FR-3.1, FR-4.1)

## V4 — Execution
- V4.1 Each running task has its own worktree/branch and container; the
  container environment contains no GitHub token or cloud keys. (FR-4.2, NFR-5)
- V4.2 A task whose own validation passes but breaks a previously-passing test
  is not marked `done`. (FR-4.4)
- V4.3 Node capacity is never exceeded under a queue of 10 ready tasks. (FR-4.1)
- V4.4 Executor containers derive from `ai-factory/executor-base` (`ubuntu:24.04`),
  run as non-root, mount only the task worktree read-write, and cannot reach
  hosts outside the allowlist. (FR-4.6)

## V5 — Watchdog
- V5.1 Fake executor emitting the same error 3× trips loop detection. (FR-5.2)
- V5.2 Diff touching a file outside allowed globs trips the scope fence. (FR-5.3)
- V5.3 Exceeding iteration/token/time budget trips the watchdog. (FR-5.1)
- V5.4 Escalation follows retry → split → escalation model → blocked, with
  an Event logged per step. (FR-5.5)

## V6 — Replanning
- V6.1 After a task completes, a PlanRevision with reasons is recorded. (FR-6.1)
- V6.2 A revision that edits `spec.md`/`validation.md` creates a gate and is
  not applied until approved. (FR-6.2)
- V6.3 Exceeding the task-growth cap creates a gate. (FR-6.3)

## V7 — Gates
- V7.1 With all optional gates off, a 3-task fake project runs to completion
  with zero user input after Phase 1 approval. (FR-7.1, G1)
- V7.2 With the milestone gate on, the project stops in `waiting` with an
  inbox item. (FR-7.2)

## V8 — Stop controls (highest priority)
- V8.1 Stop on a running task terminates its container/process group within
  10 s; task state = `stopped_by_user`; worktree and transcript preserved. (FR-8.2, FR-8.5, NFR-3)
- V8.2 Stop works with the orchestrator process frozen (SIGSTOP). (FR-8.4)
- V8.3 Global E-stop halts all running tasks across ≥ 2 projects and the
  scheduler starts nothing new until cleared. (FR-8.3)
- V8.4 Pause lets the current step finish and starts no new task. (FR-8.1)
- V8.5 An in-flight streaming model request is aborted on Stop. (FR-8.2)

## V9 — Resilience & audit
- V9.1 Killing the orchestrator mid-run and restarting leaves no task stuck
  in `running`; it is resumed or marked failed with a reason. (NFR-2)
- V9.2 Every TaskRun has persisted prompts, responses, tool calls and diff. (NFR-4)

## V10 — Spend
- V10.1 Reaching the cloud spend cap creates a budget gate and stops cloud
  calls. (FR-9.3)

## E2E (scripted, fake LLM)
- E2E-1 Add project → Phase 1 with scripted planner → approve → decompose into
  3 tasks → execute → all validation tests pass → milestone PR created
  against a local bare repo / mocked GitHub API.
- E2E-2 Same, with one task scripted to loop; watchdog escalates; user stops
  the project from the UI (Playwright clicks the stop button).

## Optional smoke (real nodes)
- S-1 Real executor on R9700 completes a trivial task ("add function + test")
  in a toy repo.
