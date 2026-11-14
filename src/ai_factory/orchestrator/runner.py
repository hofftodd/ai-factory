"""Run one task attempt end to end.

    prepare clone -> executor step -> commit -> scope check
      -> validation step(s) -> merge into milestone branch -> done

Before every side-effecting step the runner re-reads the task: if the
supervisor marked it stopped (or E-stop is on) it bails out without touching
anything. State changes use compare-and-set transitions, so a Stop that lands
mid-step always wins.
"""

from __future__ import annotations

import asyncio
import contextlib
import re
import shutil
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from sqlmodel import col, select

from ai_factory import appcheck, gitops, images, llm, review, sandbox, testresults
from ai_factory.config import get_settings
from ai_factory.core import gates, roles, spend, watchdog
from ai_factory.core.contextpack import build_pack, build_role_pack
from ai_factory.core.models import (
    Event,
    GateKind,
    Node,
    Project,
    RunState,
    Task,
    TaskRun,
    TaskState,
    TestCase,
    TestRun,
    now,
)
from ai_factory.core.state import ACTIVE_STATES, emit, finish_run, stop_requested, transition
from ai_factory.core.watchdog import failure_signature
from ai_factory.db import session
from ai_factory.executors import EXIT_CONFIG, Executor, ExecutorContext, OutputParser, get_executor
from ai_factory.executors.shell import role_scripts
from ai_factory.gateway import new_token
from ai_factory.orchestrator.gate_actions import send_back_to_tester
from ai_factory.sandbox import Proc, Runtime, StepSpec, get_runtime, kill_handle

SUITE_RUNNER = "tests/run.sh"
ACCEPTANCE_RUNNER = f"{roles.ACCEPTANCE_DIR}/run.sh"
# The Builder writes this into $AI_FACTORY_RESULTS when tests contradict each other.
CONFLICT_REPORT = "tests-conflict.md"
LOG_FLUSH_S = 0.25
LOG_BATCH = 50


class Stopped(Exception):
    """The task was stopped (by the user or E-stop) while we were working."""


class StepFailed(Exception):
    """The attempt failed. `output` is the failing step's tail, for the signature."""

    kind = "executor"

    def __init__(self, reason: str, output: str = "", kind: str = ""):
        super().__init__(reason)
        self.output = output
        if kind:
            self.kind = kind


class ConfigError(StepFailed):
    """A configuration problem (no node, bad executor...): retrying can't help."""

    kind = "config"


class WatchdogTrip(StepFailed):
    kind = "watchdog"


def executor_context(node: Node | None) -> ExecutorContext:
    """What a coding agent is told about its model: window and output budget."""
    if node is None:
        return ExecutorContext()
    return ExecutorContext(
        context_window=node.context_window,
        max_output_tokens=node.max_output_tokens or None,
    )


def resolve_executor(
    project: Project, task: Task, role: str = roles.BUILDER
) -> tuple[str, int | None, str]:
    """(executor kind, node id, model) for one of the task's coding agents. A task-level
    node/model (set when escalated) applies to both the Tester and the Builder."""
    kind = task.executor_kind or project.executor_kind
    binding = roles.binding(project, role)
    node_id = task.node_id or binding.get("node_id")
    model = task.model or binding.get("model", "")
    return kind, node_id, model


def executor_choices(
    project: Project, task: Task, role: str = roles.BUILDER
) -> list[tuple[int | None, str]]:
    """Every (node id, model) a coding agent may run on, in order of preference. A node or
    model set on the task (escalation) is the only choice; otherwise the role's binding and
    its further choices."""
    if task.node_id is not None or task.model:
        _, node_id, model = resolve_executor(project, task, role)
        return [(node_id, model)]
    found: list[tuple[int | None, str]] = list(roles.choices(project, role))
    return found or [(None, "")]


def uses_pipeline(task: Task, executor: Executor) -> bool:
    """Agent tasks go Tester -> Builder; scripted shell tasks run their one script, unless
    it has a script per role (how ai-factory's tests drive the pipeline)."""
    return executor.needs_node or bool(role_scripts(task.executor_script))


def first_role(task: Task) -> str:
    return roles.BUILDER if task.tests_ready else roles.TESTER


def store_results(
    project_id: int,
    task_id: int | None,
    run_id: int | None,
    step: str,
    commit: str,
    res: testresults.Results,
    task_tests: list[str] | None = None,
    changed_cov: float | None = None,
    changed_lines: int = 0,
    results_dir: Path | None = None,
) -> TestRun:
    """Save one suite run's results and coverage, and the runner's HTML report when
    `results_dir` has one."""
    run = TestRun(
        project_id=project_id,
        task_id=task_id,
        run_id=run_id,
        step=step,
        commit=commit,
        passed=res.count("passed"),
        failed=res.count("failed"),
        skipped=res.count("skipped"),
        errors=res.count("error"),
        duration_s=sum(c.duration_s for c in res.cases),
        coverage=res.coverage,
        changed_coverage=changed_cov,
        changed_lines=changed_lines,
        files=res.per_file(),
        problem=res.problem,
    )
    with session() as s:
        s.add(run)
        s.commit()
        s.refresh(run)
        assert run.id is not None
        for c in res.cases:
            s.add(
                TestCase(
                    test_run_id=run.id,
                    name=c.name,
                    classname=c.classname,
                    file=c.file,
                    status=c.status,
                    duration_s=c.duration_s,
                    message=c.message,
                    task_test=testresults.is_task_case(c, task_tests or []),
                )
            )
        s.commit()
        s.refresh(run)
    if results_dir is not None:
        # The results are stored either way; only the HTML report would be missing.
        with contextlib.suppress(OSError):
            testresults.save_report(results_dir, get_settings().reports_dir / str(run.id))
    return run


# Lines of a suite's output that say what went wrong outside the tests themselves.
_ERROR_LINE = re.compile(
    r"error|cannot|could not|not found|not installed|missing|no such file|failed", re.I
)


def suite_failure(failing: int, total: int, code: int, tail: list[str]) -> str:
    """Why the suite failed. With no failed test the exit code came from outside the tests
    (a module whose tests never ran, a build step, a missing package): "0 of N failed"
    alone told the Builder nothing (larder-2's front end lacked its node packages), so
    quote the output lines that look like the error."""
    if failing:
        return f"{failing} of {total} failed"
    errors = [ln.strip() for ln in tail if _ERROR_LINE.search(ln)][-5:]
    said = "; ".join(e[:200] for e in errors) if errors else "see the end of its output"
    return (
        f"no test failed ({total} passed), but {SUITE_RUNNER} exited with status {code}: "
        f"something outside the tests failed, e.g. a module whose tests never ran: {said}"
    )


# An agent step's timeout grows by the task's timeout for each earlier run of that role
# that timed out, up to this many times the task's timeout.
MAX_TIMEOUT_FACTOR = 3
# Sourced before every agent step when the project has it: starts the services the tests
# need (a database...) and exports how to reach them. See contextpack.SERVICES_CONTRACT.
SERVICES_SCRIPT = "tests/services.sh"


def with_services(command: list[str]) -> list[str]:
    """`command`, run after sourcing tests/services.sh when the workspace has one, so the
    agent's shell inherits the services' variables (DATABASE_URL...)."""
    script = (
        f'if [ -f {SERVICES_SCRIPT} ]; then echo "ai-factory: starting {SERVICES_SCRIPT}"; '
        f'. ./{SERVICES_SCRIPT} || echo "ai-factory: {SERVICES_SCRIPT} failed"; fi; '
        'exec "$@"'
    )
    return ["sh", "-c", script, "sh", *command]


