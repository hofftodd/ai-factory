# Plan 000 — Local Agent Coding Orchestrator

Status: **In progress** — M1, M2, M3 done · Implements: [spec.md](spec.md)

## 1. Stack

| Concern | Choice |
|---|---|
| Language / packaging | Python 3.12+, `uv` |
| API | FastAPI + Uvicorn |
| Live updates | Server-Sent Events (`sse-starlette`) |
| Persistence | SQLite (WAL) via SQLModel / SQLAlchemy; Alembic migrations |
| Model routing | LiteLLM (Python library) |
| Executors | OpenCode (default), Aider — in Docker containers from `ubuntu:24.04`-based image |
| Git / GitHub | `git` CLI (worktrees), GitHub REST via `httpx` |
| Frontend | React + Vite + TypeScript, served as static files by FastAPI |
| Tests | pytest, pytest-asyncio; Playwright for UI e2e |
| Lint / types | ruff, mypy (strict on `core/`) |

## 2. Process architecture

```
┌──────────── mini PC ─────────────────────────────────────────────┐
│                                                                  │
│  Browser ──HTTP/SSE──► api (FastAPI)                             │
│                          │  writes commands / stop flags         │
│                          ▼                                       │
│                       SQLite  ◄──────────────┐                   │
│                          ▲                   │                   │
│                          │                   │                   │
│                    orchestrator          supervisor              │
│              (scheduler, planner,     (polls stop flags,         │
│               watchdog, replanner)     kills process groups /    │
│                          │             containers; heartbeats)   │
│                          ▼                   │                   │
│                 executor sandboxes ◄─────────┘                   │
│              (Docker, one per running task, own worktree)        │
└──────────────────────────┬───────────────────────────────────────┘
                           │ OpenAI-compatible HTTP (LAN) / cloud
              ┌────────────┴────────────┐
        Mac Studio (MLX/llama.cpp)   R9700 (llama.cpp)      Cloud APIs
```

- `api`, `orchestrator`, `supervisor` are separate OS processes (one repo,
  three entry points) so a hung orchestrator cannot block Stop.
- The DB is the only channel between them: commands table, stop flags,
  heartbeats. Supervisor is deliberately tiny and has no model calls.

## 3. Package layout

```
src/foreman/            # working package name
  api/                  # FastAPI routers, SSE stream, static UI mount
  core/
    models.py           # SQLModel tables
    state.py            # task/project state machines
    contextpack.py      # build + size context packs (token counting)
    watchdog.py         # pure functions: (task run events) -> verdicts
  orchestrator/
    scheduler.py        # ready-queue, node slots
    planner.py          # phase-1 dialogue, decomposition
    replanner.py        # post-task review, plan ops
    runner.py           # launch executor, collect results, run validation
  executors/
    base.py             # Executor protocol
    opencode.py
    aider.py
  llm/router.py         # LiteLLM wrapper, role→endpoint, spend tracking
  gitops/               # clone, worktree, merge, PR
  supervisor/main.py
docker/executor-base/   # Dockerfile for foreman/executor-base (ubuntu:24.04)
web/                    # React app
tests/
specs/
```

## 4. Key data model (abridged)

- `Project(id, repo_url, workspace_path, phase, autonomy_gates, budgets, role_bindings)`
- `Node(id, base_url, capacity, models, healthy, last_seen)`
- `Task(id, project_id, title, req_refs, todos, allowed_globs, validation_cmd, deps, state, budgets, attempt)`
- `TaskRun(id, task_id, node_id, model, started, ended, outcome, tokens, cost, diff_hash, error_sig)`
- `Event(id, project_id, task_id, kind, payload, ts)` — transcript, tool calls, watchdog, gates
- `PlanRevision(id, project_id, version, ops, reason, approved_by)`
- `Gate(id, project_id, kind, payload, state)`
- `Control(id, scope, target_id, action[pause|stop|estop], created, acknowledged)`

Task states: `queued → running → validating → done`, plus `blocked`,
`waiting_gate`, `stopped_by_user`, `failed`, `discarded`.

## 5. Context pack

Built per task attempt, persisted for audit:
1. System prompt for the executor role (rules, scope fence, how to run tests).
2. Requirement slice (spec excerpts referenced by `req_refs`).
3. Todos.
4. Interface context: signatures/types of symbols the task depends on
   (extracted with tree-sitter/ast, not whole files).
