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
import time
from collections.abc import Callable
from pathlib import Path

from foreman import gitops, images, sandbox
from foreman.config import get_settings
from foreman.core import gates, watchdog
from foreman.core.contextpack import build_pack
from foreman.core.models import GateKind, Node, Project, RunState, Task, TaskRun, TaskState
from foreman.core.state import ACTIVE_STATES, emit, finish_run, stop_requested, transition
from foreman.core.watchdog import failure_signature
from foreman.db import session
from foreman.executors import ExecutorContext, OutputParser, get_executor
from foreman.sandbox import Proc, Runtime, StepSpec, get_runtime, kill_handle

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


def resolve_executor(project: Project, task: Task) -> tuple[str, int | None, str]:
    kind = task.executor_kind or project.executor_kind
    binding = project.role_bindings.get("executor", {}) if project.role_bindings else {}
    node_id = task.node_id or binding.get("node_id")
    model = task.model or binding.get("model", "")
    return kind, node_id, model


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
    ) -> int:
        """Run one container step, streaming output into events and a log file."""
        task, project = self._load()
        spec = StepSpec(
            name=f"foreman-run{self.run_id}-{name}-{int(time.time() * 1000) % 10**8}",
            workspace=gitops.task_dir(task.project_id, self.task_id),
            ctl_dir=self.run_dir / "ctl",
            command=command,
            env=env,
            image=self.image,
            network=project.network,
            labels={
                "foreman.run": str(self.run_id),
                "foreman.task": str(self.task_id),
                "foreman.project": str(task.project_id),
            },
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
            raise WatchdogTrip(f"watchdog: {reason}", reason)
        return code

    async def _toolchain_image(self, project_id: int, clone: Path) -> str:
        """Build (or reuse) the project's .foreman/Dockerfile image for this attempt. A
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
        from foreman.orchestrator import escalation

        task, _ = self._load()
        try:
            if task.state == TaskState.FAILED:
                escalation.after_failure(self.task_id, self.run_id)
        except Exception:  # noqa: BLE001 - never let bookkeeping crash the runner
            import logging

            logging.getLogger("foreman.runner").exception("post-run step failed")

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
        self._check()

        # 1. Executor
        kind, node_id, model = resolve_executor(project, task)
        executor = get_executor(kind)
        node = None
        if node_id is not None:
            with session() as s:
                node = s.get(Node, node_id)
        if executor.needs_node and (node is None or not model):
            raise ConfigError("no executor node/model configured for this project or task")
        if executor.needs_node and node is not None and node.provider != "openai":
            # Cloud keys must never enter a sandbox (NFR-5).
            raise ConfigError(f"node {node.name} ({node.provider}) cannot run executors")
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
        self.image = await self._toolchain_image(task.project_id, clone)
        self._check()
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
        ctx = ExecutorContext(context_window=node.context_window if node else None)
        inv = executor.prepare(project, task, endpoint, model, self.run_dir / "ctl", pack.text, ctx)
        self._emit(
            "system", f"executor {kind} starting" + (f" on {node.name} / {model}" if node else "")
        )
        code = await self._step(
            "execute", inv.command, inv.env, "log", task.timeout_s, output=inv.output
        )
        task = self._check()
        await asyncio.to_thread(gitops.commit_all, clone, f"Task {self.task_id}: {task.title}")
        if code != 0:
            raise StepFailed(f"executor exited with status {code}", "\n".join(self._tail))
        failure = inv.output.failure() if inv.output else None
        if failure:
            raise StepFailed(f"executor failed: {failure}", "\n".join(self._tail))

        # 2. Scope fence and diff size (FR-5.3)
        files = await asyncio.to_thread(gitops.changed_files, clone)
        outside = gitops.out_of_scope(files, task.allowed_globs)
        if outside:
            raise WatchdogTrip(f"changed files outside allowed scope: {', '.join(outside)}")
        lines = await asyncio.to_thread(gitops.diff_size, clone)
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

        if task.hold:
            transition(self.task_id, (TaskState.RUNNING,), TaskState.PAUSED, "paused after execute")
            finish_run(self.run_id, RunState.SUCCEEDED, "paused after execute")
            return

        # 3. Validation
        if not transition(self.task_id, (TaskState.RUNNING,), TaskState.VALIDATING):
            raise Stopped("not running")
        checks = [("validate", task.validation_cmd), ("tests", project.test_cmd)]
        for name, cmd in checks:
            if not cmd:
                continue
            self._check()
            self._emit("test", f"$ {cmd}", step=name)
            code = await self._step(name, ["bash", "-lc", cmd], {}, "test", task.timeout_s)
            if code != 0:
                raise StepFailed(
                    f"{name} failed (exit {code}): {cmd}", "\n".join(self._tail), kind="validation"
                )
            self._emit("test", f"{name} passed", step=name, passed=True)

        task = self._check()
        if task.hold:
            transition(
                self.task_id, (TaskState.VALIDATING,), TaskState.PAUSED, "paused after validation"
            )
            finish_run(self.run_id, RunState.SUCCEEDED, "paused after validation")
            return

        # 4. Merge, or wait for the user's approval (task_merge checkpoint)
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
        try:
            await asyncio.to_thread(merge_task, self.task_id)
        except gitops.GitError as e:
            raise StepFailed(str(e), kind="merge") from e
        finish_run(self.run_id, RunState.SUCCEEDED)

    def _previous_failure(self) -> str:
        from sqlmodel import col, select

        with session() as s:
            prev = s.exec(
                select(TaskRun)
                .where(TaskRun.task_id == self.task_id, col(TaskRun.id) != self.run_id)
                .order_by(col(TaskRun.id).desc())
            ).first()
        if prev and prev.state in (RunState.FAILED, RunState.STOPPED) and prev.reason:
            return prev.reason
        return ""


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
    from foreman.orchestrator.replan import queue_review

    queue_review(task.project_id, f"task {task.key or task_id} is done", task_id)
    transition(task_id, (TaskState.VALIDATING, TaskState.AWAITING_MERGE), TaskState.DONE)


def log_path(run_id: int, step: str) -> Path:
    return get_settings().runs_dir / str(run_id) / f"{step}.log"
