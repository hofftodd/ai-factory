"""The run report and the agent-vs-factory causes of failed attempts."""

from __future__ import annotations

from ai_factory import report
from ai_factory.core import causes, gates
from ai_factory.core.models import (
    Event,
    GateKind,
    PlannerJob,
    RunState,
    Task,
    TaskRun,
    TaskState,
)
from ai_factory.db import session
from tests.helpers import make_project

H = 3600.0
T0 = 1_800_000_000.0


def test_failure_kinds_map_to_causes():
    assert causes.cause_of(TaskRun(task_id=1, project_id=1, failure_kind="validation")) == "agent"
    assert causes.cause_of(TaskRun(task_id=1, project_id=1, failure_kind="app")) == "agent"
    assert causes.cause_of(TaskRun(task_id=1, project_id=1, failure_kind="behind")) == (
        "coordination"
    )
    assert causes.cause_of(TaskRun(task_id=1, project_id=1, failure_kind="internal")) == "factory"
    assert causes.cause_of(TaskRun(task_id=1, project_id=1, failure_kind="weird")) == "other"
    marked = TaskRun(task_id=1, project_id=1, failure_kind="validation", cause_override="factory")
    assert causes.cause_of(marked) == "factory"


def seed(pid: int) -> tuple[int, int]:
    """Two tasks: T1 done after a failed Builder attempt (one marked a factory bug), T2
    failed on an internal error after a 2-hour stall while a checkpoint waited."""
    with session() as s:
        t1 = Task(project_id=pid, key="T1", title="Greet | people", state=TaskState.DONE)
        t2 = Task(project_id=pid, key="T2", title="Wave", state=TaskState.FAILED)
        s.add(t1)
        s.add(t2)
        s.commit()
        s.refresh(t1)
        s.refresh(t2)
        assert t1.id is not None and t2.id is not None

        def run(task_id: int, step: str, start: float, end: float, **kw) -> None:
            s.add(
                TaskRun(
                    task_id=task_id,
                    project_id=pid,
                    step=step,
                    started_at=T0 + start,
                    ended_at=T0 + end,
                    state=kw.pop("state", RunState.SUCCEEDED),
                    **kw,
                )
            )

        run(t1.id, "tester", 0, 0.5 * H, tokens_in=1000, tokens_out=100)
        run(t1.id, "builder", 0.5 * H, 1 * H, state=RunState.FAILED, failure_kind="validation")
        run(
            t1.id,
            "builder",
            1 * H,
            1.5 * H,
            state=RunState.FAILED,
            failure_kind="validation",
            cause_override="factory",
        )
        run(t1.id, "builder", 1.5 * H, 2 * H)
        run(t1.id, "reviewer", 2 * H, 2.1 * H, tokens_in=500, tokens_out=50)
        # 2-hour gap, then T2 fails on an internal error.
        run(t2.id, "tester", 4.1 * H, 4.5 * H, state=RunState.FAILED, failure_kind="internal")
        s.add(
            PlannerJob(
                project_id=pid,
                kind="decompose",
                started_at=T0 - 0.2 * H,
                ended_at=T0,
                input_tokens=2000,
                output_tokens=300,
            )
        )
        s.add(Event(project_id=pid, task_id=t1.id, kind="review", data={"passed": False}))
        s.add(Event(project_id=pid, task_id=t1.id, kind="review", data={"passed": True}))
        s.add(Event(project_id=pid, task_id=t1.id, kind="review", data={"note": "rename it"}))
        s.commit()
    gate = gates.open_gate(pid, GateKind.MILESTONE, "Approve the docs change")
    with session() as s:
        g = s.get(type(gate), gate.id)
        assert g is not None
        g.created_at, g.resolved_at = T0 + 2.5 * H, T0 + 4 * H
        s.add(g)
        s.commit()
    return t1.id, t2.id


def test_the_report_shows_time_causes_tasks_and_stalls(client, origin):
    p = make_project(client, origin)
    seed(p["id"])
    text = report.build(p["id"], now=T0 + 5 * H)
    assert text.startswith("# Run report: demo")
    assert "4h 42m" in text  # from the planner's start to the last run's end
    assert "| Tasks | 2 (1 done, 1 failed) |" in text
    assert "| Failed runs | 3 |" in text
    assert "| Tokens | 3,500 in, 450 out |" in text
    assert "| agent: " in text and "| 1 | validation 1 |" in text
    (factory,) = [ln for ln in text.splitlines() if ln.startswith("| factory: ")]
    assert "| 2 |" in factory and "internal 1" in factory and "validation 1" in factory
    assert "| T1 Greet / people | done | 2h 06m | 1 / 3 / 1 | 2 (validation 2) | 1,650 |" in text
    assert "## Stalls" in text and "| 2h 00m | waiting for you: Approve the docs change |" in text
    assert "- 2 reviews, 1 sent back to the Builder, 1 notes" in text


def test_an_empty_project_says_so(client, origin):
    p = make_project(client, origin)
    assert "Nothing has run yet." in report.build(p["id"])


def test_the_report_is_served_and_saved(client, origin):
    p = make_project(client, origin)
    seed(p["id"])
    r = client.get(f"/api/projects/{p['id']}/report")
    assert r.status_code == 200 and r.text.startswith("# Run report: demo")
    assert (report.get_settings().reports_dir / f"project-{p['id']}.md").is_file()
    assert client.get("/api/projects/999/report").status_code == 404


def test_resolving_a_blocked_task_can_mark_a_factory_bug(client, origin):
    p = make_project(client, origin)
    _, t2 = seed(p["id"])
    gate = gates.open_gate(p["id"], GateKind.ESCALATION, "T2 is blocked", task_id=t2)
    with session() as s:
        run = s.get(TaskRun, 6)
        assert run is not None and run.failure_kind == "internal"
        run.failure_kind = "validation"  # looked like the agent's fault
        s.add(run)
        s.commit()
    r = client.post(f"/api/gates/{gate.id}/resolve", json={"approve": False, "factory_bug": True})
    assert r.status_code == 200, r.text
    with session() as s:
        run = s.get(TaskRun, 6)
        assert run is not None and causes.cause_of(run) == "factory"
