"""Supervisor process: executes Stop / E-stop requests.

Deliberately small and independent of the orchestrator. For each request in
control.db it:

1. reads the affected runs from ai_factory.db (WAL reads never wait for a writer),
2. kills their containers / process groups by handle - this needs no database
   write, so it works even if a hung orchestrator holds ai-factory.db's write lock,
3. then records the outcome: tasks -> stopped_by_user, runs -> stopped,
   project paused (these writes may wait for the lock; the processes are
   already dead by then),
4. acknowledges the request in control.db.

The orchestrator independently consults control.db before advancing any task,
so it can never report a stopped task as failed or carry on with it.

It also sweeps orphans: ai-factory containers whose run is no longer open.
"""

from __future__ import annotations

import logging
import signal
import time
from concurrent.futures import ThreadPoolExecutor

from sqlalchemy import update
from sqlmodel import col, select

from ai_factory.config import get_settings
from ai_factory.core.models import (
    Control,
    ControlAction,
    ControlScope,
    JobState,
    PlannerJob,
    Project,
    RunState,
    Task,
    TaskRun,
    TaskState,
    now,
)
from ai_factory.core.state import (
    ACTIVE_STATES,
    ESTOP_FLAG,
    emit,
    finish_run,
    heartbeat,
    set_flag,
    transition,
)
from ai_factory.db import control_session, session
from ai_factory.sandbox import kill_handle, running_containers

log = logging.getLogger("ai_factory.supervisor")

ORPHAN_SWEEP_S = 5.0


def _scoped(q, model, control: Control):  # type: ignore[no-untyped-def]
    if control.scope == ControlScope.PROJECT:
        return q.where(model.project_id == control.target_id)
    if control.scope == ControlScope.TASK:
        return q.where((model.task_id if model is TaskRun else model.id) == control.target_id)
    return q


def apply_control(control: Control, pool: ThreadPoolExecutor) -> str:
    reason = f"{control.action.value} requested by user"
    if control.action == ControlAction.ESTOP or control.scope == ControlScope.GLOBAL:
        set_flag(ESTOP_FLAG, "1")

    # 1-2. Find and kill. Reads only.
    with session() as s:
        runs = list(
            s.exec(
                _scoped(select(TaskRun).where(TaskRun.state == RunState.RUNNING), TaskRun, control)
            ).all()
        )
    kills = [pool.submit(kill_handle, r.handle) for r in runs if r.handle]
    results = [f.result() for f in kills]
    if control.scope != ControlScope.TASK:
        from ai_factory.orchestrator.milestone import kill_acceptance

        results += kill_acceptance(
            control.target_id if control.scope == ControlScope.PROJECT else None
        )

    # 3. Record. These may wait on ai-factory.db's write lock.
    if control.scope == ControlScope.PROJECT:
        # Stop = kill what runs AND start nothing new until resumed - unless the user
        # already resumed after making this request.
        with session() as s:
            s.exec(  # type: ignore[call-overload]
                update(Project)
                .where(
                    col(Project.id) == control.target_id,
                    col(Project.paused_at) < control.created_at,
                )
                .values(paused=True, paused_at=now())
            )
            s.commit()
    # Planner jobs: mark them stopped; the orchestrator sees it between streamed tokens,
    # closes the model connection and applies nothing.
    if control.scope != ControlScope.TASK:
        with session() as s:
            q = update(PlannerJob).where(
                col(PlannerJob.state).in_((JobState.QUEUED, JobState.RUNNING))
            )
            if control.scope == ControlScope.PROJECT:
                q = q.where(col(PlannerJob.project_id) == control.target_id)
            s.exec(q.values(state=JobState.STOPPED, error=reason, ended_at=now()))  # type: ignore[call-overload]
            s.commit()
    stoppable = (*ACTIVE_STATES, TaskState.QUEUED)
    task_q = select(Task).where(
        col(Task.state).in_(stoppable if control.scope == ControlScope.TASK else ACTIVE_STATES)
    )
    with session() as s:
        tasks = list(s.exec(_scoped(task_q, Task, control)).all())
    stopped = sum(transition(t.id, stoppable, TaskState.STOPPED_BY_USER, reason) for t in tasks)
    for run in runs:
        finish_run(run.id, RunState.STOPPED, reason)
        emit(
            run.project_id,
            run.task_id,
            "control",
            f"{control.action.value}: run killed",
            run_id=run.id,
        )
    return f"tasks stopped={stopped} runs killed={len(runs)} {'; '.join(results)}".strip()


def process_controls(pool: ThreadPoolExecutor) -> int:
    with control_session() as s:
        pending = s.exec(
            select(Control).where(col(Control.acked_at).is_(None)).order_by(col(Control.id))
        ).all()
    for c in pending:
        try:
            result = apply_control(c, pool)
        except Exception as e:  # noqa: BLE001 - record and keep going
            log.exception("control %s failed", c.id)
            result = f"error: {e}"
        with control_session() as s:
            row = s.get(Control, c.id)
            if row is not None:
                row.acked_at = now()
                row.result = result
                s.add(row)
                s.commit()
        log.info("control %s %s/%s -> %s", c.id, c.scope, c.action, result)
    return len(pending)


def sweep_orphans() -> None:
    """Kill ai-factory containers whose run is not open (e.g. after a crash)."""
    containers = running_containers()
    if not containers:
        return
    with session() as s:
        open_runs = {
            str(r.id) for r in s.exec(select(TaskRun).where(TaskRun.state == RunState.RUNNING))
        }
    for name, run_id in containers.items():
        if not run_id:
            continue  # acceptance checks carry no run; Stop kills them by project
        if run_id not in open_runs:
            log.warning("killing orphan container %s (run %s)", name, run_id)
            kill_handle(f"docker:{name}", 2)


class Supervisor:
    def __init__(self) -> None:
        self.settings = get_settings()
        self._running = True
        self._last_sweep = 0.0

    def stop(self, *_: object) -> None:
        self._running = False

    def run_forever(self) -> None:
        log.info("supervisor started")
        with ThreadPoolExecutor(max_workers=16) as pool:
            while self._running:
                heartbeat("supervisor")
                try:
                    process_controls(pool)
                    if time.monotonic() - self._last_sweep > ORPHAN_SWEEP_S:
                        self._last_sweep = time.monotonic()
                        sweep_orphans()
                except Exception:
                    log.exception("supervisor loop error")
                time.sleep(min(self.settings.poll_interval_s, 0.25))


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    sup = Supervisor()
    signal.signal(signal.SIGTERM, sup.stop)
    signal.signal(signal.SIGINT, sup.stop)
    sup.run_forever()