class TaskRunner:
    def __init__(
        self,
        task_id: int,
        run_id: int,
        runtime: Runtime | None = None,
        gateway_url: str = "",
        gateway_port: int = 0,
    ):
        self.task_id = task_id
        self.run_id = run_id
        self.runtime = runtime or get_runtime()
        # Base URL of the model gateway as seen from where the executor runs.
        self.gateway_url = gateway_url
        self.gateway_port = gateway_port
        self._tail: list[str] = []
        self.image = ""  # set once the attempt's toolchain image is ready
        self.acceptance_in_suite = 0  # acceptance test cases the last suite run included
        self.conflicted: list[str] = []  # files merged with conflict markers for the Builder
        # The milestone branch conflicted while the Tester was still to run: merged (with
        # the markers) after the Tester, for the Builder.
        self.deferred_sync = False
        self.settings = get_settings()
        self.run_dir = self.settings.runs_dir / str(run_id)
        with session() as s:
            run = s.get(TaskRun, run_id)
            assert run is not None
            self.started_at = run.started_at

    # -- helpers ---------------------------------------------------------

    def _load(self) -> tuple[Task, Project]:
        with session() as s:
            task = s.get(Task, self.task_id)
            assert task is not None
            project = s.get(Project, task.project_id)
            assert project is not None
            return task, project

    def _check(self) -> Task:
        """Raise Stopped if the task should not advance any further."""
        task, _ = self._load()
        if task.state not in ACTIVE_STATES:
            raise Stopped(task.state.value)
        if stop_requested(task.project_id, self.task_id, self.started_at):
            raise Stopped("stop requested")
        return task

    def _set_handle(self, handle: str) -> None:
        with session() as s:
            run = s.get(TaskRun, self.run_id)
            assert run is not None
            run.handle = handle
            s.add(run)
            s.commit()

    def _emit(self, kind: str, message: str, **data: object) -> None:
        task, _ = self._load()
        emit(task.project_id, self.task_id, kind, message, run_id=self.run_id, **data)

    async def _step(
        self,
        name: str,
        command: list[str],
        env: dict[str, str],
        kind: str,
        timeout_s: float,
        output: OutputParser | None = None,
        **mounts: Any,
    ) -> int:
        """Run one container step, streaming output into events and a log file.

        `output` renders the executor's raw output; `mounts` passes role restrictions
        and the results directory to StepSpec."""
        task, project = self._load()
        spec = StepSpec(
            name=f"ai-factory-run{self.run_id}-{name}-{int(time.time() * 1000) % 10**8}",
            workspace=gitops.task_dir(task.project_id, self.task_id),
            ctl_dir=self.run_dir / "ctl",
            command=command,
            env=env,
            labels={
                "ai-factory.run": str(self.run_id),
                "ai-factory.task": str(self.task_id),
                "ai-factory.project": str(task.project_id),
            },
            network=project.network,
            image=self.image,
            **mounts,
        )
        proc = await self.runtime.start(spec)
        self._set_handle(proc.handle)
        # A Stop may have landed before the handle was recorded: honour it ourselves.
        try:
            self._check()
        except Stopped:
            await asyncio.to_thread(kill_handle, proc.handle, 0)
            await proc.wait()
            raise
        self._tail = []
        watcher = asyncio.create_task(self._watch(proc.handle))
        try:
            code = await asyncio.wait_for(self._pump(proc, kind, name, output), timeout_s)
        except TimeoutError:
            await asyncio.to_thread(kill_handle, proc.handle)
            raise WatchdogTrip(
                f"{name} timed out after {int(timeout_s)}s", "\n".join(self._tail)
            ) from None
        finally:
            watcher.cancel()
        reason = self._watchdog_reason()
        if reason:
            kind = "budget" if reason.startswith("spend cap") else ""
            raise WatchdogTrip(f"watchdog: {reason}", reason, kind)
        return code

    async def _toolchain_image(self, project_id: int, clone: Path) -> str:
        """Build (or reuse) the project's .ai-factory/Dockerfile image for this attempt. A
        broken Dockerfile is a configuration problem: retrying the agent can't fix it."""
        if self.runtime.name != "docker" or images.project_dockerfile(clone) is None:
            return ""
        lines: list[str] = []

        def output(line: str) -> None:
            lines.append(line)
            if len(lines) >= LOG_BATCH:
                self._emit("log", "\n".join(lines), step="image")
                lines.clear()

        self._emit("system", f"preparing toolchain image from {images.DOCKERFILE}")
        try:
            tag = await images.ensure_image(
                project_id, clone, on_output=output, on_handle=self._set_handle
            )
        except images.ImageBuildError as e:
            self._check()  # a Stop that killed the build is a stop, not a failure
            raise ConfigError(f"{e}; fix {images.DOCKERFILE}", e.output) from e
        finally:
            if lines:
                self._emit("log", "\n".join(lines), step="image")
        self._emit("system", f"toolchain image {tag}")
        return tag

    async def _isolate(self, setup: Callable[[], None]) -> None:
        try:
            await asyncio.to_thread(setup)
        except sandbox.IsolationError as e:
            raise ConfigError(f"isolated executor network: {e}") from e

    async def _gateway_base(self, project: Project) -> str:
        """The gateway as the executor reaches it: through the relay on an isolated
        network, directly via host.docker.internal on an open one."""
        if self.runtime.name != "docker" or project.network == "open":
            return self.gateway_url
        port = self.gateway_port
        await self._isolate(lambda: sandbox.ensure_relay(port))
        return sandbox.RELAY_URL

    def _watchdog_reason(self) -> str:
        with session() as s:
            run = s.get(TaskRun, self.run_id)
            return run.watchdog if run else ""

    async def _watch(self, handle: str) -> None:
        """Kill the step as soon as the gateway trips the watchdog for this run."""
        while True:
            await asyncio.sleep(0.5)
            if self._watchdog_reason():
                await asyncio.to_thread(kill_handle, handle)
                return

    async def _pump(
        self, proc: Proc, kind: str, name: str, output: OutputParser | None = None
    ) -> int:
        """Raw output goes to the step's log file; what `output` renders (default: the
        raw line) goes to the dashboard and the failure tail."""
        task, _ = self._load()
        log_path = self.run_dir / f"{name}.log"
        buf: list[str] = []
        last = time.monotonic()

        def flush() -> None:
            nonlocal last
            if buf:
                emit(
                    task.project_id,
                    self.task_id,
                    kind,
                    "\n".join(buf),
                    run_id=self.run_id,
                    step=name,
                )
                buf.clear()
            last = time.monotonic()

        with log_path.open("a") as log:
            async for line in proc.lines():
                log.write(line + "\n")
                shown = output.render(line) if output else line
                if shown is None:
                    continue
                buf.append(shown)
                self._tail = [*self._tail[-79:], *shown.splitlines()][-80:]
                if len(buf) >= LOG_BATCH or time.monotonic() - last > LOG_FLUSH_S:
                    flush()
            flush()
        return await proc.wait()

    # -- main ------------------------------------------------------------

    async def run(self) -> None:
        try:
            await self._run()
        except Stopped as e:
            # The supervisor normally records this first; whoever gets there first wins and
            # the other write is a no-op. Never report a user stop as a failure.
            task, _ = self._load()
            if stop_requested(task.project_id, self.task_id, self.started_at):
                transition(
                    self.task_id, ACTIVE_STATES, TaskState.STOPPED_BY_USER, "stop requested by user"
                )
            finish_run(self.run_id, RunState.STOPPED, f"stopped ({e})")
        except StepFailed as e:
            self._fail(str(e), e.output, e.kind)
        except Exception as e:  # noqa: BLE001 - any crash fails the task, never wedges it
            self._fail(f"internal error: {type(e).__name__}: {e}", kind="internal")
        self._after()

    def _after(self) -> None:
        """Next step once the attempt is over: escalate a failure. (Successes queue their
        plan review in merge_task.)"""
        from ai_factory.orchestrator import escalation

        task, _ = self._load()
        try:
            if task.state == TaskState.FAILED:
                escalation.after_failure(self.task_id, self.run_id)
        except Exception:  # noqa: BLE001 - never let bookkeeping crash the runner
            import logging

            logging.getLogger("ai-factory.runner").exception("post-run step failed")

    def _fail(self, reason: str, output: str = "", kind: str = "executor") -> None:
        task, _ = self._load()
        if stop_requested(task.project_id, self.task_id, self.started_at):
            # The failure is a consequence of the stop (e.g. killed process): report the stop.
            transition(
                self.task_id, ACTIVE_STATES, TaskState.STOPPED_BY_USER, "stop requested by user"
            )
            finish_run(self.run_id, RunState.STOPPED, "stop requested by user")
            return
        with session() as s:
            run = s.get(TaskRun, self.run_id)
            if run is not None:
                run.error_sig = failure_signature(output or reason)
                run.failure_kind = kind
                s.add(run)
                s.commit()
        finish_run(self.run_id, RunState.FAILED, reason)
        transition(
            self.task_id, (TaskState.RUNNING, TaskState.VALIDATING), TaskState.FAILED, reason
        )

    async def _run(self) -> None:
        task, project = self._load()
        (self.run_dir / "ctl").mkdir(parents=True, exist_ok=True)

        clone = await asyncio.to_thread(
            gitops.prepare_task_clone, task.project_id, self.task_id, project.milestone_branch
        )
        await self._sync(task, project, clone)
        self._check()
        # Fail fast on a broken Dockerfile, before any model or gateway work.
        self.image = await self._toolchain_image(task.project_id, clone)
        self._check()

        kind, _, _ = resolve_executor(project, task)
        executor = get_executor(kind)
        if uses_pipeline(task, executor):
            await self._pipeline(task, project, clone, executor)
        else:
            await self._single_step(task, project, clone, executor)

    async def _sync(self, task: Task, project: Project, clone: Path) -> None:
        """Start every attempt from the milestone branch's latest work (other tasks may
        have merged since the clone was made), so the task's merge doesn't conflict."""
        try:
            merged = await asyncio.to_thread(
                gitops.sync_task_clone, task.project_id, self.task_id, project.milestone_branch
            )
        except gitops.MergeConflict as e:
            if not task.tests_ready and self._builder_can_resolve(
                task, project, e.files, after_tester=True
            ):
                # The Tester goes first (sent back for more tests): it works from the
                # task's own state, and the Builder gets the merge with the conflicts.
                self.deferred_sync = True
                self._emit(
                    "system",
                    f"the latest {project.milestone_branch} conflicts with this task in "
                    f"{', '.join(e.files)}; it comes in after the Tester, for the Builder "
                    "to resolve",
                    files=e.files,
                )
                return
            await self._merge_with_conflicts(task, project, e)
            merged = True
        if merged:
            self._emit("system", f"brought in the latest {project.milestone_branch}")
            if task.tests_ready:  # the Builder's diff starts after the merge
                self._update_task(tests_commit=gitops.head(clone))

    async def _merge_with_conflicts(
        self, task: Task, project: Project, e: gitops.MergeConflict
    ) -> None:
        """Merge the milestone branch with its conflict markers committed, for the Builder
        to resolve; or fail the step when the Builder may not resolve them."""
        if not self._builder_can_resolve(task, project, e.files):
            raise StepFailed(
                f"the task's work conflicts with {project.milestone_branch} "
                f"in {', '.join(e.files) or 'some files'}",
                kind="conflict",
            ) from e
        self.conflicted = await asyncio.to_thread(
            gitops.merge_keeping_conflicts, task.project_id, self.task_id, project.milestone_branch
        )
        self._emit(
            "system",
            f"brought in the latest {project.milestone_branch}; it conflicts in "
            f"{', '.join(self.conflicted)}, which the Builder resolves",
            files=self.conflicted,
        )

    def _builder_can_resolve(
        self, task: Task, project: Project, files: list[str], after_tester: bool = False
    ) -> bool:
        """Whether a merge conflict in `files` can go to the task's Builder: its tests are
        written (or, `after_tester`, the Tester writes them first), a Builder step follows,
        and every file is one the Builder may edit."""
        if not (task.tests_ready or after_tester):
            return False
        if task.no_build or task.kind in ("acceptance", "setup"):
            return False
        try:
            if not uses_pipeline(task, get_executor(resolve_executor(project, task)[0])):
                return False
        except ValueError:
            return False
        return bool(files) and not any(
            roles.is_test_path(project, f) or roles.is_env_path(f) or roles.is_test_config(f)
            for f in files
        )

    async def _merge_for_builder(self, task: Task, project: Project, clone: Path) -> None:
        """Bring in the milestone branch that conflicted before the Tester ran, keeping the
        conflict markers for the Builder; its diff starts after the merge."""
        self.deferred_sync = False
        task = self._check()
        try:
            merged = await asyncio.to_thread(
                gitops.sync_task_clone, task.project_id, self.task_id, project.milestone_branch
            )
        except gitops.MergeConflict as e:
            await self._merge_with_conflicts(task, project, e)
            merged = True
        if merged:
            self._update_task(tests_commit=gitops.head(clone))

    def _conflict_brief(self) -> str:
        return (
            "Work another task merged into the milestone branch conflicts with this task's "
            "work. These files were committed with git conflict markers (<<<<<<<, =======, "
            f">>>>>>>) still in them: {', '.join(self.conflicted)}. Resolve every conflict "
            "first, keeping what both sides meant to do: the other task's work is already "
            "merged and its tests must keep passing. Then make this task's tests pass."
        )

    async def _agent_setup(
        self, project: Project, task: Task, role: str, executor: Executor
    ) -> tuple[Node | None, str, str]:
        """(node, model, gateway endpoint) for the current run's agent. On an isolated
        network, also makes sure the executor network (and the gateway relay) exist."""
        kind, node_id, model = resolve_executor(project, task, role)
        # The scheduler picked this run's node and model (the role may have several).
        with session() as s:
            run = s.get(TaskRun, self.run_id)
            if run is not None and run.node_id is not None:
                node_id, model = run.node_id, run.model or model
        node = None
        if node_id is not None:
            with session() as s:
                node = s.get(Node, node_id)
        who = roles.ROLES[role].name if role in roles.ROLES else "executor"
        if executor.needs_node and (node is None or not model):
            raise ConfigError(f"no node/model configured for the {who} in this project or task")
        if executor.needs_node and node is not None and node.provider not in executor.providers:
            raise ConfigError(
                f"the {kind} executor can't use node {node.name} ({node.provider} API); "
                f"it supports: {', '.join(sorted(executor.providers))}"
            )
        endpoint = ""
        if executor.needs_node:
            if not self.gateway_url:
                raise ConfigError("the model gateway is not running")
            base = await self._gateway_base(project)
            with session() as s:
                run = s.get(TaskRun, self.run_id)
                assert run is not None
                endpoint = f"{base}/g/{run.gateway_token}/v1"
        elif self.runtime.name == "docker" and project.network != "open":
            await self._isolate(sandbox.ensure_exec_network)
        return node, model, endpoint

    # -- scripted tasks: one step, the task's own validation command -------------

    async def _single_step(
        self, task: Task, project: Project, clone: Path, executor: Executor
    ) -> None:
        kind = executor.kind
        node, model, endpoint = await self._agent_setup(project, task, roles.BUILDER, executor)
        pack = await asyncio.to_thread(
            build_pack,
            project,
            task,
            clone,
            context_window=node.context_window if node else None,
            previous_failure=self._previous_failure(),
        )
        if pack.over_budget and executor.needs_node:
            raise StepFailed(
                f"context pack ~{pack.tokens} tokens exceeds budget {pack.budget}; split the task",
                kind="too_big",
            )
        (self.run_dir / "context_pack.md").write_text(pack.text)
        inv = executor.prepare(
            project,
            task,
            endpoint,
            model,
            self.run_dir / "ctl",
            pack.text,
            provider=node.provider if node else "openai",
            context=executor_context(node),
        )
        self._emit(
            "system", f"executor {kind} starting" + (f" on {node.name} / {model}" if node else "")
        )
        code = await self._step(
            "execute", inv.command, inv.env, "log", task.timeout_s, output=inv.output
        )
        task = self._check()
        await asyncio.to_thread(gitops.commit_all, clone, f"Task {self.task_id}: {task.title}")
        if code == EXIT_CONFIG and executor.needs_node:
            raise ConfigError(self._tail[-1] if self._tail else f"{kind} is not usable here")
        if code != 0:
            raise StepFailed(f"executor exited with status {code}", "\n".join(self._tail))
        failure = inv.output.failure() if inv.output else None
        if failure:
            raise StepFailed(f"executor failed: {failure}", "\n".join(self._tail))

        # Scope fence and diff size (FR-5.3)
        files = await asyncio.to_thread(gitops.changed_files, clone)
        outside = gitops.out_of_scope(files, task.allowed_globs)
        if outside:
            raise WatchdogTrip(f"changed files outside allowed scope: {', '.join(outside)}")
        self._check_diff_size(project, clone, files)

        if task.hold:
            transition(self.task_id, (TaskState.RUNNING,), TaskState.PAUSED, "paused after execute")
            finish_run(self.run_id, RunState.SUCCEEDED, "paused after execute")
            return

        # Validation
        if not transition(self.task_id, (TaskState.RUNNING,), TaskState.VALIDATING):
            raise Stopped("not running")
        cmd = task.validation_cmd
        if cmd:
            self._check()
            self._emit("test", f"$ {cmd}", step="validate")
            code = await self._step("validate", ["bash", "-lc", cmd], {}, "test", task.timeout_s)
            if code != 0:
                raise StepFailed(
                    f"validate failed (exit {code}): {cmd}",
                    "\n".join(self._tail),
                    kind="validation",
                )
            self._emit("test", "validate passed", step="validate", passed=True)
        await self._merge_or_gate(project)

    def _check_diff_size(
        self, project: Project, clone: Path, files: list[str], base: str = gitops.BASE_REF
    ) -> None:
        lines = gitops.diff_size(clone, base)
        with session() as s:
            run = s.get(TaskRun, self.run_id)
            assert run is not None
            run.diff_lines = lines
            s.add(run)
            s.commit()
        max_lines = watchdog.limit(project, "max_diff_lines")
        if lines > max_lines:
            raise WatchdogTrip(f"diff too large: {lines} lines changed (limit {max_lines})")
        self._emit("diff", f"{len(files)} file(s) changed, {lines} lines", files=files)

    async def _merge_or_gate(self, project: Project) -> None:
        """The task passed: merge it, or wait for the user's approval (task_merge)."""
        task = self._check()
        if task.hold:
            transition(
                self.task_id, (TaskState.VALIDATING,), TaskState.PAUSED, "paused after validation"
            )
            finish_run(self.run_id, RunState.SUCCEEDED, "paused after validation")
            return
        if gates.enabled(project, "task_merge"):
            transition(
                self.task_id,
                (TaskState.VALIDATING,),
                TaskState.AWAITING_MERGE,
                "validated; waiting for merge approval",
            )
            finish_run(self.run_id, RunState.SUCCEEDED, "awaiting merge approval")
            gates.open_gate(
                task.project_id,
                GateKind.TASK_MERGE,
                f"Merge task {task.key or self.task_id}: {task.title}?",
                "The task passed its checks. Review the diff, then approve the merge or reject it.",
                task_id=self.task_id,
            )
            return
        async with merge_lock(task.project_id):
            await self._catch_up(project, task)
            try:
                await asyncio.to_thread(merge_task, self.task_id)
            except gitops.GitError as e:
                raise StepFailed(str(e), kind="merge") from e
        finish_run(self.run_id, RunState.SUCCEEDED)

    async def _catch_up(self, project: Project, task: Task) -> None:
        """Before merging: if other tasks merged since this one last synced (tasks run in
        parallel), bring their work in and run the whole suite again, so two tasks that
        pass on their own can't break the branch together."""
        clone = gitops.task_dir(task.project_id, self.task_id)
        try:
            merged = await asyncio.to_thread(
                gitops.sync_task_clone, task.project_id, self.task_id, project.milestone_branch
            )
        except gitops.MergeConflict as e:
            # The next attempt merges with the markers kept and the Builder resolves them.
            resolvable = self._builder_can_resolve(task, project, e.files)
            raise StepFailed(
                f"the task's work conflicts with work merged into {project.milestone_branch} "
                f"while it ran, in {', '.join(e.files) or 'some files'}",
                kind="behind" if resolvable else "conflict",
            ) from e
        if not merged or not self._suite_cmd(clone):
            return
        self._emit(
            "system",
            f"brought in work merged into {project.milestone_branch} meanwhile; "
            "running the suite again before merging",
        )
        task_tests = [
            f
            for f in gitops.changed_since(clone, gitops.BASE_REF)
            if roles.is_test_path(project, f)
        ]
        result = await self._run_suite(project, clone, "builder", task_tests, base=gitops.BASE_REF)
        if result is None:
            return
        run, code = result
        failing = run.failed + run.errors
        if run.problem or code != 0 or failing:
            raise StepFailed(
                f"tests fail after bringing in work merged into {project.milestone_branch} "
                f"while this task ran: "
                + suite_failure(failing, run.passed + failing, code, self._tail)
                + (f" ({run.problem})" if run.problem else ""),
                "\n".join(self._tail),
                kind="validation",
            )

    # -- agent tasks: Tester -> Builder -> verify -----------------------------------

    async def _pipeline(
        self, task: Task, project: Project, clone: Path, executor: Executor
    ) -> None:
        """The Tester writes the task's tests (once), the Builder makes them pass, and the
        suite's results and coverage decide whether it merges."""
        if task.tests_ready and not gitops.has_commit(clone, task.tests_commit):
            task = self._update_task(tests_ready=False, tests_commit="")  # clone was reset
        steps = [] if task.tests_ready else [roles.TESTER]
        if task.kind != "acceptance" and not task.no_build:
            steps.append(roles.BUILDER)
        for i, role in enumerate(steps):
            if i == 0:
                self._set_run(step=role)
            else:
                await self._next_run(role, project, task)
            if role == roles.TESTER:
                await self._tester(project, clone, executor)
            else:
                if self.deferred_sync:
                    await self._merge_for_builder(task, project, clone)
                await self._builder(project, clone, executor)
            task = self._check()
            if task.no_build:
                break  # its tests are the whole task (acceptance tests only)
        if task.kind == "acceptance" or not steps or task.no_build:
            if not transition(self.task_id, (TaskState.RUNNING,), TaskState.VALIDATING):
                raise Stopped("not running")
            await self._merge_or_gate(project)
            return
        # The Builder's verify step left the task VALIDATING.
        if await self._coverage_ok(project) and await self._review(project, clone):
            await self._merge_or_gate(project)

    def _set_run(self, **fields: Any) -> None:
        with session() as s:
            run = s.get(TaskRun, self.run_id)
            assert run is not None
            for k, v in fields.items():
                setattr(run, k, v)
            s.add(run)
            s.commit()

    def _update_task(self, **fields: Any) -> Task:
        with session() as s:
            t = s.get(Task, self.task_id)
            assert t is not None
            for k, v in fields.items():
                setattr(t, k, v)
            t.updated_at = now()
            s.add(t)
            s.commit()
            s.refresh(t)
            return t

    async def _next_run(
        self,
        role: str,
        project: Project,
        task: Task,
        options: list[tuple[int | None, str]] | None = None,
    ) -> None:
        """Hand over to the next agent: a new run, on its own node and gateway token.
        `options` overrides where it may run (default: the coding agent's choices)."""
        from ai_factory import residency
        from ai_factory.orchestrator.main import choose_node, node_load

        finish_run(self.run_id, RunState.SUCCEEDED, "step done")
        options = options or executor_choices(project, task, role)
        node_id, model = options[0]
        # Wait for a free slot on one of the next agent's nodes, and (on ai-swap nodes) until
        # its model can load without swapping out a model other running work is using.
        while node_id is not None:
            ids = [n for n, _ in options if n is not None]
            with session() as s:
                nodes = {n.id: n for n in s.exec(select(Node).where(col(Node.id).in_(ids)))}
            if not nodes:
                break
            with session() as s:
                wanted = residency.bound_models(
                    s.exec(select(Project)).all(), {n.id: n for n in s.exec(select(Node))}
                )
            pick, blocked = choose_node(
                options, nodes, node_load(), residency.in_use(), project, wanted=wanted
            )
            if pick is not None:
                node_id, model = pick
                residency.hold("step", self.task_id, "", self.settings.swap_wait_s)
                break
            if blocked is not None:
                waiting, first = residency.hold(
                    "step", self.task_id, blocked[2], self.settings.swap_wait_s
                )
                if not waiting:
                    node_id, model = blocked[0], blocked[1]
                    break
                if first:
                    emit(task.project_id, self.task_id, "system", f"{role} waiting: {blocked[2]}")
            self._check()
            await asyncio.sleep(1)
        with session() as s:
            run = TaskRun(
                task_id=self.task_id,
                project_id=task.project_id,
                node_id=node_id,
                model=model,
                gateway_token=new_token(),
                step=role,
            )
            s.add(run)
            s.commit()
            assert run.id is not None
            self.run_id = run.id
        self.run_dir = self.settings.runs_dir / str(self.run_id)
        (self.run_dir / "ctl").mkdir(parents=True, exist_ok=True)

    def _agent_timeout(self, task: Task, role: str) -> float:
        """The task's timeout, once more for each earlier run of this role on the task that
        timed out (MAX_TIMEOUT_FACTOR at most): a task that needs longer gets longer,
        instead of failing the same way again on every model."""
        with session() as s:
            timed_out = len(
                s.exec(
                    select(TaskRun.id).where(
                        TaskRun.task_id == self.task_id,
                        TaskRun.step == role,
                        col(TaskRun.id) != self.run_id,
                        col(TaskRun.reason).contains(f"{role} timed out after"),
                    )
                ).all()
            )
        factor = min(1 + timed_out, MAX_TIMEOUT_FACTOR)
        if factor > 1:
            self._emit(
                "system",
                f"{roles.ROLES[role].name} gets {factor}x the timeout "
                f"({int(task.timeout_s * factor)}s): {timed_out} earlier run(s) timed out",
            )
        return task.timeout_s * factor

    async def _agent(
        self, role: str, project: Project, clone: Path, executor: Executor, task_tests: list[str]
    ) -> list[str]:
        """Run one agent in its sandbox; returns the files it changed. Changes outside the
        role's capabilities are thrown away and fail the step."""
        task = self._check()
        node, model, endpoint = await self._agent_setup(project, task, role, executor)
        start = gitops.head(clone)
        test_paths = [d.strip("/") for d in (project.test_paths or ["tests"]) if d.strip("/")]
        from ai_factory.planner import doc_path

        pack = await asyncio.to_thread(
            build_role_pack,
            project,
            task,
            clone,
            role,
            docs={k: doc_path(project, k) for k in roles.PLANNING_DOCS},
            test_paths=test_paths,
            task_tests=task_tests,
            context_window=node.context_window if node else None,
            previous_failure="\n\n".join(
                x
                for x in (
                    self._conflict_brief() if role == roles.BUILDER and self.conflicted else "",
                    self._previous_failure(),
                )
                if x
            ),
            sent_back=self._sent_back(project, clone) if role == roles.TESTER else "",
            failing_acceptance=self._failing_acceptance(task),
        )
        if pack.over_budget and executor.needs_node:
            raise StepFailed(
                f"context pack ~{pack.tokens} tokens exceeds budget {pack.budget}; split the task",
                kind="too_big",
            )
        (self.run_dir / "context_pack.md").write_text(pack.text)
        inv = executor.prepare(
            project,
            task,
            endpoint,
            model,
            self.run_dir / "ctl",
            pack.text,
            provider=node.provider if node else "openai",
            role=role,
            context=executor_context(node),
        )
        who = roles.ROLES[role].name
        self._emit("system", f"{who} starting" + (f" on {node.name} / {model}" if node else ""))
        mounts: dict[str, Any] = {"results_dir": self._results_dir(f"{role}-agent")}
        if role == roles.TESTER:
            # Tests also live next to the code (src/test/, *.spec.ts) and test config files
            # anywhere: the workspace is writable and the fence below rejects the rest.
            mounts |= {"readonly_dirs": [roles.ENV_DIR]}
        else:
            mounts |= {"readonly_dirs": [*test_paths, roles.ENV_DIR]}
        code = await self._step(
            role,
            with_services(inv.command),
            inv.env,
            "log",
            self._agent_timeout(task, role),
            output=inv.output,
            **mounts,
        )
        task = self._check()
        await asyncio.to_thread(
            gitops.commit_all, clone, f"Task {task.key or self.task_id} ({role}): {task.title}"
        )
        changed = gitops.changed_since(clone, start)
        outside = roles.fence(project, role, changed)
        if role == roles.BUILDER and task.kind != "setup":
            outside += [
                f
                for f in changed
                if f not in outside
                and roles.is_test_config(f)
                and roles.test_config_changed(
                    f, gitops.file_at(clone, start, f), gitops.file_at(clone, "HEAD", f)
                )
            ]
        if outside and role == roles.BUILDER and all(roles.is_test_config(f) for f in outside):
            # Only test configuration, which is the Tester's: retrying the Builder gets
            # nowhere (it can't make the change), so the Tester gets what it wanted.
            wanted = self._config_wanted(clone, start, outside)
            await asyncio.to_thread(gitops.reset_to, clone, start)
            roles.denied(
                task.project_id, self.task_id, role, "change the test configuration", files=outside
            )
            self._tests_conflict(
                wanted, (TaskState.RUNNING,), what="needs a test configuration change"
            )
        if outside:
            await asyncio.to_thread(gitops.reset_to, clone, start)
            if any(roles.is_env_path(f) for f in outside):
                what = "change the toolchain image (.ai-factory/)"
            elif role == roles.TESTER:
                what = "change non-test files"
            elif any(roles.is_test_path(project, f) for f in outside):
                what = "change tests"
            else:
                what = "change the test configuration"
            roles.denied(task.project_id, self.task_id, role, what, files=outside)
            raise WatchdogTrip(
                f"the {who} changed files it may not touch: {', '.join(outside)}", kind="fence"
            )
        if code == EXIT_CONFIG and executor.needs_node:
            raise ConfigError(self._tail[-1] if self._tail else f"{executor.kind} is not usable")
        if code != 0:
            raise StepFailed(f"the {who} exited with status {code}", "\n".join(self._tail))
        failure = inv.output.failure() if inv.output else None
        if failure:
            raise StepFailed(f"the {who} failed: {failure}", "\n".join(self._tail))
        self._emit("diff", f"{who}: {len(changed)} file(s) changed", files=changed, role=role)
        return changed

    async def _tester(self, project: Project, clone: Path, executor: Executor) -> None:
        task = self._check()
        changed = await self._agent(roles.TESTER, project, clone, executor, [])
        # The task's tests are all it has written since it started, not only this run's: a
        # retry keeps the branch, so earlier attempts' tests may already be committed (on
        # larder-2, T12.2.1's Tester found its tests in place, changed nothing, and was
        # rejected for adding none).
        earlier = await asyncio.to_thread(gitops.changed_since, clone, gitops.BASE_REF)
        changed = sorted(set(changed) | {f for f in earlier if roles.is_test_path(project, f)})
        if not changed:
            raise StepFailed("the Tester added no tests for this task", kind="no_tests")
        if task.kind == "acceptance" and not (clone / ACCEPTANCE_RUNNER).is_file():
            raise StepFailed(f"the Tester did not write {ACCEPTANCE_RUNNER}", kind="no_tests")
        if not self._suite_cmd(clone):
            raise StepFailed(f"there is no test runner: {SUITE_RUNNER} is missing", kind="no_tests")
        # Record the new tests' first results: they are expected to fail for now.
        result = await self._run_suite(project, clone, "tester", changed, base="")
        green = (
            result is not None
            and result[1] == 0
            and not result[0].problem
            and not (result[0].failed + result[0].errors)
        )
        if task.no_build and not self._acceptance_only(task, project, changed):
            # A tests-only task (the replanner fixing tests that pinned a detail): the code
            # is already right, so the fixed tests pass from the start, and no Builder runs.
            if not green:
                raise StepFailed(
                    "this task only fixes tests, and the suite fails: make the tests check "
                    "the behaviour the documents describe, which the code already has",
                    "\n".join(self._tail),
                    kind="validation",
                )
            self._emit("system", "tests-only task: no Builder step; it merges as the suite passes")
            self._update_task(tests_ready=True, tests_commit=gitops.head(clone))
            return
        if self._acceptance_only(task, project, changed):
            # Acceptance tests run only at the end, against the finished project, so they
            # can't fail first here; the regular suite must still pass, and no Builder runs.
            if not green:
                raise StepFailed(
                    "this task changes only acceptance tests, and the regular suite fails",
                    "\n".join(self._tail),
                    kind="validation",
                )
            self._emit(
                "system",
                f"the task changes only acceptance tests ({roles.ACCEPTANCE_DIR}/): no "
                "Builder step; it merges once the suite passes",
            )
            self._update_task(tests_ready=True, tests_commit=gitops.head(clone), no_build=True)
            return
        if self._setup_only(task, changed):
            # Only how the tests run changed (e.g. tests/run.sh now installs packages): no
            # test of the task's own can fail first, and there is nothing to build.
            if not green:
                raise StepFailed(
                    "this task changes only how the tests run, and the suite fails",
                    "\n".join(self._tail),
                    kind="validation",
                )
            self._emit(
                "system",
                "the task changes only how the tests run: no Builder step; it merges once "
                "the suite passes",
            )
            self._update_task(tests_ready=True, tests_commit=gitops.head(clone), no_build=True)
            return
        if result is not None and self._needs_red(task) and not self._task_failures(result[0]):
            if green and self._task_cases(result[0]):
                self._update_task(tests_ready=True, tests_commit=gitops.head(clone))
                self._already_built(task, self._task_cases(result[0]))
            raise StepFailed(
                "none of this task's new tests fail: they already pass against the current "
                "code, so they don't test what this task must change. Write tests that fail "
                "until the task is built"
                + (
                    ", against the calls the failing acceptance tests make"
                    if self._failing_acceptance(task)
                    else ""
                ),
                kind="no_red",
            )
        if task.kind == "acceptance" and self.acceptance_in_suite:
            raise StepFailed(
                f"{SUITE_RUNNER} also runs the acceptance tests: it must leave "
                f"{roles.ACCEPTANCE_DIR}/ out (they run through {ACCEPTANCE_RUNNER} and fail "
                "until the project is done)",
                kind="validation",
            )
        self._update_task(tests_ready=True, tests_commit=gitops.head(clone))

    async def _builder(self, project: Project, clone: Path, executor: Executor) -> None:
        task = self._check()
        task_tests = gitops.changed_since(clone, gitops.BASE_REF)
        task_tests = [f for f in task_tests if roles.is_test_path(project, f)]
        changed = await self._agent(roles.BUILDER, project, clone, executor, task_tests)
        if self.conflicted:
            left = gitops.conflict_markers(clone, self.conflicted)
            if left:
                raise StepFailed(
                    f"the Builder left conflict markers in {', '.join(left)}", kind="conflict"
                )
            self._emit("system", "the Builder resolved the merge conflicts")
        report = self.run_dir / "results" / f"{roles.BUILDER}-agent" / CONFLICT_REPORT
        # A conflict report only counts if the tests then fail: a Builder that got the suite
        # green has, by definition, found no tests that can't both pass (models sometimes
        # write the file just to say there is no conflict).
        conflict = report.read_text(errors="replace").strip() if report.is_file() else ""
        self._check_diff_size(project, clone, changed, base=task.tests_commit or gitops.BASE_REF)
        if task.hold and conflict:
            self._tests_conflict(conflict, (TaskState.RUNNING,))
        if task.hold:
            transition(self.task_id, (TaskState.RUNNING,), TaskState.PAUSED, "paused after build")
            finish_run(self.run_id, RunState.SUCCEEDED, "paused after build")
            raise Stopped("paused")
        # Verify: the whole suite, in the (possibly just changed) toolchain image.
        if not transition(self.task_id, (TaskState.RUNNING,), TaskState.VALIDATING):
            raise Stopped("not running")
        self.image = await self._toolchain_image(task.project_id, clone)
        # Changed-line coverage covers all the code this task changed, across rounds.
        result = await self._run_suite(project, clone, "builder", task_tests, base=gitops.BASE_REF)
        green = (
            result is not None
            and not result[0].problem
            and result[1] == 0
            and not (result[0].failed + result[0].errors)
        )
        if conflict and not green:
            self._tests_conflict(conflict, (TaskState.VALIDATING,))
        if conflict:
            self._emit(
                "system",
                "the Builder Agent wrote a test-conflict report, but the whole suite passes, "
                "so there is no conflict; carrying on",
            )
        if result is None:
            raise StepFailed(
                f"there is no test runner: {SUITE_RUNNER} is missing", kind="validation"
            )
        run, code = result
        if run.problem:
            raise StepFailed(f"the test results are unusable: {run.problem}", kind="validation")
        failing = run.failed + run.errors
        if code != 0 or failing:
            raise StepFailed(
                "tests fail: " + suite_failure(failing, run.passed + failing, code, self._tail),
                "\n".join(self._tail),
                kind="validation",
            )
        await self._app_check(project, clone)

    async def _app_check(self, project: Project, clone: Path) -> None:
        """Start the app with scripts/start.sh and look at it in a browser (appcheck). A
        project without the script has no app to check. Problems go back to the Builder."""
        if not appcheck.applies(clone):
            return
        task = self._check()
        appcheck.install(self.run_dir / "ctl")
        results_dir = self._results_dir(appcheck.STEP)
        self._emit("test", "app check: starting the app with scripts/start.sh", step="app")
        await self._step(
            "app",
            with_services(["bash", "-lc", appcheck.shell_command()]),
            appcheck.env(self.runtime.name),
            "test",
            appcheck.CHECK_TIMEOUT_S,
            results_dir=results_dir,
        )
        await asyncio.to_thread(gitops.clean_tree, clone)
        result = appcheck.read_result(results_dir)
        res = await asyncio.to_thread(testresults.collect, results_dir, clone)
        test_run = store_results(
            task.project_id,
            self.task_id,
            self.run_id,
            appcheck.STEP,
            gitops.head(clone),
            res,
            results_dir=results_dir,
        )
        self._emit(
            "test",
            f"app check: {result.summary()}",
            step="app",
            test_run=test_run.id,
            passed=result.ok,
        )
        if not result.ok:
            raise StepFailed(
                "the app check found problems with the running app (scripts/start.sh):\n"
                + result.summary(),
                "\n".join(self._tail),
                kind="app",
            )

    def _results_dir(self, name: str) -> Path:
        path = self.run_dir / "results" / name
        if path.exists():
            shutil.rmtree(path)
        path.mkdir(parents=True)
        return path

    def _suite_cmd(self, clone: Path) -> str:
        return f"sh {SUITE_RUNNER}" if (clone / SUITE_RUNNER).is_file() else ""

    async def _run_suite(
        self, project: Project, clone: Path, step: str, task_tests: list[str], base: str
    ) -> tuple[TestRun, int] | None:
        """Run the whole suite, store its results and coverage. `base` is where the Builder's
        changes start (for changed-line coverage); "" for none."""
        cmd = self._suite_cmd(clone)
        if not cmd:
            return None
        task = self._check()
        results_dir = self._results_dir(f"{step}-suite")
        self._emit("test", f"$ {cmd}", step=f"tests-{step}")
        code = await self._step(
            f"tests-{step}",
            ["bash", "-lc", cmd],
            {},
            "test",
            task.timeout_s,
            results_dir=results_dir,
        )
        await asyncio.to_thread(gitops.clean_tree, clone)
        res = await asyncio.to_thread(testresults.collect, results_dir, clone)
        # The acceptance tests fail until the project is done: they never count for a task.
        # (tests/run.sh should leave them out; older projects' runners may not.)
        acceptance = [c for c in res.cases if testresults.in_dir(c, roles.ACCEPTANCE_DIR)]
        self.acceptance_in_suite = len(acceptance)
        if acceptance and task.kind != "acceptance":
            res.cases = [c for c in res.cases if not testresults.in_dir(c, roles.ACCEPTANCE_DIR)]
            if code != 0 and not res.count("failed") and not res.count("error"):
                code = 0  # only acceptance tests failed
        changed_cov, changed_lines = None, 0
        if base and res.lines:
            code_files = [
                f for f in gitops.changed_since(clone, base) if not roles.is_test_path(project, f)
            ]
            added = await asyncio.to_thread(testresults.added_lines, clone, base, code_files)
            changed_cov, changed_lines = testresults.changed_coverage(res.lines, added)
        run = store_results(
            task.project_id,
            self.task_id,
            self.run_id,
            step,
            gitops.head(clone),
            res,
            task_tests,
            changed_cov,
            changed_lines,
            results_dir,
        )
        summary = f"{run.passed} passed, {run.failed + run.errors} failed"
        if run.coverage is not None:
            summary += f", {run.coverage:.0f}% coverage"
        if changed_cov is not None:
            summary += f", {changed_cov:.0f}% of changed lines"
        if acceptance and task.kind != "acceptance":
            summary += f"; {len(acceptance)} acceptance tests not counted"
        if res.problem:
            summary += f" ({res.problem})"
        self._emit("test", f"tests ({step}): {summary}", step=f"tests-{step}", test_run=run.id)
        return run, code

    async def _coverage_ok(self, project: Project) -> bool:
        """Changed-line coverage against the project minimum. Below it the task goes back to
        the Tester once, then waits for the user. Setup only reports it: its code is a
        skeleton that later tasks fill in and test."""
        task = self._check()
        if task.kind == "setup":
            return True
        with session() as s:
            run = s.exec(
                select(TestRun)
                .where(TestRun.task_id == self.task_id, TestRun.step == "builder")
                .order_by(col(TestRun.id).desc())
            ).first()
        minimum = project.min_task_coverage
        cov = run.changed_coverage if run else None
        if minimum <= 0 or cov is None or cov >= minimum:
            return True
        why = f"tests cover {cov:.0f}% of the changed lines (minimum {minimum}%)"
        if task.coverage_rounds == 0:
            self._update_task(coverage_rounds=1, tests_ready=False)
            finish_run(self.run_id, RunState.SUCCEEDED, f"{why}; back to the Tester")
            self._set_run(failure_kind="coverage")
            transition(
                self.task_id,
                (TaskState.VALIDATING,),
                TaskState.QUEUED,
                f"{why}; back to the Tester",
            )
            return False
        transition(self.task_id, (TaskState.VALIDATING,), TaskState.AWAITING_MERGE, why)
        finish_run(self.run_id, RunState.SUCCEEDED, why)
        gates.open_gate(
            task.project_id,
            GateKind.COVERAGE,
            f"Task {task.key or self.task_id} is under-tested: {task.title}",
            f"The task's tests pass, but {why}, also after the Tester added more. Approve to "
            "merge it anyway, or reject to send it back to the Tester.",
            task_id=self.task_id,
        )
        return False

    async def _review(self, project: Project, clone: Path) -> bool:
        """The Reviewer Agent checks the change against the requirements and design before
        it merges (the `review` checkpoint, on by default). Blocking findings send the task
        back to the Builder; after max_review_rounds rejections the user decides. True
        when the task may merge now."""
        task = self._check()
        options: list[tuple[int | None, str]] = list(roles.choices(project, roles.REVIEWER))
        if (
            not gates.enabled(project, "review")
            or task.kind == "acceptance"
            or task.no_build
            or not options
        ):
            return True
        await self._next_run(roles.REVIEWER, project, task, options=options)
        with session() as s:
            run = s.get(TaskRun, self.run_id)
            assert run is not None
            node = s.get(Node, run.node_id) if run.node_id is not None else None
            model = run.model
        if node is None:
            self._emit("review", "no node for the Reviewer; merging without a review")
            return True
        from ai_factory.planner import doc_path

        def read(rel: str) -> str:
            path = clone / rel
            return path.read_text(errors="replace") if path.is_file() else ""

        docs_req = read(doc_path(project, "requirements"))
        docs_design = read(doc_path(project, "design"))
        code_files = [
            f
            for f in gitops.changed_since(clone, gitops.BASE_REF)
            if not roles.is_test_path(project, f)
        ]
        max_lines = int(watchdog.limit(project, "max_file_lines"))
        system = review.system_prompt(
            project,
            task,
            review.requirements_for(task, docs_req),
            docs_design,
            await asyncio.to_thread(gitops.diff, clone),
            review.big_files(clone, code_files, max_lines),
            max_lines,
        )
        self._emit("review", f"Reviewer ({model or node.name}) is reading the change")
        try:
            result = await llm.stream_chat(
                node,
                model,
                system,
                [{"role": "user", "content": "Review the change now."}],
                repo=clone,
                should_abort=lambda: stop_requested(task.project_id, self.task_id, self.started_at),
            )
        except llm.Aborted:
            raise Stopped("stopped during the review") from None
        except Exception as e:  # noqa: BLE001 - a model error fails the step like any other
            raise StepFailed(f"the review could not run: {e}", kind="executor") from e
        self._set_run(
            model_requests=1, tokens_in=result.input_tokens, tokens_out=result.output_tokens
        )
        spend.charge(task.project_id, node, result.input_tokens, result.output_tokens)
        try:
            verdict = review.parse_review(result.text)
        except review.ReviewError as e:
            self._emit("review", f"the Reviewer's answer was unreadable ({e}); merging anyway")
            return True
        for note in verdict.notes:
            self._emit("review", f"note: {note}", note=note)
        if verdict.passed:
            self._emit("review", "Reviewer: no blocking problems", passed=True)
            return True
        findings = verdict.findings()
        self._emit("review", f"Reviewer: blocking problems\n{findings}", passed=False)
        with session() as s:
            earlier = len(
                s.exec(
                    select(TaskRun).where(
                        TaskRun.task_id == self.task_id,
                        TaskRun.step == roles.REVIEWER,
                        TaskRun.failure_kind == "review",
                        col(TaskRun.id) != self.run_id,
                    )
                ).all()
            )
        if earlier + 1 < int(watchdog.limit(project, "max_review_rounds")):
            raise StepFailed(
                "the Reviewer found problems to fix before this merges:\n" + findings,
                findings,
                kind="review",
            )
        why = f"the Reviewer still objects after {earlier + 1} review(s)"
        self._set_run(failure_kind="review")
        transition(self.task_id, (TaskState.VALIDATING,), TaskState.AWAITING_MERGE, why)
        finish_run(self.run_id, RunState.SUCCEEDED, f"{why}:\n{findings}")
        gates.open_gate(
            task.project_id,
            GateKind.REVIEW,
            f"Task {task.key or self.task_id}: the Reviewer still objects: {task.title}",
            f"The task's tests pass, but {why}:\n\n{findings}\n\nApprove to merge it anyway, "
            "or reject to send it back to the Builder (your note goes with it).",
            task_id=self.task_id,
            payload={"findings": findings},
        )
        return False

    def _previous_failure(self) -> str:
        with session() as s:
            prev = s.exec(
                select(TaskRun)
                .where(TaskRun.task_id == self.task_id, col(TaskRun.id) != self.run_id)
                .order_by(col(TaskRun.id).desc())
            ).first()
        if prev and prev.state in (RunState.FAILED, RunState.STOPPED) and prev.reason:
            return prev.reason
        return ""

    def _config_wanted(self, clone: Path, start: str, files: list[str]) -> str:
        """For the Tester: the test configuration the Builder tried to change, and how."""
        parts = [
            "The Builder Agent could not finish without changing the test configuration, "
            "which is yours: " + ", ".join(files) + ". Its changes were not kept. Make the "
            "change yourself if the task needs it (or explain in the tests why it doesn't), "
            "so the Builder can work against it. What it wrote:"
        ]
        for f in files:
            after = gitops.file_at(clone, "HEAD", f)
            before = gitops.file_at(clone, start, f)
            if after is None:
                parts.append(f"\n{f}: deleted")
                continue
            label = "new file" if before is None else "changed to"
            parts.append(f"\n{f} ({label}):\n```\n{after[:1500]}\n```")
        return "\n".join(parts)

    def _tests_conflict(
        self,
        report: str,
        frm: tuple[TaskState, ...],
        what: str = "says the tests contradict each other",
    ) -> None:
        """The Builder reports tests that contradict each other, or needs a change to the
        test configuration: the task goes back to the Tester with the report. A second
        report in a row fails the attempt instead, so the two agents can't hand it back and
        forth."""
        with session() as s:
            backs = s.exec(
                select(Event)
                .where(Event.task_id == self.task_id, Event.kind == "sent_back")
                .order_by(col(Event.id).desc())
            ).all()
        if backs and backs[0].data.get("source") == roles.BUILDER:
            raise StepFailed(f"the Builder Agent {what}: {report[:500]}", kind="tests_conflict")
        self._set_run(failure_kind="tests_conflict")
        why = f"the Builder Agent {what}; back to the Tester"
        finish_run(self.run_id, RunState.SUCCEEDED, why)
        send_back_to_tester(self.task_id, frm, report[:4000], source=roles.BUILDER)
        raise Stopped("sent back to the Tester")

    def _needs_red(self, task: Task) -> bool:
        """A feature or docs task's first tests must fail: if they already pass, they test
        something that is already there. (Not once a Builder has worked on it: tests added
        after that, e.g. for coverage, pass from the start.)"""
        if task.kind in ("setup", "acceptance"):
            return False
        with session() as s:
            built = s.exec(
                select(TaskRun).where(
                    TaskRun.task_id == self.task_id, TaskRun.step == roles.BUILDER
                )
            ).first()
        return built is None

    def _setup_only(self, task: Task, changed: list[str]) -> bool:
        """The Tester changed only the test runner, the services script or test
        configuration files, and no test (larder-2's T12.1: "the test runner installs the
        front end's packages", rejected three times for having no failing test)."""
        if task.kind in ("setup", "acceptance"):
            return False
        return bool(changed) and all(
            f in (SUITE_RUNNER, SERVICES_SCRIPT) or roles.is_test_config_file(f) for f in changed
        )

    def _acceptance_only(self, task: Task, project: Project, changed: list[str]) -> bool:
        """The Tester changed only acceptance tests (a feature task the Task Planner added
        to refine them): there is nothing for a Builder to do."""
        if task.kind in ("setup", "acceptance"):
            return False
        tests = [f for f in changed if roles.is_test_path(project, f)]
        prefix = roles.ACCEPTANCE_DIR + "/"
        return bool(tests) and all(f.startswith(prefix) for f in tests)

    def _already_built(self, task: Task, passing: int) -> None:
        """Every one of the task's new tests passed before any work: what it asks for is
        probably there already (an earlier task built it). The user decides: merge the
        tests and call the task done, or send it back to the Tester."""
        why = (
            f"its {passing} new tests already pass before any work: what it asks for looks "
            "built already"
        )
        if not transition(self.task_id, (TaskState.RUNNING,), TaskState.BLOCKED, why):
            raise Stopped("not running")
        finish_run(self.run_id, RunState.SUCCEEDED, "tests already pass; asking the user")
        gates.open_gate(
            task.project_id,
            GateKind.ALREADY_BUILT,
            f"Task {task.key or self.task_id} looks done already: {task.title}",
            f"All {passing} of the tests the Tester wrote for it pass against the current "
            "code, before any Builder work, so an earlier task probably built this. Approve "
            "to merge the tests (after the suite runs again) and mark the task done; reject "
            "to send it back to the Tester for tests of what is still missing.",
            task_id=self.task_id,
        )
        raise Stopped("waiting for the user: tests already pass")

    def _task_cases(self, run: TestRun) -> int:
        with session() as s:
            return len(
                s.exec(
                    select(TestCase).where(TestCase.test_run_id == run.id, col(TestCase.task_test))
                ).all()
            )

    def _task_failures(self, run: TestRun) -> int:
        with session() as s:
            return len(
                s.exec(
                    select(TestCase).where(
                        TestCase.test_run_id == run.id,
                        col(TestCase.task_test),
                        col(TestCase.status).in_(("failed", "error")),
                    )
                ).all()
            )

    def _failing_acceptance(self, task: Task, limit: int = 25) -> list[str]:
        """The failing acceptance tests a repair task was added for: those of the last
        acceptance run before it was added (by the replanner or by hand). Tasks added
        before any acceptance run have none."""
        if task.kind == "acceptance":
            return []
        with session() as s:
            run = s.exec(
                select(TestRun)
                .where(
                    TestRun.project_id == task.project_id,
                    TestRun.step == "acceptance",
                    TestRun.created_at <= task.created_at,
                )
                .order_by(col(TestRun.id).desc())
            ).first()
            if run is None or not (run.failed or run.errors):
                return []
            cases = s.exec(
                select(TestCase)
                .where(
                    TestCase.test_run_id == run.id,
                    col(TestCase.status).in_(("failed", "error")),
                )
                .order_by(col(TestCase.id))
            ).all()
        out = []
        for c in cases[:limit]:
            where = c.file or c.classname
            message = " ".join(c.message.split())[:300]
            out.append(f"`{where}::{c.name}`" + (f": {message}" if message else ""))
        if len(cases) > limit:
            out.append(f"... and {len(cases) - limit} more")
        return out

    def _sent_back(self, project: Project, clone: Path) -> str:
        """Why the task's tests came back to the Tester: the Builder's code is under-tested
        (with the files that need it most), and the note it was sent back with (the user's,
        or the Builder's report of contradicting tests)."""
        with session() as s:
            suite = s.exec(
                select(TestRun)
                .where(TestRun.task_id == self.task_id)
                .order_by(col(TestRun.id).desc())
            ).first()
            built = s.exec(
                select(TaskRun)
                .where(TaskRun.task_id == self.task_id, TaskRun.step == roles.BUILDER)
                .order_by(col(TaskRun.id).desc())
            ).first()
            back = s.exec(
                select(Event)
                .where(Event.task_id == self.task_id, Event.kind == "sent_back")
                .order_by(col(Event.id).desc())
            ).first()
        lines: list[str] = []
        minimum = project.min_task_coverage
        cov = suite.changed_coverage if suite and suite.step == "builder" else None
        if suite and cov is not None and minimum > 0 and cov < minimum:
            lines += [
                f"The Builder's code passes the tests, but they run only {cov:.0f}% of the "
                f"lines this task changed (minimum {minimum}%). Add tests that exercise the "
                "code below, including its error paths; the code itself is in the workspace.",
                "",
            ]
            code = {
                f
                for f in gitops.changed_since(clone, gitops.BASE_REF)
                if not roles.is_test_path(project, f)
            }
            thin = sorted(
                (c["covered"] / c["total"], f, c)
                for f, c in suite.files.items()
                if f in code and c.get("total") and c["covered"] < c["total"]
            )
            lines += [
                f"- `{f}`: {c['covered']} of {c['total']} lines run by the tests"
                for _, f, c in thin[:15]
            ]
        if back and back.message and built and back.ts >= built.started_at:
            if back.data.get("source") == roles.BUILDER:
                lines += [
                    "The Builder Agent found tests that contradict each other, so the suite "
                    "can never pass. Fix or remove the test that is wrong. Its report:",
                ]
            else:
                lines += ["", "The user's note:"]
            lines.append(back.message.strip())
        return "\n".join(lines).strip()


