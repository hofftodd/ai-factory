# Spec 000 — Local Agent Coding Orchestrator

Status: **Approved for build** · M1–M3 implemented (see plan.md §8–10)

## 1. Summary

A self-hosted, web-based dashboard that drives a software project through a
spec-driven SDLC using AI agents. A user adds a project tied to a GitHub repo,
works interactively with a planner model to produce a specification,
implementation plan and executable validation tests, then hands off to an
orchestrator that decomposes the plan into small, context-bounded subtasks and
executes them autonomously with local (or cloud) models until the validation
suite passes. The orchestrator watches every subtask for runaway behaviour,
re-reviews the whole plan after each completed subtask, and the user can
monitor, gate, or stop everything from the dashboard.

## 2. Goals

- G1. Take a project from idea to "all validation tests pass" with minimal
  human intervention, local-model first.
- G2. Keep every executor subtask small enough to fit comfortably in a local
  model's context window.
- G3. Never let an agent loop, drift or burn budget unnoticed.
- G4. Give the user full visibility and an always-working stop control.
- G5. Keep all durable project artifacts (specs, plans, tasks) in the project's
  own git repo, readable without this app.

## 3. Non-goals (v1)

- Multi-user / multi-tenant access control (single trusted user on a LAN).
- Building our own coding agent (we wrap existing ones).
- Hosting/serving models (we consume existing inference endpoints).
- Non-GitHub forges.
- GitHub Issues / Projects integration (see §9).

## 4. Environment & assumptions

