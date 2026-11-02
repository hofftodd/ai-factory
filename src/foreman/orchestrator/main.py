"""Orchestrator process: picks ready tasks and runs them.

M1 scheduling rules:
- nothing starts while E-stop is active;
- a paused project, or a task on hold, never starts;
- a task starts only when all its dependencies are done;
- at most one active task per project (tasks merge into one milestone branch);
- a node never runs more tasks than its capacity.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import signal
from typing import Any

from sqlmodel import col, select

from foreman import engine, sandbox
from foreman.config import get_settings
from foreman.core import gates
from foreman.core.models import (
    JobKind,
    JobState,
    Node,
    PlannerJob,
    Project,
    ProjectPhase,
    RunState,
    Task,
    TaskRun,
    TaskState,
)
from foreman.core.state import (
    ACTIVE_STATES,
    emit,
    estop_active,
    finish_run,
    heartbeat,
    pending_stops,
    transition,
)
from foreman.db import session
from foreman.executors import get_executor
from foreman.gateway import Gateway, new_token, serve
from foreman.orchestrator import gate_actions, milestone
from foreman.orchestrator.jobs import JobRunner, finish_job
from foreman.orchestrator.runner import TaskRunner, resolve_executor
from foreman.sandbox import Runtime, get_runtime, kill_handle

log = logging.getLogger("foreman.orchestrator")


def recover() -> None:
    """After a restart no task may be left silently 'running' (NFR-2)."""
    with session() as s:
        runs = s.exec(select(TaskRun).where(TaskRun.state == RunState.RUNNING)).all()
    for run in runs:
        assert run.id is not None
        if run.handle:
            kill_handle(run.handle, 2)
        finish_run(run.id, RunState.INTERRUPTED, "orchestrator restarted during run")
        transition(
            run.task_id,
            ACTIVE_STATES,
            TaskState.FAILED,
            "orchestrator restarted during run; retry to continue",
        )
    # Tasks marked active without an open run (crash between transition and run insert).
    with session() as s:
        orphans = s.exec(select(Task).where(col(Task.state).in_(ACTIVE_STATES))).all()
        open_runs = {
            r.task_id for r in s.exec(select(TaskRun).where(TaskRun.state == RunState.RUNNING))
        }
    for t in orphans:
        if t.id is not None and t.id not in open_runs:
            transition(t.id, ACTIVE_STATES, TaskState.FAILED, "orchestrator restarted")
    # Planner jobs cut off mid-call.
    with session() as s:
        jobs = s.exec(select(PlannerJob).where(PlannerJob.state == JobState.RUNNING)).all()
    for job in jobs:
        assert job.id is not None
        if finish_job(job.id, (JobState.RUNNING,), JobState.FAILED, "orchestrator restarted"):
            JobRunner(job.id)._after_failure("orchestrator restarted; send your message again")


def pick_ready() -> list[tuple[Task, int | None, str]]:
    """Ready tasks in id order with their resolved (node_id, model)."""
    with session() as s:
        projects = {p.id: p for p in s.exec(select(Project))}
        tasks = s.exec(select(Task).order_by(col(Task.id))).all()
        nodes = {n.id: n for n in s.exec(select(Node))}
    stopping_projects, stopping_tasks = pending_stops()
    # Replanning happens between tasks: the next one waits for the plan review.
    with session() as s:
        reviewing = {
            j.project_id
            for j in s.exec(
                select(PlannerJob).where(
                    PlannerJob.kind == JobKind.REVIEW,
                    col(PlannerJob.state).in_((JobState.QUEUED, JobState.RUNNING)),
                )
            )
        }
    by_id = {t.id: t for t in tasks}
    busy_projects = {t.project_id for t in tasks if t.state in ACTIVE_STATES}
    node_load: dict[int, int] = {}
    for t in tasks:
        if t.state in ACTIVE_STATES:
            _, nid, _ = resolve_executor(projects[t.project_id], t)
            if nid is not None:
                node_load[nid] = node_load.get(nid, 0) + 1

    ready = []
    for t in tasks:
        p = projects.get(t.project_id)
        if t.state != TaskState.QUEUED or t.hold or p is None or p.paused or p.clone_error:
            continue
        if p.phase != ProjectPhase.EXECUTE:
            continue
        if t.project_id in busy_projects or t.project_id in stopping_projects:
            continue
        if t.project_id in reviewing:
            continue
        if t.id in stopping_tasks:
            continue
        if any(by_id.get(d) is None or by_id[d].state != TaskState.DONE for d in t.deps):
            continue
        kind, nid, model = resolve_executor(p, t)
        try:
            needs_node = get_executor(kind).needs_node
        except ValueError:
            needs_node = False
        if needs_node and nid is not None:
            node = nodes.get(nid)
            if node is None or node_load.get(nid, 0) >= node.capacity:
                continue
            node_load[nid] = node_load.get(nid, 0) + 1
        busy_projects.add(t.project_id)
        ready.append((t, nid, model))
    return ready


class Orchestrator:
    def __init__(self, runtime: Runtime | None = None):
        self.runtime = runtime or get_runtime()
        self.settings = get_settings()
        self.running: dict[int, asyncio.Task[None]] = {}
        self.jobs: dict[int, asyncio.Task[None]] = {}  # planner jobs by project id
        self.acceptance: dict[int, asyncio.Task[None]] = {}  # acceptance checks by project
        self.gateway: Gateway | None = None
        self.gateway_server: Any = None
        self.gateway_port = 0
        self._stop = asyncio.Event()

    async def ensure_gateway(self) -> None:
        if self.gateway is None:
            self.gateway = Gateway()
            bind = await asyncio.to_thread(self.gateway_bind_host)
            self.gateway_server, self.gateway_port = await serve(
                self.gateway, bind, self.settings.gateway_port
            )
            log.info("model gateway on %s:%s", bind, self.gateway_port)

    def gateway_bind_host(self) -> str:
        """Where the gateway listens: only where executors arrive (foreman.engine)."""
        if self.settings.gateway_host != "auto":
            return self.settings.gateway_host
        if self.runtime.name != "docker":
            return "127.0.0.1"
        return engine.detect().bind_host

    @property
    def gateway_url(self) -> str:
        """The gateway as reachable from where executors run."""
        if not self.gateway_port:
            return ""
        host = engine.HOST_ALIAS if self.runtime.name == "docker" else "127.0.0.1"
        return f"http://{host}:{self.gateway_port}"

    def start_task(self, task: Task, node_id: int | None, model: str) -> bool:
        assert task.id is not None
        task_id = task.id
        if not transition(task_id, (TaskState.QUEUED,), TaskState.RUNNING):
            return False
        with session() as s:
            t = s.get(Task, task_id)
            assert t is not None
            t.attempts += 1
            run = TaskRun(
                task_id=task_id,
                project_id=t.project_id,
                node_id=node_id,
                model=model,
                gateway_token=new_token(),
            )
            s.add(t)
            s.add(run)
            s.commit()
            run_id = run.id
        assert run_id is not None
        emit(task.project_id, task_id, "system", f"attempt {t.attempts} started", run_id=run_id)
        runner = TaskRunner(
            task_id, run_id, self.runtime, self.gateway_url, gateway_port=self.gateway_port
        )
        self.running[task_id] = asyncio.create_task(runner.run())
        return True

    async def tick(self) -> None:
        heartbeat("orchestrator")
        await self.ensure_gateway()
        for registry in (self.running, self.jobs, self.acceptance):
            for key, fut in list(registry.items()):
                if fut.done():
                    del registry[key]
        for gate in gates.unhandled():
            await gate_actions.handle(gate)
        if estop_active():
            return
        self.start_jobs()
        self.start_acceptance()
        for task, node_id, model in pick_ready():
            if task.id not in self.running:
                self.start_task(task, node_id, model)

    def start_jobs(self) -> None:
        """Start queued planner jobs: one at a time per project."""
        stopping, _ = pending_stops()
        with session() as s:
            queued = s.exec(
                select(PlannerJob)
                .where(PlannerJob.state == JobState.QUEUED)
                .order_by(col(PlannerJob.id))
            ).all()
            busy = {
                j.project_id
                for j in s.exec(select(PlannerJob).where(PlannerJob.state == JobState.RUNNING))
            }
            paused = {p.id for p in s.exec(select(Project).where(col(Project.paused)))}
        for job in queued:
            assert job.id is not None
            if job.project_id in busy or job.project_id in self.jobs:
                continue
            if job.project_id in stopping or job.project_id in paused:
                continue
            busy.add(job.project_id)
            self.jobs[job.project_id] = asyncio.create_task(JobRunner(job.id).run())

    def start_acceptance(self) -> None:
        stopping, _ = pending_stops()
        with session() as s:
            projects = s.exec(select(Project).where(Project.phase == ProjectPhase.EXECUTE)).all()
        for p in projects:
            assert p.id is not None
            if p.id in self.acceptance or p.id in self.jobs or p.id in stopping:
                continue
            if milestone.ready_for_acceptance(p):
                self.acceptance[p.id] = asyncio.create_task(
                    milestone.run_acceptance(p.id, self.runtime)
                )

    async def shutdown(self) -> None:
        for fut in [*self.running.values(), *self.jobs.values(), *self.acceptance.values()]:
            fut.cancel()
        if self.gateway_server is not None:
            self.gateway_server.should_exit = True
        if self.gateway is not None:
            await self.gateway.aclose()
        if self.runtime.name == "docker":
            with contextlib.suppress(Exception):
                await asyncio.to_thread(sandbox.teardown_isolation)

    async def run_forever(self) -> None:
        recover()
        log.info("orchestrator started (runtime=%s)", self.runtime.name)
        while not self._stop.is_set():
            try:
                await self.tick()
            except Exception:  # keep the loop alive; the error is logged
                log.exception("tick failed")
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._stop.wait(), self.settings.poll_interval_s)
        await self.shutdown()

    def request_stop(self) -> None:
        self._stop.set()


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    orch = Orchestrator()

    async def _amain() -> None:
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, orch.request_stop)
        await orch.run_forever()

    asyncio.run(_amain())