_MERGE_LOCKS: dict[int, asyncio.Lock] = {}


def merge_lock(project_id: int) -> asyncio.Lock:
    """One merge into a project's milestone branch at a time (tasks may run in parallel;
    the main clone is a single working tree)."""
    return _MERGE_LOCKS.setdefault(project_id, asyncio.Lock())


def merge_task(task_id: int) -> None:
    """Merge a validated task into the milestone branch and mark it done.

    Used by the runner, and by the orchestrator when a task_merge checkpoint is
    approved. Raises GitError on conflict.
    """
    with session() as s:
        task = s.get(Task, task_id)
        assert task is not None
        project = s.get(Project, task.project_id)
        assert project is not None
    gitops.merge_task(task.project_id, task_id, task.title)
    emit(task.project_id, task_id, "system", f"merged into {project.milestone_branch}")
    if project.push_after_merge:
        try:
            gitops.push(task.project_id, project.repo_url, project.milestone_branch)
            emit(task.project_id, task_id, "system", f"pushed {project.milestone_branch}")
        except gitops.GitError as e:
            # The merge stands locally; a failed push must not fail the task.
            emit(task.project_id, task_id, "system", f"push failed: {e}")
    # Queue the plan review *before* marking the task done, so the scheduler never
    # starts the next task in between (replanning happens between tasks).
    from ai_factory.orchestrator.replan import queue_review

    queue_review(task.project_id, f"task {task.key or task_id} is done", task_id)
    transition(task_id, (TaskState.VALIDATING, TaskState.AWAITING_MERGE), TaskState.DONE)


def log_path(run_id: int, step: str) -> Path:
    return get_settings().runs_dir / str(run_id) / f"{step}.log"