5. Contents of allowed files (current).
6. Validation command and, on retry, a summary of the previous failure
   (never the full prior transcript).

Size is token-counted against the executor model's configured usable context;
over-budget ⇒ the task is returned to the planner to split.

## 6. Milestones

### M0 — Spike (no app code) — skipped by decision; model choice deferred to real use
Manually run Spec Kit-style spec → decomposition → OpenCode/Aider on the R9700
and Mac against a toy repo. Record: which models follow tool protocols, real
usable context, tokens/s, typical failure modes. Output: `specs/000-orchestrator/spike-notes.md`
and default role bindings.

### M1 — Walking skeleton + stop ✅
- Project add/clone, node registry, role bindings.
- Manual task creation; sequential execution on one node via OpenCode in Docker.
- Validation run, merge to milestone branch.
- Supervisor + Pause/Stop/E-stop end-to-end.
- Live task view (SSE transcript, diff, test output).

### M2 — Specify & decompose ✅
- Phase 1 chat with planner, doc editing, commits to `specs/`.
- Validation tests generation; Phase 1 approval gate.
- Decomposition with size enforcement and context packs.

### M3 — Autonomy ✅
- Watchdog rules + escalation ladder.
- Replanner with plan revisions, growth cap, spec-change gate.
- Configurable gates + "Needs you" inbox + ntfy notifications.
- Milestone PRs on GitHub.

### M4 — Hosts, pi, egress (part 1 ✅, see §11); multi-node & polish
- macOS hosting next to Linux (Lima VM), pi executor (default), executor
  network locked to the gateway. ✅
- Multi-node scheduling with capacities, node health view.
- Cloud spend tracking and caps.
- Portfolio dashboard, validation trend charts.
- Crash-recovery hardening.

## 7. Risks

| Risk | Mitigation |
|---|---|
| Local executors unreliable at tool use | M0 spike picks models; escalation ladder; tiny tasks |
| Mac prefill latency | Small context packs; Mac reserved for planner/escalation |
| Replanner churn | Growth cap, reasons required, spec-change gate |
| Validation tests themselves wrong | Phase 1 human approval; tests changes always gated |
| Executor CLI interface drift | `Executor` protocol + pinned versions + contract tests |

## 8. M1 implementation notes

Design changes made while building M1 (spec updated where relevant):

- **Task clones instead of git worktrees.** A worktree's `.git` file points at
  an absolute host path, which would force mounting the main repository (and
  its config) into the container. Each task gets a standalone local clone with
  no remote; the orchestrator pushes the task branch back into the main clone
  to merge.
- **Two databases.** Stop requests and the E-stop flag live in `control.db`,
  which the orchestrator only reads. Found by the frozen-orchestrator test
  (V8.2): an orchestrator stopped mid-transaction holds `foreman.db`'s write
  lock, which previously blocked recording a Stop at all.
- **Kill first, record second.** The supervisor kills processes before writing
  any state, and the runner consults `control.db` before advancing or failing
  a task, so a stop is never reported as a failure.
- **Heartbeats are files** (mtime), so liveness never contends for a lock.
- **Containers are labelled with an instance id** so the orphan sweep never
  touches another foreman installation's containers on the same host.
- **One active task per project** in M1 (tasks merge into one milestone
  branch); parallelism comes from multiple projects and node capacity.
- **Scope fence implemented early** (it was planned for M3): changes outside a
  task's allowed globs fail the task.

### Validation coverage after M1

| Check | Status |
|---|---|
| V1.1, V1.2 projects & config | ✅ `tests/test_api.py` |
| V3.3 deps respected | ✅ `test_dependencies_run_in_order` |
| V4.1 own clone/branch/container, no secrets | ✅ `tests/test_docker.py` |
| V4.2 regression blocks done | ✅ (exit-code based; per-test regression counting is M3) |
| V4.3 node capacity | ✅ `test_node_capacity_respected` |
| V4.4 image, non-root, mounts | ✅ — **egress allowlist not yet implemented** (containers use the default bridge network) |
| V5.2 scope fence | ✅ `test_scope_fence` |
| V5.3 time budget | ✅ `test_timeout` (iteration/token budgets: M3) |
| V8.1–V8.5 stop controls | ✅ `tests/test_stop.py` (real processes), `tests/test_docker.py`, Playwright `web/e2e` |
| V9.1 crash recovery | ✅ `test_orchestrator_crash_recovery` |
| E2E-2 (stop from UI) | ✅ `scripts/e2e.sh` |
| V2.x, V3.1–V3.2, V5.1, V5.4, V6–V7, V10, E2E-1 | M2–M4 |

