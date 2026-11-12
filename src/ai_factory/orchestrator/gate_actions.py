"""Carry out decided checkpoints. Runs in the orchestrator (the only repo writer)."""

from __future__ import annotations

import asyncio
import logging

from sqlmodel import col, select

from ai_factory import gitops
from ai_factory.core import gates, roles, spend
from ai_factory.core.models import (
    Gate,
    GateKind,
    GateState,
    Project,
    RunState,
    Task,
    TaskRun,
    TaskState,
    now,
)
from ai_factory.core.state import emit, transition
from ai_factory.db import session

log = logging.getLogger("ai-factory.gates")


def send_back_to_tester(
    task_id: int, frm: tuple[TaskState, ...], note: str = "", source: str = "user"
) -> None:
    """The task's tests may be wrong or thin: the Tester writes them again. The `sent_back`
    event carries the note (the user's, or the Builder's report) to the Tester's briefing."""
    with session() as s:
        t = s.get(Task, task_id)
        if t is None or t.state not in frm:
            return
        t.tests_ready, t.tests_commit, t.no_build = False, "", False
        t.escalation_level, t.level_failures = 0, 0
        s.add(t)
        s.commit()
        project_id = t.project_id
    if transition(
        task_id, frm, TaskState.QUEUED, "sent back to the Tester" + (f": {note}" if note else "")
    ):
        emit(project_id, task_id, "sent_back", note, source=source)


def send_back_to_builder(task_id: int, findings: str, note: str = "") -> None:
    """The Reviewer's findings stand: the Builder works on the task again (its tests are
    kept). The reviewer run is marked failed with the findings and the user's note, which
    the Builder's next briefing shows as why the last attempt failed."""
    reason = "the Reviewer found problems to fix before this merges:\n" + findings
    if note:
        reason += f"\n\nFrom the user: {note}"
    with session() as s:
        t = s.get(Task, task_id)
        if t is None or t.state != TaskState.AWAITING_MERGE:
            return
        t.escalation_level, t.level_failures = 0, 0
        s.add(t)
        run = s.exec(
            select(TaskRun)
            .where(TaskRun.task_id == task_id, TaskRun.step == roles.REVIEWER)
            .order_by(col(TaskRun.id).desc())
        ).first()
        if run is not None:
            run.state, run.reason = RunState.FAILED, reason
            s.add(run)
        s.commit()
    transition(task_id, (TaskState.AWAITING_MERGE,), TaskState.QUEUED, "back to the Builder")


async def handle(gate: Gate) -> None:
    from ai_factory.orchestrator import escalation, milestone, replan
    from ai_factory.orchestrator.runner import merge_lock, merge_task

    approved = gate.state == GateState.APPROVED
    assert gate.id is not None
    try:
        if gate.kind == GateKind.TASK_MERGE and gate.task_id is not None:
            if approved:
                try:
                    async with merge_lock(gate.project_id):
                        await asyncio.to_thread(merge_task, gate.task_id)
                except gitops.GitError as e:
                    transition(
                        gate.task_id,
                        (TaskState.AWAITING_MERGE,),
                        TaskState.FAILED,
                        f"merge failed: {e}",
                    )
            else:
                transition(
                    gate.task_id,
                    (TaskState.AWAITING_MERGE,),
                    TaskState.FAILED,
                    f"merge rejected by user{': ' + gate.note if gate.note else ''}",
                )
        elif gate.kind in (GateKind.COVERAGE, GateKind.REVIEW) and gate.task_id is not None:
            if approved:  # merge anyway
                try:
                    async with merge_lock(gate.project_id):
                        await asyncio.to_thread(merge_task, gate.task_id)
                except gitops.GitError as e:
                    transition(
                        gate.task_id,
                        (TaskState.AWAITING_MERGE,),
                        TaskState.FAILED,
                        f"merge failed: {e}",
                    )
            elif gate.kind == GateKind.REVIEW:
                send_back_to_builder(
                    gate.task_id, str(gate.payload.get("findings") or ""), gate.note
                )
            else:
                send_back_to_tester(gate.task_id, (TaskState.AWAITING_MERGE,), gate.note)
        elif gate.kind == GateKind.ALREADY_BUILT and gate.task_id is not None:
            if approved:  # merge the tests: no Builder, the suite runs again first
                with session() as s:
                    t = s.get(Task, gate.task_id)
                    if t is not None:
                        t.no_build, t.escalation_level, t.level_failures = True, 0, 0
                        s.add(t)
                        s.commit()
                transition(
                    gate.task_id,
                    (TaskState.BLOCKED,),
                    TaskState.QUEUED,
                    "the user agreed the work is already there: merging its tests",
                )
            else:
                send_back_to_tester(gate.task_id, (TaskState.BLOCKED,), gate.note)
        elif gate.kind == GateKind.ESCALATION:
            if gate.task_id is not None and gate.payload.get("action") == "send_back":
                send_back_to_tester(gate.task_id, (TaskState.BLOCKED,), gate.note)
            elif gate.task_id is not None:
                escalation.resolve_blocked(gate.task_id, approved)
            elif approved:  # acceptance keeps failing: let the replanner try again
                with session() as s:
                    p = s.get(Project, gate.project_id)
                    if p is not None:
                        p.acceptance_failures, p.acceptance_commit = 0, ""
                        s.add(p)
                        s.commit()
        elif gate.kind == GateKind.PLAN_REVISION:
            rev_id = gate.payload.get("revision_id")
            if rev_id is not None:
                if approved:
                    replan.apply_revision(rev_id)
                else:
                    replan.reject_revision(rev_id)
        elif gate.kind == GateKind.SPEC_CHANGE and approved:
            replan.apply_spec_change(gate.project_id, gate.payload)
        elif gate.kind == GateKind.BUDGET:
            if approved:
                spend.raise_cap(gate.project_id, float(gate.payload.get("cap") or 0))
            else:
                with session() as s:
                    p = s.get(Project, gate.project_id)
                    if p is not None:
                        p.paused, p.paused_at = True, now()
                        s.add(p)
                        s.commit()
                emit(gate.project_id, None, "control", "project paused at its spend cap")
        elif gate.kind == GateKind.MILESTONE and approved:
            await asyncio.to_thread(milestone.publish, gate.project_id)
    except Exception:  # noqa: BLE001 - one bad gate must not wedge the others
        log.exception("handling gate %s failed", gate.id)
        emit(gate.project_id, gate.task_id, "system", f"could not carry out: {gate.title}")
    gates.mark_handled(gate.id)