- Inference nodes (initial):
  - **Mac Studio M5 Ultra, 256 GB** — large models (fully resident up to
    ~200 GB); prompt prefill on par with or faster than the R9700 (measured
    by ai-bench, Nov 2026).
  - **Mini PC with Radeon AI Pro R9700, 32 GB** — fast ~30B-class models.
  - Both run [ai-swap](https://github.com/hofftodd/ai-swap) as peers, so
    every model on either machine is reachable through either machine's
    endpoint, loaded on demand.
- Orchestrator, dashboard, supervisor and agent sandboxes run together on one
  host, which reaches model servers over the LAN via OpenAI-compatible
  endpoints. The host can be either model machine — Linux (the mini PC) or
  macOS (the Mac Studio) — or, preferably, a separate machine, so agent
  containers don't compete with models for memory and CPU and Stop works even
  if a model host hangs. Such a host needs Linux with Docker, roughly 8+ cores,
  32+ GB of RAM and a fast SSD for clones and toolchain images.
- The agent containers must run on the orchestrator's host: they bind-mount
  each task's clone, briefing and results folder from ai-factory's data
  directory, and reach the model gateway through the Docker host address.
- Cloud providers (Anthropic, OpenAI, …) are optional per role.
- A Docker engine is available to the orchestrator host: dockerd on Linux; on
  macOS a Linux VM, by default a Lima VM that ai-factory manages itself
  (`ai-factory vm`), or any Docker-compatible engine (Docker Desktop, OrbStack,
  colima) via `DOCKER_HOST`.

## 5. Concepts

| Term | Meaning |
|---|---|
| Project | A GitHub repo plus its specs, plan, tasks and run history. |
| Phase 1 (Specify) | Interactive work on spec, plan and validation tests. |
| Phase 2 (Execute) | Autonomous decomposition, execution and replanning. |
| Validation test | An executable test (or command) that defines part of "done". |
| Task | A small unit of work with its own requirement slice, todos, allowed files and validation command. |
| Context pack | The exact material handed to an executor for one task. |
| Role | project planner, task planner, tester, builder, escalation — each bound to a model endpoint (defaults in [spike-notes.md](spike-notes.md)). |
| Node | An inference endpoint with a concurrency capacity. |
| Gate | A configurable checkpoint where the project waits for user approval. |
| Watchdog | Rules that detect looping, drift, scope violations and budget overruns. |

## 6. Functional requirements

### 6.1 Projects

- FR-1.1 User can add a project by GitHub URL (existing repo) or by creating a
  new repo; the app clones it to a local workspace.
- FR-1.2 User can configure per project: role→model bindings, autonomy gates,
  budgets, executor type, test command(s).
- FR-1.3 Projects list shows phase, % validation passing, active tasks,
  pending gates, and cloud spend.

### 6.2 Phase 1 — Specify (interactive)

- FR-2.1 Chat-style session with the planner model to draft and refine
  `spec.md` (requirements), `plan.md` (architecture, milestones) and
  `validation.md` (acceptance criteria).
- FR-2.2 Planner proposes executable validation tests; they are committed to
  the repo and initially expected to fail.
- FR-2.3 Documents are editable directly in the UI; every change is a git
  commit under `specs/<feature>/`.
- FR-2.4 Phase 1 ends only when the user approves the spec + validation set
  (mandatory gate).

### 6.3 Decomposition

- FR-3.1 Planner decomposes `plan.md` into a dependency-ordered task graph
  written to `specs/<feature>/tasks.md` (human-readable) and the DB.
- FR-3.2 Each task contains: id, title, requirement slice (refs into spec),
  todos, allowed file globs, interface context, validation command, budgets,
  dependencies.
- FR-3.3 Size limits enforced at decomposition time (defaults, configurable):
  ≤ 3 files touched; context pack ≤ 35% of executor's usable context; ≥ 1
  task-local validation command. Tasks exceeding limits are split before
  being queued.

### 6.4 Execution

- FR-4.1 Scheduler runs ready tasks (deps satisfied) onto nodes respecting
  per-node concurrency slots.
- FR-4.2 Each task runs in its own isolated git clone + branch inside a Docker
  container with no host credentials. Executor containers are built from a
  minimal Ubuntu Server base image (`ubuntu:24.04`) — see FR-4.6.
- FR-4.3 The executor (default: pi, alternative: OpenCode, behind a common
  interface) receives the context pack and runs non-interactively. The
  executor's own prompt overhead (system prompt, tool schemas) is subtracted
  from the window before the context budget is applied. An executor whose
  exit status doesn't reflect failure reports it from its output.
- FR-4.4 After the executor finishes, the orchestrator runs the task's
  validation command and the full previously-passing test set; a task is done
  only if its validation passes and no previously-passing test regresses.
- FR-4.5 Completed task branches merge into the milestone branch; milestones
  become GitHub PRs.
- FR-4.6 Executor images are layered:
  - `ai-factory/executor-base`: `ubuntu:24.04` + git, ca-certificates, curl,
    the executor CLIs (pi, with a private Node.js runtime under `/opt/pi` that
    is not on PATH; OpenCode), socat for the gateway relay, a non-root `agent`
    user. Nothing else. Versions are pinned.
  - Per-project toolchain layer: optional `.ai-factory/Dockerfile` in the project
    repo (`FROM ai-factory/executor-base`) adds language runtimes and deps;
    generated by the planner in Phase 1 and approved with the spec.
  - Containers run as non-root, with only the task clone mounted
    read-write, CPU/memory limits, and network egress restricted to the
    model gateway: an internal Docker network whose only other member is a
    relay to the gateway. A project can opt into an "open" network when its
    tests need one. Toolchain image builds use the normal build network, so
    package registries are reachable while building, not while the agent
    runs.

### 6.5 Watchdog & escalation

- FR-5.1 Per-task budgets: max iterations, tokens, wall-clock time.
- FR-5.2 Loop detection: same failure signature (normalised error hash) or
  same diff hash seen N times (default 3).
- FR-5.3 Scope fence: diff touches files outside allowed globs, or diff size
  exceeds limit.
- FR-5.4 Regression: count of passing tests decreases.
- FR-5.5 On trip, escalate in order: retry with fresh context → split task →
  reassign to escalation model → park as `blocked` for the user. Every step is
  logged with reason.

### 6.6 Replanning

- FR-6.1 After each completed task, the reviewer reads the plan, task graph,
  completed diff and test status, and proposes add/remove/modify task ops,
  each with a reason.
- FR-6.2 Task-list changes apply automatically (unless gated); changes to
  `spec.md` or `validation.md` always require user approval.
- FR-6.3 Net task growth per milestone is capped (default +50% of original
  count); exceeding it triggers a gate.
- FR-6.4 Plan revisions are versioned and shown as diffs in the UI.

### 6.7 Autonomy gates

- FR-7.1 Configurable gates: task-breakdown approval, per-task merge,
  milestone PR merge, spec-changing replan, escalation to cloud, escalation to
  user. Always-on: Phase 1 approval, budget/spend cap reached.
- FR-7.2 A gate puts the project in `waiting` and adds an item to the global
  "Needs you" inbox; optional push notification (ntfy-compatible webhook).

### 6.8 Stop controls

- FR-8.1 **Pause** (project or task): finish current step, start nothing new.
- FR-8.2 **Stop** (project or task): kill executor process tree / container
  (SIGTERM, then SIGKILL after grace period ≤ 10 s) and abort in-flight model
  requests.
- FR-8.3 **Global E-stop**: Stop every project and halt the scheduler.
- FR-8.4 Stop is enforced by a supervisor process independent of the
  orchestrator loop; it must work while the orchestrator is hung.
- FR-8.5 Stopped work is preserved (worktree, transcript, diff) with state
  `stopped_by_user`; user can resume, edit & retry, split, or discard.
- FR-8.6 The orchestrator checks stop flags before every side-effecting
  action (model call, process spawn, merge, GitHub write).

### 6.9 Models & nodes

- FR-9.1 Register nodes as OpenAI-compatible endpoints with a concurrency
  capacity and a list of available models.
- FR-9.2 Cloud providers configured by API key; calls routed through LiteLLM.
- FR-9.3 Per-project cloud spend cap; reaching it triggers the budget gate.
- FR-9.4 Node health (reachable, loaded model, tokens/s, queue depth) shown in
  the UI.

### 6.10 Monitoring

- FR-10.1 Live updates via Server-Sent Events.
- FR-10.2 Task view: live agent transcript/tool calls, current diff, test
  output, watchdog meters, stop button.
- FR-10.3 Project view: task board (queued/running/review/done/blocked), plan
  revision history, validation-passing trend.

## 7. Non-functional requirements

- NFR-1 Single-user, LAN-only by default; bind address configurable; optional
  static token auth. Remote access via VPN (e.g. Tailscale) is the user's
  concern.
- NFR-2 Crash-safe: orchestrator restart resumes from DB state; no task left
  silently "running".
- NFR-3 Stop latency: executor processes terminated ≤ 10 s after Stop.
- NFR-4 All agent actions auditable: prompts, responses, tool calls, diffs,
  watchdog events persisted.
- NFR-5 Secrets (GitHub token, cloud keys) never enter executor sandboxes.

## 8. Open questions

- ~~OQ-1 Exact default models per role~~ — resolved from ai-bench measurements (Nov 2026) in [spike-notes.md](spike-notes.md): planners and escalation on DeepSeek V4 Flash (Mac), builder on flint (Mac, oMLX), tester on ThinkingCap-Qwen3.8-27B (R9700).

## 9. Future enhancements (out of scope for v1)

- **GitHub Issues integration.** Mirror milestones as issues and tasks as
  sub-issues with state labels and summary comments; optionally accept a
  limited set of owner-only inbound controls (stop label, guidance comments,
  intake issues routed to the replanner behind a gate). Would need write
  batching for GitHub rate limits and prompt-injection safeguards.
- **Pi Durable** (Earendil's experimental durable conversation/task runtime,
  TypeScript). Not a replacement for the orchestrator, which is a task DAG
  with git merges, container lifecycle, gates and a separate kill-switch
  process, but a candidate for two narrower jobs once its API is stable:
  - *Planner conversations that survive restarts.* Today `recover()` fails a
    planner job cut off by an orchestrator restart ("send your message
    again"). A durable conversation would resume it, and forking would let
    the user try alternative designs from any point.
  - *Resumable executor attempts.* Today an interrupted attempt is failed and
    retried fresh. A checkpointed agent could resume instead, but Stop must
    still end an attempt for good and be enforced by the supervisor, so
    resumption would be limited to orchestrator crashes.

  Constraints: one process owns a Pi Durable store, and it needs a JavaScript
  runtime, so it would run as a Node sidecar owned by the orchestrator. The
  seams are `ai_factory.llm` (planner calls) and the `Executor` interface.