### Known gaps carried into M2+

- Network egress restriction for executor containers (FR-4.6).
- The node's API key (if any) is written into the OpenCode config inside the
  run's read-only control mount; cloud keys must go through a host-side proxy
  before cloud executors are allowed (NFR-5).
- Per-project toolchain images: before a task attempt or acceptance check, the
  orchestrator builds `.foreman/Dockerfile` (context: `.foreman/` only) into
  `foreman/project-<instance>-<id>:<hash of Dockerfile + base image id>` and runs
  that step's containers from it; unchanged Dockerfiles reuse the image. A failed
  build is a `config` failure (blocks at once). Builds are killable by Stop like
  containers (`local:<pgid>` handle). Old images are not pruned yet.
- No GitHub PR creation yet (milestone branch is merged locally and optionally
  pushed).

## 9. M2 implementation notes

- **Planner protocol.** The planner writes documents with full-file blocks
  (`<<<FILE path ... >>>`) and sets the acceptance command with
  `<<<ACCEPTANCE_CMD ... >>>`. Plain text blocks, rather than tool calls, so
  small local models can follow it. Writes are limited to
  `specs/<feature>/{spec,plan,validation}.md`, `tests/acceptance/**` and
  `.foreman/Dockerfile`; anything else is refused and reported in the chat.
  Current documents go in the system prompt on every turn, so the
  conversation history only carries the talk (and is trimmed oldest-first to
  fit the planner's window).
- **Planner jobs** run inside the orchestrator, stream into the dashboard,
  and are stoppable mid-stream: project Stop / E-stop closes the model
  connection and nothing is applied (CAS on the job state). A failure while
  applying results is reported, not swallowed.
- **Providers.** `openai` = any OpenAI-compatible endpoint (raw streaming
  HTTP). `anthropic` = Claude through the official SDK, adaptive thinking,
  server-side refusal fallback (`fallbacks: "default"`) on models that support
  it. Claude nodes can only be planners (NFR-5).
- **Context packs** (`core/contextpack.py`): requirement slice by id, Python
  interfaces via `ast` (signatures + first docstring line, no bodies), other
  languages via signature-line matching, editable files' current contents
  (clipped at 400 lines), validation and test commands. Tokens are estimated
  at ~4 chars/token against `context_budget_pct` (default 35%) of the executor
  node's `context_window`. The runner refuses to start a task whose pack is
  over budget.
- **Size rules** (FR-3.3): explicit files only (no wildcards), at most
  `max_task_files` (default 3), a validation command, pack within budget,
  requirement ids that exist in the spec, and no edits to the spec documents.
  Violations go back to the planner (up to 3 attempts); what remains is shown
  in plan review and blocks approval until fixed by hand.
- **Phases:** `specify -> decomposing -> plan_review -> execute`. Tasks only
  run in `execute`. `skip_spec` creates a project straight in `execute` for
  hand-written tasks (this is how the M1 tests and screenshots run).
- **Schema migration:** new columns are added to existing SQLite databases on
  startup (`db._add_missing_columns`), so v1 data directories keep working.

### Validation coverage after M2

| Check | Status |
|---|---|
| V2.1 chat produces and commits the documents | ✅ `tests/test_spec_flow.py` |
| V2.2 no `execute` without spec approval | ✅ approval endpoint + phase gate; tasks don't run before `execute` (projects created with `skip_spec` opt out by design) |
| V3.1 generated tasks have all fields | ✅ `tests/test_planner.py` |
| V3.2 over-budget tasks are split, never queued | ✅ retry loop + approval block (`test_oversized_task_is_sent_back_to_be_split`, `test_persistent_violations_block_plan_approval`) |
| V3.3 acyclic graph | ✅ cycle detection before tasks are created |
| V8.5 in-flight planner stream aborted on Stop | ✅ `test_stop_aborts_planner_mid_stream_and_applies_nothing` |
| E2E-1 spec → done with a real executor | ✅ `test_spec_to_done_with_agent[pi|opencode]` (Docker, pi and OpenCode, fake model) and Playwright `web/e2e/spec.spec.ts` |

### Known gaps carried into M3+

- Network egress restriction for executor containers (FR-4.6) — still open.
- Planner jobs don't count against node capacity.
- Token counts are estimates (chars/4), not tokenizer-exact.
- Watchdog, replanning after each task, and the remaining checkpoints are M3.

## 10. M3 implementation notes

- **Model gateway** (`gateway.py`, served by the orchestrator on
  `FOREMAN_GATEWAY_PORT`). Each run gets a secret URL `/g/<token>/v1` as the
  executor's base URL; the gateway forwards to the node, adds the node's API
  key host-side (NFR-5 for node keys), counts requests and tokens (server
  usage when reported, else estimates), detects identical responses, and
  refuses calls for stopped, finished or tripped runs. Because the gateway now
  connects to nodes, node URLs must be reachable from the orchestrator host
  (no more `host.docker.internal` for node URLs).
- **Watchdog** (FR-5.1–5.4): per attempt `max_requests`, `max_tokens`,
  `max_diff_lines`, `repeat_limit`, plus the task timeout. A trip is recorded
  on the run; the runner polls for it and kills the step. Scope violations and
  oversized diffs are watchdog failures too. Regression is still exit-code
  based (the project test command must pass), not per-test counting.
- **Failure signatures** normalise numbers, hashes, temp paths and whitespace,
  so "the same failure" survives timing and random-id noise.
- **Escalation** (FR-5.5, `orchestrator/escalation.py`): retry (fresh session,
  told the previous failure) up to `max_retries` → split via the planner
  (once per task, max depth 2; dependents are rewired to all children) →
  escalation-model binding → blocked + inbox item. Configuration errors block
  immediately; an over-budget context pack goes straight to splitting; the
  same signature `repeat_limit` times skips remaining retries.
  `watchdog.escalation = false` restores plain failures.
- **Replanning** (FR-6, `orchestrator/replan.py`): a REVIEW planner job after
  every completed task. The review is queued *before* the task is marked done
  and the scheduler holds the project's next task while it is pending, so
  replanning really happens between tasks. Ops are validated (only
  queued/proposed/blocked tasks, reasons required, size-checked new tasks);
  growth over `growth_cap_pct` of `baseline_tasks` and the optional
  `replan_approval` checkpoint gate a revision; spec changes always gate and,
  when approved, are appended to spec.md.
- **Checkpoints** (FR-7, `core/gates.py`): one `Gate` table behind the
  "Needs you" inbox. The API records decisions; the orchestrator carries them
  out (single writer to the repositories). ntfy notifications on new gates.
- **Milestone**: acceptance runs once per milestone commit in the sandbox on a
  fresh clone; failures go to the replanner up to `max_acceptance_failures`,
  then to the user; success marks the project done and (after the milestone
  checkpoint) pushes and opens a GitHub PR via the REST API. Adding or
  retrying a task reopens a finished project. Stop / E-stop kill a running
  acceptance check.

### Validation coverage after M3

| Check | Status |
|---|---|
| V5.1 same error 3× trips loop detection | ✅ failure signatures + gateway repeat detection (`tests/test_autonomy.py`, `tests/test_gateway.py`) |
| V5.3 iteration / token / time budgets | ✅ `test_request_budget_trips_watchdog`, `test_token_budget_trips_watchdog`, `test_watchdog_trip_kills_the_running_agent`, `test_timeout` |
| V5.4 escalation order with an event per step | ✅ `test_retries_then_blocks_without_planner`, `test_split_after_retries`, `test_unusable_split_blocks`, `test_same_failure_repeating_skips_retries` |
| V6.1 revision with reasons after each task | ✅ `test_replan_after_task_applies_ops` |
| V6.2 spec edits gated | ✅ `test_spec_change_is_always_gated` |
| V6.3 growth cap gated | ✅ `test_replan_growth_cap_needs_approval` |
| V7.1 runs to completion without input (optional gates off) | ✅ `test_runs_to_done_without_any_input_when_optional_gates_are_off` (spec approval → 3 tasks → 3 reviews → acceptance → done, zero checkpoints) |
| V7.2 gate → waiting + inbox item | ✅ `test_task_merge_checkpoint`, Playwright `web/e2e/inbox.spec.ts` |
| Notifications | ✅ `test_new_gate_sends_push_notification` |

### Known gaps carried into M4

- Container network egress restriction (FR-4.6): executors now only *need*
  the gateway, so the next step is a Docker network that allows nothing else.
- Planner jobs don't count against node capacity; the gateway enforces
  budgets but not node-level concurrency.
- Token counts fall back to estimates when a server doesn't report usage.
- Regression detection is exit-code based.

## 11. M4 part 1 implementation notes: macOS hosts, pi, egress

**Hosts.** Foreman's processes run natively on Linux and macOS; containers run
on whatever Docker engine `DOCKER_HOST` names. `foreman.engine` resolves the
socket (`FOREMAN_DOCKER_HOST`, `DOCKER_HOST`, the managed Lima VM's socket,
the CLI default) and how containers reach the host: executors always use the
name `host.docker.internal`, mapped with `--add-host` to `host-gateway` (Linux,
Docker Desktop, OrbStack) or `192.168.5.2` (Lima, colima: on Lima
`host-gateway` is the VM). The gateway binds only where containers arrive:
the docker bridge address on Linux, loopback on macOS (`FOREMAN_GATEWAY_HOST`
overrides; it used to be `0.0.0.0`). Every docker call, including the
supervisor's kills, uses the resolved socket.

**Lima VM** (`foreman vm start|stop|status|shell|delete`, `foreman.vm`): Lima's
`docker-rootful` template, `vz` + virtiofs, mounting only `FOREMAN_DATA_DIR`,
writable, at the same path (bind mounts resolve inside the VM). Rootful so
containers write the task clone as the user's own uid. `foreman doctor` runs
the real paths: engine, image, a container writing to the data dir, a
container reaching the gateway address, and the isolated network.

**Process groups on Darwin.** `group_alive` now asks `ps` where there is no
`/proc`, and treats `EPERM` from `killpg` as dead: Darwin returns it for a
group that holds only zombies, which made Stop and timeouts crash on macOS.

**Egress (FR-4.6).** Each instance has an internal network
`foreman-exec-<instance>` and a relay container `foreman-gw-<instance>` (socat,
read-only, no capabilities) on that network and the default bridge,
forwarding one port to the gateway. Executors, validation and acceptance run
on the internal network and reach the gateway as `http://foreman-gw:8701`;
nothing else resolves or routes. The relay carries no `foreman.run` label, so
Stop and the orphan sweep leave it alone; it is recreated when the gateway
port changes and removed when the orchestrator exits. `Project.network`
("isolated" default, "open") is the per-project escape hatch. Existing
projects become isolated on upgrade.

**pi executor.** `foreman.executors.pi`: a generated `models.json` registers
the gateway as provider `foreman` (`openai-completions`, dummy key, the
node's context window); pi runs with `--mode json --no-session --offline`
and every discovery feature off, including `AGENTS.md` (the context pack is
the agent's whole context). pi exits 0 when model calls fail, so executors
can now return an `OutputParser` that renders their raw output for the
dashboard (raw JSONL stays in `execute.log`) and reports a failure after a 0
exit. Executors declare `prompt_overhead_tokens`, measured from the first
request each agent sends: pi ~1.4k (4 tools), OpenCode ~7.4k (9 tools); the
context budget is now `pct × (window − overhead)`.

### Validation coverage after M4 part 1

| Check | Status |
|---|---|
| Full suite on macOS (Darwin process groups) | ✅ 8 previously failing stop/timeout tests pass; `tests/test_stop.py` uses `sandbox.alive_in_group` |
| Engine detection and overrides | ✅ `tests/test_engine.py` |
| Isolated network reaches only the gateway | ✅ `test_isolated_network_reaches_only_the_gateway` (host, IP and DNS targets unreachable; "open" unchanged) |
| pi end to end | ✅ `test_executor_against_fake_model[pi]`, `test_spec_to_done_with_agent[pi]` (through the relay) |
| pi loop tripped by the gateway | ✅ `test_pi_stuck_in_a_loop_is_stopped_by_the_watchdog` |
| pi adapter and budgets | ✅ `tests/test_executors.py` |
| Lima VM end to end | manual on a Mac: `foreman vm start && foreman doctor` (hosted macOS CI runners cannot run VMs) |

### Known gaps

- Hosted CI covers macOS without Docker only; Docker/Lima on a Mac is a manual check.
- The relay forwards to the gateway port only; package registries during
  agent runs need the "open" network (a registry allowlist is possible later).
